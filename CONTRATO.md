# Contrato de servicio — App Java ↔ App Python

**Versión 2.2 — 06/09/2026 · gRPC + Protobuf**

Especificación de lo que las dos implementaciones tienen que responder **igual**, para que sean
intercambiables detrás del balanceador del equipo Plataforma.

> **Esto no es una lista de propuestas.** Cada punto está decidido. Si algo hay que cambiar, se
> cambia sobre este documento y sube la versión — no se resuelve por chat ni se asume distinto de
> cada lado.

> ⚠️ **La v2.0 es un cambio incompatible.** El servicio deja de hablar HTTP/JSON y pasa a
> **gRPC sobre HTTP/2 con Protobuf**. Un cliente de la v1.3 no puede hablar con un servidor v2.0.
> Las consecuencias para los otros dos equipos están en §7 y §10: no son un detalle de
> implementación, son trabajo que hay que negociar antes de escribir código.

**Estado de implementación:** App Python ✅ al día con la v2.2 · App Java ⬜ pendiente (ver §7)

El esquema formal vive en **[`contrato.proto`](contrato.proto)**. Este documento especifica lo que
el `.proto` no puede expresar: validación, orden de los chequeos, semántica de los errores y qué
hace cada implementación cuando algo falla.

---

## 1. Reglas generales

| Regla | Valor |
| :--- | :--- |
| Transporte | **gRPC sobre HTTP/2** |
| Serialización | **Protobuf 3** (`contrato.proto`) |
| Paquete proto | `sdypp` |
| Servicio | `sdypp.Servicio` |
| Canal | **Inseguro** (sin TLS): el cifrado lo pone Tailscale por debajo (ver §10) |
| Puerto | Primer argumento de línea de comandos; si no, variable `PORT`; default **8080** |
| Identidad de la instancia | Variable de entorno **`HOST_NAME`** |
| Nodo donde corre | Variable de entorno **`CASA`** |
| Zona horaria | **`America/Argentina/Buenos_Aires`** en las dos implementaciones |

> **La zona horaria es contrato.** Un contenedor sin `tzdata` corre en UTC y
> devuelve `arrancado` con offset `+00:00`, mientras una instancia fuera de
> contenedor devuelve `-03:00`: dos réplicas del mismo servicio informando horas
> distintas. Y como las bitácoras de las tres casas se cruzan entre sí y con la del
> balanceador, la diferencia deja de ser cosmética. En la imagen se instala
> `tzdata` y se fija `TZ`.

### Qué cambia respecto de la v1.3, y por qué importa

No es sólo "otro formato". Tres cosas dejan de funcionar como antes:

1. **Los nombres de los campos dejan de ser contrato; los números lo son.** En JSON, renombrar
   `servidoPor` rompía a todos los clientes. En Protobuf lo que viaja es el número de campo, así
   que renombrar es gratis y **cambiar un número es catastrófico**. Nunca se reutiliza un número
   liberado.
2. **Ausente y vacío dejan de distinguirse.** En proto3 un `string` que no se manda llega como
   `""` y un `int32` como `0`. Se puede recuperar la distinción marcando el campo como `optional`
   —desde proto3 3.15 eso habilita la presencia explícita—, pero **el contrato elige no hacerlo**:
   la v1.3 ya trataba igual al campo ausente y al vacío, así que la diferencia no le servía a
   nadie. Media matriz de casos borde desaparece por esta decisión (§6).
3. **El tipo hace cumplir parte del contrato.** `legajo` es `int32`: un string numérico o un
   decimal ya no llegan al servidor, los rechaza el stub. Lo que antes era una regla de validación
   ahora es un error de compilación del cliente.

---

## 2. `Identidad` — identidad de la instancia

`rpc Identidad(IdentidadPedido) returns (Instancia)`

