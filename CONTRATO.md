# Contrato de servicio — App Java ↔ App Python

**v2.2 · gRPC + Protobuf** — lo que las dos implementaciones tienen que responder **igual** para
ser intercambiables detrás del balanceador.

El esquema formal está en **[`contrato.proto`](contrato.proto)**. Acá va lo que el `.proto` no
puede expresar: validación, orden de los chequeos y semántica de los errores.

> Cada punto está decidido. Si algo hay que cambiar, se cambia acá y sube la versión — no se
> resuelve por chat ni se asume distinto de cada lado.

---

## 1. Reglas generales

| Regla | Valor |
| :--- | :--- |
| Transporte | **gRPC sobre HTTP/2** · Protobuf 3 |
| Paquete / servicio | `sdypp` / `sdypp.Servicio` |
| Canal | **Inseguro** (sin TLS): el cifrado lo pone Tailscale por debajo |
| Puerto | Primer argumento de CLI; si no, `PORT`; default **8080** |
| Identidad | `HOST_NAME` (instancia) y `CASA` (nodo), por variable de entorno |
| Zona horaria | **`America/Argentina/Buenos_Aires`** en las dos implementaciones |

---

## 2. Los cinco RPC

### `Identidad` → `Instancia`

| Campo | Tipo | Detalle |
| :--- | :--- | :--- |
| `app` | string | `"python"` o `"java"`. Permite ver qué implementación atendió. |
| `lenguaje` | string | Texto libre, informativo. |
| `equipo` | repeated Integrante | `nombre`, `apellido`, `legajo` por persona. |
| `version` | int32 | Lo que cambia en cada deploy. |
| `mensaje` | string | Texto plano. |
| `host` | string | Valor de `HOST_NAME`. |
| `arrancado` | string | ISO-8601 con offset, **precisión de segundos, sin fracción**. |

### `Salud` → `EstadoSalud`

Devuelve `status: SANO`, `app` y `version`. `status` es un **enumerado**, no un string: con texto
libre una implementación manda `"ok"` y la otra `"OK"`. El valor `0` (`ESTADO_NO_ESPECIFICADO`) es
obligatorio en proto3 y es el que llega si el campo no se manda, así que **no** significa sano.

> **Además hay que exponer `grpc.health.v1.Health`**, el estándar: es lo que consultan el
> `HEALTHCHECK` del contenedor y el balanceador. `Salud` lleva `app` y `version`, que el estándar
> no tiene. Se implementan los dos.

### `Echo` → `PongRespuesta`

Devuelve `pong` con el valor recibido, más `servido_por` y `version`.
Si `ping` viene vacío: `INVALID_ARGUMENT` · `se requiere el campo ping`.

### `ListarPersonas` → `ListaPersonas`

`servido_por` y las personas **ordenadas por `id` ascendente**. Sin personas devuelve lista vacía,
no un error. Si la base no responde: `UNAVAILABLE` · `base de datos no disponible`.

### `CrearPersona` → `RespuestaPersona`

| Situación | Código | Mensaje |
| :--- | :--- | :--- |
| Alta correcta | `OK` | — |
| `nombre` vacío o sólo espacios | `INVALID_ARGUMENT` | `se requieren los campos nombre y legajo` |
| `legajo` fuera de `1 … 2147483647` | `INVALID_ARGUMENT` | `legajo fuera de rango` |
| `nombre` de más de 120 caracteres | `INVALID_ARGUMENT` | `nombre inválido` |
| `legajo` ya registrado | `ALREADY_EXISTS` | `el legajo ya está registrado` |
| La base no responde | `UNAVAILABLE` | `base de datos no disponible` |

---

## 3. Validación: reglas y orden

Las dos apps validan **igual** y en **este orden**. El orden es contrato: ante un mensaje con dos
problemas a la vez, las dos tienen que devolver el mismo error y no cada una el que detectó primero.

1. **`nombre` presente**, después del trim. En proto3 esto cubre también el caso de no mandarlo.
2. **`legajo` en `1 … 2147483647`.** El `0` cae acá, y en proto3 el `0` es también lo que llega
   cuando el campo no se manda: los dos casos dan el mismo error, que es lo que se quiere.
3. **`nombre` de 1 a 120 caracteres**, sobre el valor ya trimeado.
4. **`legajo` no registrado**, o `ALREADY_EXISTS`.

| Regla | Decisión |
| :--- | :--- |
| Espacios en `nombre` | **Trim** de los extremos. Los internos se preservan: no se colapsan. |
| Largo | Caracteres sobre el valor trimeado. |
| `nombre` duplicado | **Permitido.** Lo único único es el `legajo`. |

### Matriz de verificación

| `NuevaPersona` | Esperado |
| :--- | :--- |
| `nombre:"Ada Lovelace" legajo:100200` | `OK` |
| `nombre:"  Ada Lovelace  " legajo:100201` | `OK`, guardado como `"Ada Lovelace"` |
| `nombre:"Ada  Lovelace" legajo:100202` | `OK`, los espacios internos se conservan |
| `nombre:"Ada" legajo:100200` (repetido) | `ALREADY_EXISTS` |
| `nombre:""` / `nombre:"   "` / sin `nombre` | `INVALID_ARGUMENT` campos requeridos |
| sin `legajo` / `legajo:0` / `legajo:-5` | `INVALID_ARGUMENT` legajo fuera de rango |
| `nombre:<121 caracteres>` | `INVALID_ARGUMENT` nombre inválido |
| `nombre:<120 caracteres>` | `OK` |
| cualquiera, con Redis caído | `UNAVAILABLE` |

