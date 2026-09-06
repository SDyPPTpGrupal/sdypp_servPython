# Contrato de servicio — App Java ↔ App Python

**Versión 1.3 — 06/09/2026**

Especificación de lo que las dos implementaciones tienen que responder **igual**, para que sean
intercambiables detrás del balanceador del equipo Plataforma.

> **Esto no es una lista de propuestas.** Cada punto está decidido. Si algo hay que cambiar, se
> cambia sobre este documento y sube la versión — no se resuelve por chat ni se asume distinto de
> cada lado.
>
> El motivo es concreto: al comparar las dos implementaciones lado a lado aparecieron ocho
> divergencias. Un cliente que consume el servicio a través del balanceador recibe respuestas
> distintas según qué réplica lo atendió, y termina rompiéndose.

**Estado de implementación:** App Python ✅ al día con la v1.3 salvo la bitácora · App Java ⬜ pendiente (ver §7)

---

## 1. Reglas generales

| Regla | Valor |
| :--- | :--- |
| Formato | JSON en request y response |
| Encoding | UTF-8 |
| `Content-Type` de respuesta | `application/json; charset=utf-8` |
| Puerto | Primer argumento de línea de comandos; si no, variable `PORT`; default **8080** |
| Identidad de la instancia | Variable de entorno **`HOST_NAME`** |
| Nodo donde corre | Variable de entorno **`CASA`** (ej. `casa-tomas`) |
| Métodos usados | Sólo `GET` y `POST` |

Las claves del JSON van **en el orden en que están especificadas** en este documento. No es un
requisito técnico —un parser JSON no mira el orden— pero permite comparar dos respuestas a simple
vista y detectar una divergencia sin herramientas.

> **Sobre el puerto.** El default `8080` sirve para desarrollo y para levantar réplicas sin
> permisos especiales. En el despliegue el puerto real se pasa siempre explícito, como primer
> argumento o por `PORT`; si ese destino es un puerto privilegiado (<1024), el proceso necesita
> permisos para hacer el `bind` o falla con `Permission denied`.

---

## 2. `GET /` — identidad de la instancia

**Respuesta `200`:**

```json
{
  "app": "python",
  "lenguaje": "Python 3.14.7",
  "equipo": [
    { "nombre": "Tomás", "apellido": "Resnik", "legajo": 190168 },
    { "nombre": "Mateo", "apellido": "Nomico", "legajo": 168102 },
    { "nombre": "Salvador", "apellido": "Baez", "legajo": 195157 }
  ],
  "version": 1,
  "mensaje": "hola mundo python",
  "host": "casa-tomas",
  "arrancado": "2026-09-06T12:33:34-03:00"
}
```

| Campo | Tipo | Detalle |
| :--- | :--- | :--- |
| `app` | string | `"python"` o `"java"`. Es lo que permite ver qué implementación atendió. |
| `lenguaje` | string | Texto libre, informativo. |
| `equipo` | array de objetos | Un objeto por integrante, con `nombre` (string), `apellido` (string) y `legajo` (number). |
| `version` | number | Entero. Lo que cambia en cada deploy. |
| `mensaje` | string | Texto plano. Lo que cambia en cada deploy. |
| `host` | string | Valor de `HOST_NAME`. |
| `arrancado` | string | ISO-8601 con offset, **precisión de segundos, sin fracción**. |

> **`equipo` es una lista de objetos, no de strings.** App Python venía mandando
> `"Tomás Resnik (Legajo 190168)"` y App Java `"Agustina"`: dos formatos distintos, y el primero
> obliga a parsear por paréntesis para sacar el legajo. Con un campo por dato no queda nada que
> parsear y las dos implementaciones mandan la misma estructura.
>
> El `legajo` es **number**, no string: es el campo por el que un cliente identifica a una persona,
> y es el mismo tipo que usa `/personas` en §6.

> **`mensaje` es texto plano.** Es el campo que un cliente procesaría de verdad —los demás son
> metadatos de la instancia—, así que se evaluó darle estructura propia. Se deja plano a
> propósito: hoy su único uso es hacer visible el cambio de contenido en cada deploy, y una
> estructura que ningún cliente consume es una forma más de divergir entre las dos apps. Si
> alguna vez transporta datos, cambia acá y sube la versión del contrato.

> **`arrancado` sin fracción de segundo.** Java devuelve nanosegundos por defecto
> (`2026-09-06T12:33:34.270495056-03:00`), lo cual no es comparable contra la otra implementación.
> En Java: `OffsetDateTime.now().truncatedTo(ChronoUnit.SECONDS).toString()`.