| Campo | Tipo | Detalle |
| :--- | :--- | :--- |
| `app` | string | `"python"` o `"java"`. Es lo que permite ver qué implementación atendió. |
| `lenguaje` | string | Texto libre, informativo. |
| `equipo` | repeated Integrante | Un `Integrante` por persona: `nombre`, `apellido`, `legajo`. |
| `version` | int32 | Lo que cambia en cada deploy. |
| `mensaje` | string | Texto plano. Lo que cambia en cada deploy. |
| `host` | string | Valor de `HOST_NAME`. |
| `arrancado` | string | ISO-8601 con offset, **precisión de segundos, sin fracción**. |

> **`arrancado` sigue siendo string.** Protobuf tiene `google.protobuf.Timestamp`, que sería lo
> correcto, pero obliga a importar el well-known type en las dos implementaciones y a decidir cómo
> se muestra. Se deja como string ISO-8601 para que el valor sea idéntico al de la v1.3 y la
> comparación entre las dos apps siga siendo a simple vista. **Java:**
> `OffsetDateTime.now().truncatedTo(ChronoUnit.SECONDS).toString()`.

---

## 3. `Salud` — chequeo de salud

`rpc Salud(SaludPedido) returns (EstadoSalud)` → `status: SANO`, `app`, `version`.

`status` es un **enumerado**, no un string. Con texto libre una implementación puede mandar `"ok"` y
la otra `"OK"`, que es justo la clase de divergencia que este documento evita. El valor `0` del
enum (`ESTADO_NO_ESPECIFICADO`) es obligatorio en proto3 y es el que llega si el campo no se manda,
así que **no** significa "sano".

Cualquier respuesta que no sea `OK` significa que la instancia no está sana.

> **Además hay que exponer `grpc.health.v1.Health`**, el health checking estándar de gRPC. Es lo
> que entienden las herramientas (`grpc_health_probe`, el `HEALTHCHECK` del contenedor, los
> balanceadores). `Salud` es el equivalente del `/health` de la v1.3 y lleva `app` y `version`,
> que el estándar no tiene; el estándar es lo que consultan las máquinas. Se implementan los dos.

---

## 4. `Echo`

`rpc Echo(PingPedido) returns (PongRespuesta)`

Devuelve `pong` con el valor recibido, más `servido_por` y `version`.

**Si `ping` viene vacío:** `INVALID_ARGUMENT` con el mensaje `se requiere el campo ping`.

> Los dos casos llegan como `""` mientras el campo no se declare `optional`. El contrato se apoya
> en eso a propósito y los trata igual, que es lo que la v1.3 ya hacía por decisión propia.

---

## 5. `ListarPersonas` — estado compartido

`rpc ListarPersonas(ListarPersonasPedido) returns (ListaPersonas)`

Devuelve `servido_por` y las personas **ordenadas por `id` ascendente**. Sin personas cargadas
devuelve la lista vacía, no un error.

**Si la base no responde:** `UNAVAILABLE` con el mensaje `base de datos no disponible`.

### Motor: **Redis**, en un contenedor

Las dos apps leen y escriben sobre **la misma base**. El estado sale de las instancias, que quedan
*stateless* y por lo tanto reemplazables entre sí.

`INCR` es atómico: garantiza que dos réplicas dando de alta al mismo tiempo no se pisen los `id`
sin necesidad de coordinarse. Sin esa atomicidad haría falta exclusión mutua entre casas, que es un
problema bastante más grande que el que resuelve.

| Clave | Tipo | Contenido |
| :--- | :--- | :--- |
| `personas:seq` | string | Contador. `INCR` devuelve el `id` de la próxima persona. |
| `persona:<id>` | hash | `nombre`, `legajo` |
| `personas:index` | sorted set | miembro `<id>`, score `<id>` — mantiene el orden de listado |
| `legajo:<legajo>` | string | `<id>`. Se crea de forma atómica para detectar duplicados. |

**El esquema de claves es contrato tanto como el `.proto`.** Si una implementación guardara la
misma persona bajo otra clave o con otra estructura, las dos apps escribirían en la misma base sin
encontrar lo del otro.