> **El tipado eliminó trece casos borde** que la v1.3 validaba a mano: legajo como string, decimal,
> booleano, en notación exponencial, mayor a int32, cuerpo no-JSON, cuerpo vacío… Con Protobuf el
> stub del cliente los rechaza antes de salir a la red. Es el argumento más fuerte a favor de gRPC
> y va en el informe.

---

## 4. El estado: Redis en un contenedor

Las dos apps leen y escriben sobre **la misma base**. El estado sale de las instancias, que quedan
*stateless* y por lo tanto reemplazables entre sí.

| Clave | Tipo | Contenido |
| :--- | :--- | :--- |
| `personas:seq` | string | Contador. `INCR` devuelve el `id` de la próxima persona. |
| `persona:<id>` | hash | `nombre`, `legajo` |
| `personas:index` | sorted set | miembro y score `<id>` — mantiene el orden de listado |
| `legajo:<legajo>` | string | `<id>`. Se crea de forma atómica para detectar duplicados. |

**El esquema de claves es contrato tanto como el `.proto`.** Si una implementación guardara la
persona bajo otra clave, las dos apps escribirían en la misma base sin encontrar lo del otro.

El alta va en **un script Lua**: chequeo de duplicado, `INCR`, `HSET`, `ZADD` y `SET` en una sola
operación atómica. Entre comprobar que el legajo no está y escribirlo, otra réplica puede colarse
con el mismo; y entre pedir el `id` y usarlo, otra puede pedir el mismo.

La URL llega por **`TP_REDIS_URL`**. Lleva la contraseña adentro: **no se versiona**, va en el
`.env` de cada nodo.

---

## 5. Bitácora a disco

Una línea por RPC atendido, en el disco local del nodo — no en la base.

```
2026-09-08T14:03:22-03:00 | java@casa-agustina | CrearPersona | OK | id=7
```

| Campo | Contenido |
| :--- | :--- |
| 1 | Timestamp ISO-8601 con offset, precisión de segundos |
| 2 | `<app>@<CASA>` |
| 3 | Nombre del RPC |
| 4 | Código gRPC de la respuesta |
| 5 | `id=<n>` si involucra una persona; `-` si no |

**Un archivo por réplica** (`bitacora-<HOST_NAME>.log`): dos réplicas en el mismo nodo escribiendo
el mismo archivo no se pueden distinguir después, y distinguirlas es lo que la auditoría de la
Etapa 2 tiene que demostrar. El objetivo es cruzarlo con el log del balanceador: él registra a
quién derivó, el nodo registra qué hizo.

---

## 6. Contenedores

| Regla | Valor |
| :--- | :--- |
| Puerto dentro del contenedor | `8080` |
| Variables obligatorias | `HOST_NAME`, `CASA`, `TP_REDIS_URL` |
| Usuario | **no-root** |
| `HEALTHCHECK` | contra `grpc.health.v1.Health`, no con `curl` (no hay HTTP que consultar) |
| Apagado | `SIGTERM` → `NOT_SERVING` → `server.stop(grace)`, sin morir de golpe |

El plazo de gracia del contenedor tiene que ser **mayor que el `grace` del servidor**, o llega el
`SIGKILL` en medio del drenado y el graceful shutdown no sirve de nada.

---

## 7. Requisitos para el equipo Plataforma

Condiciones sin las cuales el balanceador no puede reenviar tráfico.

1. **Tiene que hablar HTTP/2.** gRPC no viaja sobre HTTP/1.1: un proxy que lee la request y la
   reenvía con una librería HTTP/1.1 **no funciona**. Dos salidas:
   - **Proxy TCP (L4):** reenviar bytes sin entender el protocolo. Camino corto, pero pierde la
     capacidad de ver qué RPC pasó — y con eso se cae el requisito de loguear a quién derivó cada
     operación.
   - **Proxy gRPC real:** entender HTTP/2 y multiplexar streams. Es bastante más que las "menos de
     cien líneas" que el enunciado estima.
2. **El health check llama a `grpc.health.v1.Health`**, no hace un GET.
3. **Una conexión gRPC es persistente y multiplexada.** No hay una conexión por request: el cliente
   abre un canal y lo reusa. Si el balanceador reparte **por conexión**, un cliente queda pegado a
   una réplica para siempre y el reparto no se ve en la demo.
4. **La IP del cliente y el id de correlación viajan como metadata gRPC**, no como cabeceras HTTP:
   `x-forwarded-for` y `x-request-id` en minúscula, que es como gRPC normaliza las claves.
5. **ngrok:** el túnel HTTP del plan free no sirve para gRPC sin TLS end-to-end. Hay que exponer el
   balanceador por el **túnel TCP**.
6. **El verificador del equipo cruzado necesita un cliente gRPC.** Ya no puede ser un `curl` en un
   `while`: hay que darles los stubs o un binario.

---

## 8. Versiones

| | Cambios |
| :--- | :--- |
| 1.0 – 1.3 | Contrato sobre HTTP/JSON: ocho divergencias resueltas, `equipo` estructurado, validación de personas y matriz de casos borde. |
| **2.0** | **Cambio incompatible: HTTP/JSON → gRPC sobre HTTP/2 con Protobuf.** Códigos HTTP → códigos gRPC. Trece casos borde desaparecen por el tipado. Se agregan health estándar, contenedores y los requisitos de §7. |
| 2.1 | Salen el RPC `Lenta`, el checksum y el rate limiting: el grupo decidió no usarlos. |
| **2.2** | Un mensaje de pedido propio por método (`IdentidadPedido`, `SaludPedido`, `ListarPersonasPedido`) en vez de uno compartido. `EstadoSalud.status` pasa de string a **enumerado**. |