---

## 3. `GET /health` — chequeo de salud

**Respuesta `200`:**

```json
{ "status": "ok", "app": "python", "version": 1 }
```

Es lo que consulta el balanceador para decidir si la instancia sigue en rotación. Cualquier
respuesta distinta de `200` significa que la instancia no está sana.

---

## 4. `POST /echo`

**Request:** `{"ping": "algo"}`

**Respuesta `200`:**

```json
{ "pong": "algo", "servidoPor": "python", "version": 1 }
```

**Si falta el campo `ping`** (o el cuerpo viene vacío): `400` con
`{"error": "se requiere el campo ping"}`.

> Antes App Python devolvía `200` con `{"pong": ""}` y App Java `400`. Se adopta el `400`: una
> petición sin el campo obligatorio es inválida, no una petición con valor vacío.

---

## 5. `GET /slow` — petición lenta

Duerme **4 segundos** y responde `200`:

```json
{
  "status": "ok",
  "mensaje": "...",
  "app": "python",
  "version": 1
}
```

Entra al contrato porque hace falta para validar dos comportamientos del sistema:

1. **Graceful shutdown:** verificar que una petición en vuelo se completa aunque el proceso reciba
   `SIGTERM`.
2. **Deploy sin downtime:** mantener tráfico en curso durante el blue-green y comprobar que no se
   pierde ninguna petición.

Si sólo una de las dos apps la expone, la ruta devuelve `200` o `404` según a qué réplica derive
el balanceador.

---

## 6. `/personas` — estado compartido

Las dos apps leen y escriben sobre **la misma base**. El estado sale de las instancias, que quedan
*stateless* y por lo tanto reemplazables entre sí.

### Motor: **Redis**

Su `INCR` es atómico: garantiza que dos réplicas dando de alta al mismo tiempo no se pisen los
`id` sin necesidad de coordinarse entre ellas. `SETNX` da la misma garantía para detectar legajos
duplicados. Sin esa atomicidad haría falta un mecanismo de exclusión mutua entre casas, que es un
problema bastante más grande que el que resuelve.

| Clave | Tipo | Contenido |
| :--- | :--- | :--- |
| `personas:seq` | string | Contador. `INCR` devuelve el `id` de la próxima persona. |
| `persona:<id>` | hash | `nombre`, `legajo` |
| `personas:index` | sorted set | miembro `<id>`, score `<id>` — mantiene el orden de listado |
| `legajo:<legajo>` | string | `<id>`. Se crea con `SETNX` para detectar duplicados. |

La URL de conexión llega por la variable de entorno **`TP_REDIS_URL`**. Lleva la contraseña
adentro, así que **no se versiona**: va en el `.env` de cada nodo, que está en el `.gitignore`.

### `GET /personas` → `200`

```json
{
  "servidoPor": "python",
  "personas": [
    { "id": 1, "nombre": "Ada Lovelace", "legajo": 100200 }
  ]
}
```

- Ordenadas por **`id` ascendente** (`ZRANGE personas:index 0 -1`). Sin un orden fijo, dos réplicas
  devuelven el mismo conjunto en distinta secuencia y el servicio parece comportarse de forma
  errática.
- Sin personas cargadas: `200` con `"personas": []`. **No** es `404`.
- `nombre` es el **nombre completo en un solo campo**, a diferencia del `equipo` de §2, que separa
  `nombre` y `apellido`. La asimetría es deliberada: el `equipo` es un dato fijo del contrato, que
  las dos apps escriben a mano y del que se conoce el legajo; una persona es un dato que carga el
  cliente, y partirlo en dos campos obliga a decidir qué pasa con los nombres compuestos. Si el
  grupo prefiere unificar las dos formas, se cambia acá y sube la versión.

### `POST /personas`

**Request:** `{"nombre": "Ada Lovelace", "legajo": 100200}`

**Respuesta `201`:**

```json
{ "servidoPor": "python", "id": 1, "nombre": "Ada Lovelace", "legajo": 100200 }
```