La URL de conexión llega por **`TP_REDIS_URL`**. Lleva la contraseña adentro, así que **no se
versiona**: va en el `.env` de cada nodo, que está en el `.gitignore`.

---

## 6. `CrearPersona` — alta

`rpc CrearPersona(NuevaPersona) returns (RespuestaPersona)`

Devuelve `servido_por` y la `Persona` creada. **El `id` lo asigna la base**, nunca la app.

| Situación | Código gRPC | Mensaje |
| :--- | :--- | :--- |
| Alta correcta | `OK` | — |
| `nombre` vacío o sólo espacios | `INVALID_ARGUMENT` | `se requieren los campos nombre y legajo` |
| `legajo` fuera de `1 … 2147483647` | `INVALID_ARGUMENT` | `legajo fuera de rango` |
| `nombre` de más de 120 caracteres | `INVALID_ARGUMENT` | `nombre inválido` |
| `legajo` ya registrado | `ALREADY_EXISTS` | `el legajo ya está registrado` |
| La base no responde | `UNAVAILABLE` | `base de datos no disponible` |

### Validación: reglas y orden

Las dos apps validan **igual** y en **este orden**. El orden es parte del contrato: ante un mensaje
con dos problemas a la vez, las dos tienen que devolver el mismo error y no cada una el que detectó
primero.

1. **`nombre` presente**, después del trim. Vacío o sólo espacios → `INVALID_ARGUMENT`. En proto3
   esto cubre también el caso de no mandarlo.
2. **`legajo` en rango `1 … 2147483647`.** El `0` cae acá, y en proto3 el `0` es también lo que
   llega cuando el campo no se manda: los dos casos dan el mismo error, que es lo que se quiere.
3. **`nombre` de 1 a 120 caracteres**, medido sobre el valor ya trimeado.
4. **`legajo` no registrado**, o `ALREADY_EXISTS`.

| Regla | Decisión |
| :--- | :--- |
| Espacios en `nombre` | **Trim** de los extremos. Los internos se preservan: **no** se colapsan. |
| Largo de `nombre` | Caracteres sobre el valor trimeado (`len()` en Python, `String.length()` en Java). |
| Campos desconocidos | Protobuf los ignora y los preserva. No hay nada que decidir. |
| `nombre` duplicado | **Permitido.** Lo único único es el `legajo`. |

> **El tope de `int32` ahora lo impone el tipo.** En la v1.3 había que validar a mano que el legajo
> entrara en un `int` de Java; con Protobuf, `int32` es el tipo del campo y el desborde lo rechaza
> el stub del cliente antes de salir a la red. La validación de rango queda igual para cubrir el
> `0` y los negativos.

### Matriz de verificación

Se achicó a la mitad respecto de la v1.3: **el tipado eliminó los casos que antes había que
validar a mano**. Los que quedan son los que el tipo no puede expresar.

| `NuevaPersona` | Esperado |
| :--- | :--- |
| `nombre:"Ada Lovelace" legajo:100200` | `OK` |
| `nombre:"  Ada Lovelace  " legajo:100201` | `OK`, guardado como `"Ada Lovelace"` |
| `nombre:"Ada  Lovelace" legajo:100202` | `OK`, los dos espacios internos se conservan |
| `nombre:"Ada" legajo:100200` (repetido) | `ALREADY_EXISTS` |
| `nombre:"" legajo:100206` | `INVALID_ARGUMENT` campos requeridos |
| `nombre:"   " legajo:100207` | `INVALID_ARGUMENT` campos requeridos |
| *(sin `nombre`)* `legajo:100205` | `INVALID_ARGUMENT` campos requeridos |
| `nombre:"Ada"` *(sin `legajo`)* | `INVALID_ARGUMENT` legajo fuera de rango |
| `nombre:"Ada" legajo:0` | `INVALID_ARGUMENT` legajo fuera de rango |
| `nombre:"Ada" legajo:-5` | `INVALID_ARGUMENT` legajo fuera de rango |
| `nombre:<121 caracteres> legajo:100212` | `INVALID_ARGUMENT` nombre inválido |
| `nombre:<120 caracteres> legajo:100213` | `OK` |
| cualquiera, con Redis caído | `UNAVAILABLE` |