| Situación | Código | Cuerpo |
| :--- | :--- | :--- |
| Alta correcta | `201` | la persona creada + `servidoPor` |
| Cuerpo ausente, no parseable como JSON, o que no es un objeto | `400` | `{"error": "cuerpo JSON inválido"}` |
| Falta `nombre` o falta `legajo` | `400` | `{"error": "se requieren los campos nombre y legajo"}` |
| `legajo` no es un entero | `400` | `{"error": "legajo debe ser numérico"}` |
| `legajo` entero pero fuera de rango | `400` | `{"error": "legajo fuera de rango"}` |
| `nombre` no es string, o supera los 120 caracteres | `400` | `{"error": "nombre inválido"}` |
| `legajo` ya registrado | `409` | `{"error": "el legajo ya está registrado"}` |
| La base no responde | `503` | `{"error": "base de datos no disponible"}` |

El `id` **lo asigna la base**, nunca la app.

### Validación: reglas y casos borde

Las dos apps validan **igual** y en **este orden**. El orden es parte del contrato: ante un cuerpo
con dos problemas a la vez, las dos tienen que devolver el mismo error, no cada una el que detectó
primero.

1. **El cuerpo tiene que ser un objeto JSON.** Vacío, texto suelto, un array o un escalar → `400`
   `cuerpo JSON inválido`. **No se exige `Content-Type: application/json`**: se parsea el cuerpo
   venga con el header que venga. (Un `curl -d` manda `application/x-www-form-urlencoded` por
   defecto, y perder la demo por eso sería absurdo.)
2. **Presencia de `nombre` y `legajo`.** Cuenta como ausente: la clave que no está, la que vale
   `null`, y un `nombre` que queda vacío después del trim. Las claves son **case-sensitive**:
   `"Nombre"` no es `"nombre"`, así que falta el campo.
3. **`legajo` entero.** Tiene que venir como número JSON entero. Se rechazan el string numérico
   (`"100200"`), el decimal (`3.7`), el booleano y la notación exponencial.
4. **`legajo` en rango `1 … 2147483647`.** Cero y negativos no son legajos, y el tope es el máximo
   de un entero de 32 bits.
5. **`nombre` string de 1 a 120 caracteres**, ya trimeado.
6. **`legajo` no registrado**, o `409`.

Reglas que aplican a todo lo anterior:

| Regla | Decisión |
| :--- | :--- |
| Espacios en `nombre` | Se hace **trim** de los extremos. Los espacios internos se preservan tal cual: **no** se colapsan. |
| Largo de `nombre` | Se mide en caracteres sobre el valor ya trimeado (`len()` en Python, `String.length()` en Java). |
| Campos de más | **Se ignoran** en silencio, incluido un `id` que venga en el cuerpo. |
| `nombre` duplicado | **Permitido.** Lo único único es el `legajo`. |
| Cuerpo inválido en `POST /echo` | Mismo `400` `cuerpo JSON inválido` de la regla 1. |

> **Por qué se rechaza el `legajo` como string.** Aceptarlo obliga a las dos apps a coincidir en
> cómo convierten `"0100200"`, `" 100200 "` y `"1e5"` — tres decisiones más donde divergir, todas
> invisibles hasta que alguien manda ese cuerpo. Rechazar es una sola regla y da el mismo resultado
> de los dos lados.

> **Por qué el tope de 2147483647.** Python maneja enteros de precisión ilimitada y Java, con un
> `int`, desborda en silencio. Sin un tope explícito, un legajo de veinte dígitos se guarda bien
> por una réplica y se rompe o se trunca en la otra. El límite se elige por el lenguaje más
> restrictivo de los dos.

> **Por qué los campos de más se ignoran.** Es lo que permite que el contrato crezca sin romper a
> un cliente viejo: agregar un campo opcional no invalida las peticiones que no lo mandan.
> Rechazarlos obligaría a las dos apps a mantener idéntica la lista exacta de claves aceptadas.

### Matriz de verificación

Sirve como batería de pruebas y como lo que el equipo verificador puede disparar contra la URL
pública. Las dos apps tienen que dar exactamente lo mismo.

| Cuerpo del `POST /personas` | Esperado |
| :--- | :--- |
| `{"nombre":"Ada Lovelace","legajo":100200}` | `201` |
| `{"nombre":"  Ada Lovelace  ","legajo":100201}` | `201`, guardado como `"Ada Lovelace"` |
| `{"nombre":"Ada  Lovelace","legajo":100202}` | `201`, los dos espacios internos se conservan |
| `{"nombre":"Ada","legajo":100200}` (legajo repetido) | `409` |
| `{"nombre":"Grace","legajo":100203,"rol":"almirante"}` | `201`, `rol` ignorado |
| `{"nombre":"Grace","legajo":100204,"id":99}` | `201` con el `id` de la base, no `99` |
| `{"nombre":"Ada"}` | `400` campos requeridos |
| `{"legajo":100205}` | `400` campos requeridos |
| `{"nombre":"","legajo":100206}` | `400` campos requeridos |
| `{"nombre":"   ","legajo":100207}` | `400` campos requeridos |
| `{"nombre":null,"legajo":100208}` | `400` campos requeridos |
| `{"Nombre":"Ada","legajo":100209}` | `400` campos requeridos |
| `{"nombre":"Ada","legajo":"100210"}` | `400` legajo debe ser numérico |
| `{"nombre":"Ada","legajo":3.7}` | `400` legajo debe ser numérico |
| `{"nombre":"Ada","legajo":true}` | `400` legajo debe ser numérico |
| `{"nombre":"Ada","legajo":0}` | `400` legajo fuera de rango |
| `{"nombre":"Ada","legajo":-5}` | `400` legajo fuera de rango |
| `{"nombre":"Ada","legajo":99999999999}` | `400` legajo fuera de rango |
| `{"nombre":123,"legajo":100211}` | `400` nombre inválido |
| `{"nombre":"<121 caracteres>","legajo":100212}` | `400` nombre inválido |
| `{"nombre":"<120 caracteres>","legajo":100213}` | `201` |
| *(cuerpo vacío)* | `400` cuerpo JSON inválido |
| `no soy json` | `400` cuerpo JSON inválido |
| `["Ada",100214]` | `400` cuerpo JSON inválido |
| `{"nombre":"Ada","legajo":100215}` con Redis caído | `503` |

> **`503` cuando Redis no está.** `/personas` no tiene degradación posible: sin base no hay datos.
> Devolver `200` con una lista vacía sería peor que fallar, porque el cliente no puede distinguir
> "no hay personas cargadas" de "no pude leerlas". Las demás rutas (`/`, `/health`, `/echo`,
> `/slow`) siguen respondiendo normal: no dependen de la base.

---

## 7. Qué tiene que cambiar cada equipo

### App Java ⬜

| # | Cambio | Cómo |
| :--- | :--- | :--- |
| 1 | `equipo` como lista de objetos | §2 — hoy manda `["Agustina", ...]`, van objetos con `nombre`, `apellido` y `legajo` |
| 2 | `arrancado` sin fracción | `OffsetDateTime.now().truncatedTo(ChronoUnit.SECONDS)` |
| 3 | Agregar `GET /slow` | `Thread.sleep(4000)` y la respuesta de §5 |
| 4 | Agregar `/personas`, con la validación y el orden de §6 | §6 |
| 5 | Bitácora a disco | §8 |

Ya cumple sin cambios: `404` `{"error":"ruta no encontrada"}`, `405`
`{"error":"metodo no permitido"}`, `POST /echo` sin `ping` → `400`, variable `HOST_NAME`,
puerto default `8080`.

### App Python ✅

Implementado y verificado corriendo las dos apps lado a lado. Pendiente: la bitácora (§8).

| # | Cambio | Estado |
| :--- | :--- | :--- |
| 1 | `equipo` como lista de objetos | ✅ v1.2 |
| 2 | `arrancado` sin fracción | ✅ |
| 3 | `GET /slow` | ✅ |
| 4 | `/personas`, con la validación y el orden de §6 | ✅ v1.3 |
| 5 | Bitácora a disco | ⬜ |

---

## 8. Bitácora a disco

Cada instancia escribe **una línea por operación atendida**, en un archivo del disco local del
nodo donde corre. Formato idéntico en las dos implementaciones:

```
2026-09-08T14:03:22-03:00 | java@casa-agustina | POST /personas | 201 | id=7
```

| Campo | Contenido |
| :--- | :--- |
| 1 | Timestamp ISO-8601 con offset, precisión de segundos |
| 2 | `<app>@<CASA>`, de las variables de entorno |
| 3 | Método y ruta |
| 4 | Código HTTP de la respuesta |
| 5 | `id=<n>` si la operación involucra una persona; `-` en cualquier otro caso |

El objetivo es poder cruzar el log del balanceador con el de cada nodo y auditar una operación
puntual: el balanceador registra a quién derivó, el nodo registra qué hizo.

---

## 9. Fuera del contrato