**Casos de la v1.3 que ya no existen:** legajo como string, legajo decimal, legajo booleano,
notación exponencial, legajo mayor a int32, `nombre` no string, clave con mayúscula distinta,
cuerpo no-JSON, cuerpo que es un array, cuerpo vacío. Trece casos borrados por el tipado — es el
argumento más fuerte a favor de este cambio, y va en el informe.

---

## 7. Qué tiene que cambiar cada equipo

### App Java ⬜

| # | Cambio |
| :--- | :--- |
| 1 | Generar los stubs desde `contrato.proto` (`protoc` + `grpc-java`) |
| 2 | Reemplazar el servidor HTTP por un servidor gRPC |
| 3 | Los cinco RPC de §2 a §6, con un mensaje de pedido propio por método |
| 4 | `grpc.health.v1.Health` además de `Salud` (§3) |
| 5 | `/personas` sobre Redis con el esquema de §5 y la validación de §6 |
| 6 | Bitácora a disco (§8) |
| 7 | Contenedor (§9) |

**Es una reescritura, no un ajuste.** El servidor HTTP no se reusa.

### App Python ⬜

| # | Cambio | Estado |
| :--- | :--- | :--- |
| 1 | Stubs desde `contrato.proto` | ⬜ |
| 2 | Servidor gRPC en lugar del HTTP | ⬜ |
| 3 | Los cinco RPC | ✅ |
| 4 | `grpc.health.v1.Health` | ⬜ |
| 5 | Validación y repositorio | ✅ se reusan tal cual: no dependen del transporte |
| 6 | Bitácora | ⬜ |
| 7 | Contenedor | ⬜ |

---

## 8. Bitácora a disco

Cada instancia escribe **una línea por RPC atendido**, en el disco local del nodo donde corre —
no en la base. Formato idéntico en las dos implementaciones:

```
2026-09-08T14:03:22-03:00 | java@casa-agustina | CrearPersona | OK | id=7
```

| Campo | Contenido |
| :--- | :--- |
| 1 | Timestamp ISO-8601 con offset, precisión de segundos |
| 2 | `<app>@<CASA>`, de las variables de entorno |
| 3 | **Nombre del RPC** (antes era método y ruta) |
| 4 | **Código gRPC** de la respuesta (`OK`, `INVALID_ARGUMENT`, …) |
| 5 | `id=<n>` si la operación involucra una persona; `-` en cualquier otro caso |

**Un archivo por réplica** (`bitacora-<HOST_NAME>.log`): dos réplicas en el mismo nodo escribiendo
el mismo archivo no se pueden distinguir después, y distinguirlas es justo lo que la auditoría de
la Etapa 2 tiene que demostrar.

El objetivo es cruzar el log del balanceador con el de cada nodo: él registra a quién derivó, el
nodo registra qué hizo.

---

## 9. El servicio corre en contenedores

Cada réplica es un contenedor. La base es otro.

| Regla | Valor |
| :--- | :--- |
| Puerto dentro del contenedor | `8080` |
| Variables obligatorias | `HOST_NAME`, `CASA`, `TP_REDIS_URL` |
| Usuario | **no-root** |
| `HEALTHCHECK` | contra `grpc.health.v1.Health`, no con `curl` (no hay HTTP que consultar) |
| Apagado | el contenedor recibe `SIGTERM`; el proceso tiene que hacer `server.stop(grace)` y no morir de golpe |

El `stop_grace_period` del contenedor tiene que ser **mayor que el `grace` del servidor**, o Docker
manda `SIGKILL` en medio del drenado y el graceful shutdown no sirve de nada.