Extensiones que hoy implementa una sola de las dos apps. **No son obligatorias.** Mientras el
balanceador reparta entre implementaciones distintas, una extensión activa hace que el servicio
se comporte distinto según quién atendió — que es exactamente lo que este documento evita. Por eso
van apagadas salvo que se acuerde incorporarlas, y ese acuerdo sube la versión del contrato.

| Extensión | De quién | Estado |
| :--- | :--- | :--- |
| `checksum` (SHA-256 del fuente) en `/` y `/health` | App Python | **Fuera del contrato — decidido.** Apagada por defecto |
| Rate limiting por IP con ventana deslizante | App Python | A definir con el grupo |

### `checksum`

Agrega un campo extra a las respuestas de `/` y `/health` con el SHA-256 del fuente en ejecución,
para verificar qué versión exacta del código está corriendo cada réplica.

**Queda fuera del contrato.** El campo sólo tendría sentido si las dos apps lo expusieran, y el
hash del fuente no es comparable entre un `.py` y un `.jar`: no habría forma de contrastar dos
valores, sólo de mirar cada uno por separado. Sigue disponible como herramienta de diagnóstico de
App Python, detrás de una variable:

```bash
python3 Clase01/app.py 8080                  # respuesta del contrato
TP_CHECKSUM=on python3 Clase01/app.py 8080   # con el checksum expuesto
```

### Rate limiting

Limita las peticiones por IP en una ventana deslizante y devuelve `429` al superar el límite.

| Parámetro | Variable | Default |
| :--- | :--- | :--- |
| Máximo de peticiones | `TP_RATE_LIMIT_MAX` | `100` |
| Ventana en segundos | `TP_RATE_LIMIT_WINDOW` | `60` |
| Contador compartido | `TP_REDIS_URL` | vacío → contador local por réplica |

Si el grupo decide incorporarlo al contrato, hay tres cosas que tienen que quedar cerradas al
mismo tiempo:

1. **App Java lo implementa también.** Si no, el límite se aplica sólo a una parte de las réplicas
   y el comportamiento del servicio depende de a quién derivó el balanceador.
2. **El contador va en Redis.** En memoria, cada réplica lleva su propia cuenta y el límite
   efectivo se multiplica por la cantidad de réplicas.
3. **El cliente se identifica por `X-Forwarded-For`** (§10). Detrás del balanceador, todas las
   peticiones llegan con la misma IP de origen: sin esa cabecera el límite se aplicaría a todos
   los clientes en conjunto, como si fueran uno solo.

Y una consecuencia para Plataforma: con `100/60s`, el health check no puede consultar más de una
vez por segundo por instancia. Si chequea más seguido recibe `429`, interpreta que la instancia
está caída y **saca de rotación réplicas sanas**.

---

## 10. Requisitos para el equipo Plataforma

No son parte del contrato entre las apps, pero el balanceador depende de esto:

1. **Propagar `X-Forwarded-For`** con la IP del cliente original. Hoy ninguna ruta del contrato la
   usa, pero sin esa cabecera las apps no tienen forma de saber quién es el cliente real: todo
   llega con la IP del balanceador. Es la condición para cualquier control o registro por cliente
   —el rate limiting de §9 entre ellos— y agregarla después es más caro que dejarla desde el
   principio.
2. **Definir qué considera "instancia sana".** Si alcanza con `200` en `/health` o si el chequeo
   también tiene que verificar la base. Una instancia puede responder `200` en `/health` y `503`
   en `/personas`: un health check superficial no la saca de rotación.

---

## 11. Registro de versiones

| Versión | Fecha | Cambios |
| :--- | :--- | :--- |
| 1.0 | 06/09/2026 | Contrato inicial. Resuelve las ocho divergencias detectadas entre las dos implementaciones y agrega `/personas`, bitácora y rate limiting. |
| 1.1 | 06/09/2026 | El rate limiting sale del contrato y pasa a §9 como extensión, pendiente de acuerdo con el grupo. Se agrega el `503` de `/personas` cuando la base no responde. |
| 1.2 | 06/09/2026 | `equipo` pasa a lista de objetos con `nombre`, `apellido` y `legajo` (pedido de la corrección de la Clase 1). `mensaje` se fija como texto plano. El `checksum` queda fuera del contrato de forma definitiva. |
| 1.3 | 06/09/2026 | `/personas` gana las reglas de validación, el orden en que se aplican, tres códigos de error nuevos (`cuerpo JSON inválido`, `legajo fuera de rango`, `nombre inválido`) y una matriz de casos que sirve de batería de pruebas. |