---

## 10. Requisitos para el equipo Plataforma

⚠️ **Estos requisitos cambiaron por completo con la v2.0.** No son ajustes: son condiciones sin
las cuales el balanceador no puede reenviar tráfico.

1. **El balanceador tiene que hablar HTTP/2.** gRPC no viaja sobre HTTP/1.1. Un proxy que lee una
   request, elige backend y la reenvía con una librería HTTP/1.1 **no funciona con gRPC**. Las
   salidas son dos:
   - **Proxy de nivel 4 (TCP):** reenviar bytes sin entender el protocolo. Es el camino corto, pero
     pierde la capacidad de ver qué RPC pasó — y con eso se cae el requisito del enunciado de que
     el balanceador loguee a quién derivó cada operación con detalle.
   - **Proxy gRPC real:** entender HTTP/2 y multiplexar streams. Es bastante más que las "menos de
     cien líneas" que el enunciado estima para el balanceador.
2. **El health check tiene que llamar a `grpc.health.v1.Health`**, no hacer un GET.
3. **Una conexión gRPC es persistente y multiplexada.** No hay una conexión por request: el cliente
   abre un canal y lo reusa. El balanceo por request deja de ser gratis — si reparte por conexión,
   un cliente queda pegado a una réplica para siempre y el reparto no se ve en la demo.
4. **La IP del cliente y el id de correlación viajan como metadata gRPC**, no como cabeceras HTTP:
   `x-forwarded-for` y `x-request-id` en minúscula, que es como gRPC normaliza las claves.
5. **ngrok:** el túnel HTTP del plan free no sirve para gRPC sin TLS end-to-end. Hay que exponer el
   balanceador por el **túnel TCP**, y el cliente conectarse a ese host:puerto.
6. **El verificador del equipo cruzado necesita un cliente gRPC.** Ya no puede ser un `curl` en un
   `while`: hay que darles los stubs o un binario. Esto hay que avisarlo antes de la demo.

---

## 11. Registro de versiones

| Versión | Fecha | Cambios |
| :--- | :--- | :--- |
| 1.0 | 06/09/2026 | Contrato inicial sobre HTTP/JSON. Resuelve las ocho divergencias detectadas entre las dos implementaciones y agrega `/personas`, bitácora y rate limiting. |
| 1.1 | 06/09/2026 | El rate limiting sale del contrato y pasa a extensión. Se agrega el `503` de `/personas`. |
| 1.2 | 06/09/2026 | `equipo` pasa a lista de objetos con `nombre`, `apellido` y `legajo`. `mensaje` se fija como texto plano. El `checksum` queda fuera del contrato. |
| 1.3 | 06/09/2026 | `/personas` gana las reglas de validación, el orden en que se aplican y una matriz de casos borde. |
| 2.0 | 06/09/2026 | **Cambio incompatible: el transporte pasa de HTTP/JSON a gRPC sobre HTTP/2 con Protobuf.** El esquema formal se muda a `contrato.proto`. Los códigos HTTP se reemplazan por códigos de estado gRPC. Trece casos borde desaparecen porque el tipado los hace imposibles. Se agrega `grpc.health.v1.Health`, el despliegue en contenedores (§9) y los requisitos nuevos de Plataforma (§10). |
| 2.1 | 06/09/2026 | **Se sacan del contrato el RPC `Lenta` y las dos extensiones de App Python (`checksum` y rate limiting): el grupo decidió no usarlos.** El servicio queda en cinco RPC. El graceful shutdown sigue implementado, pero ya no hay un RPC lento con que evidenciarlo. |
| **2.2** | **06/09/2026** | `Vacio` se reemplaza por un mensaje de pedido propio por método (`IdentidadPedido`, `SaludPedido`, `ListarPersonasPedido`): con uno compartido, el día que un método necesite un campo nuevo se lo agregaría también a los otros dos. `EstadoSalud.status` pasa de string a **enumerado**. |

---
