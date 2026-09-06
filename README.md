# Sistemas Distribuidos y Programación Paralela (SDyPP) - Mini-Nube

Servidor HTTP liviano desarrollado en Python para la simulación de despliegues manuales, traspaso de mando y gestión de concurrencia sobre un puerto TCP compartido.

---

## 👥 Integrantes del Equipo
* **Tomás Resnik** — Legajo 190168
* **Mateo Nomico** — Legajo 168102
* **Salvador Baez** — Legajo 195157

---

## Comandos

Desde la raíz del repositorio:

```bash
python3 -m venv .venv

# Activar el entorno, según el sistema operativo:
source .venv/bin/activate          # Linux / macOS
# .venv\Scripts\Activate.ps1       # Windows (PowerShell)
# .venv\Scripts\activate.bat       # Windows (cmd)

pip install -r requirements.txt
python3 Clase01/app.py 8080
```

El servidor queda escuchando en `http://0.0.0.0:8080`, que es también el puerto por defecto si no
se pasa ninguno. Para levantar varias réplicas a la vez, pasarle a cada una un puerto distinto.

## 🚀 Endpoints de la Aplicación

| Método | Ruta | Descripción |
| :--- | :--- | :--- |
| `GET` | `/` | Retorna metadatos de la app (nombre, equipo, versión, timestamp de arranque y host). |
| `GET` | `/health` | Chequeo de salud del servicio (retorna `{"status": "ok"}`). |
| `POST` | `/echo` | Recibe JSON `{"ping": "mensaje"}` y responde `{"pong": "mensaje"}`. |
| `GET` | `/slow` | Endpoint simulador de peticiones en vuelo (sleep de 4s) para probar **Graceful Shutdown**. |

---

## 🏗️ Diagrama de Arquitectura

Tres casas, ninguna en la misma red, coordinadas por Meet/Discord. El equipo **Plataforma** monta
el servidor y reparte el acceso; **App Java** y **App Python** compiten por el mismo puerto de
producción, que es el recurso compartido en exclusión mutua.

El servidor no es una máquina expuesta a internet: es un **contenedor Docker con Ubuntu 24.04
dentro de WSL2**, en una PC hogareña detrás de NAT. Plataforma la hace alcanzable con **dos
túneles de ngrok** — uno publica el HTTP de producción, el otro publica el SSH de deploy.

```mermaid
flowchart TB
    subgraph NUBE["Internet - ngrok, region sa, cuenta free de Plataforma"]
        T1{{"TUNEL HTTP - dominio estatico<br/>publica el puerto de produccion<br/>vuelve igual tras cada reinicio"}}
        T2{{"TUNEL TCP - address aleatorio<br/>publica el SSH de deploy<br/>CAMBIA en cada arranque"}}
    end

    subgraph PLAT["Casa Plataforma - PC Windows con WSL2 - el cloud provider"]
        AG["2 agentes ngrok<br/>su inspector web guarda request y response<br/>de las dos apps, en texto plano"]
        subgraph CT["Contenedor Docker - Ubuntu 24.04"]
            APP["La app que gano el puerto<br/>java -jar tp1.jar 80<br/>o python3 app-python.py 80"]
            PORT{{"PUERTO 80 DEL CONTENEDOR<br/>RECURSO EN EXCLUSION MUTUA<br/>lo escucha UN solo proceso"}}
            SSHD["sshd del contenedor<br/>cuenta de deploy COMPARTIDA<br/>mismo directorio para los dos equipos"]
            APP --> PORT
        end
        AG -->|"localhost 8080 al 80 del contenedor"| PORT
        AG -->|"localhost 2222 al 22 del contenedor"| SSHD
    end

    T1 -.-> AG
    T2 -.-> AG

    subgraph JAVA["Casa App Java"]
        JAR["Build con mvn<br/>produce el .jar"]
    end

    subgraph PY["Casa App Python - este repo"]
        SRC["Clase01/app.py<br/>Build: venv + pip install -r requirements.txt<br/>no compila, pero resuelve dependencias"]
    end

    JAR -->|"Ship: scp por el tunel TCP"| T2
    SRC -->|"Ship: scp por el tunel TCP"| T2

    VER(("Verify DESDE OTRA CASA<br/>GET / - GET /health - POST /echo"))
    VER -->|"curl con el header de ngrok"| T1

    style PORT fill:#f96,stroke:#333,stroke-width:3px,color:#111
    style T2 fill:#f7e3b5,stroke:#333,color:#111
    style T1 fill:#cfe8d4,stroke:#333,color:#111
```

**Qué expone cada nodo:**

- **Plataforma** expone dos cosas y ninguna es la máquina en sí: el **HTTP de producción** por el
  túnel de dominio estático, y el **acceso de deploy** (SSH) por el túnel TCP. Son los árbitros del
  recurso: si apagan un agente, el equipo que dependía de ese túnel queda afuera.
- **App Java / App Python** no exponen nada hacia afuera. Despliegan *sobre* el nodo de Plataforma,
  no corren servidor propio.
- **Recurso compartido y disputado**: el puerto `80` del contenedor (publicado como el `8080` del host). Un puerto TCP lo escucha un
  solo proceso a la vez, así que desplegar no es "instalar al lado": es un **traspaso**.

**Asimetría entre los dos túneles.** El de producción usa un dominio estático y sobrevive a los
reinicios; el de SSH recibe un address aleatorio y **cambia cada vez que Plataforma lo levanta**.
En la práctica eso significa que el canal de deploy se rompe solo y hay que pedir el puerto nuevo,
mientras que la URL que ve el mundo se mantiene.

**Lo que el diagrama deja ver sobre seguridad.** Todo el tráfico de las dos apps pasa por los
agentes de ngrok de Plataforma, cuyo inspector guarda request y response completos en texto plano.
Y el acceso de deploy es una **cuenta de sistema compartida**: no hay forma de distinguir quién
frenó qué proceso.

---

## 🔄 Diagrama de Flujo del Pipeline (Build → Ship → Stop → Start → Verify)

```mermaid
flowchart TD
    START(["Cambio trivial en local<br/>subir VERSION y cambiar mensaje"]) --> BUILD
    BUILD["1 - BUILD<br/>Java compila con mvn hasta el .jar<br/>Python arma el venv e instala requirements.txt<br/>los dos resuelven dependencias, uno ademas compila"] --> SHIP
    SHIP["2 - SHIP<br/>scp del archivo por el tunel TCP<br/>con nombre propio, sin pisar el .jar de Java"] --> TUNEL

    TUNEL{"Responde el tunel de deploy?"}
    TUNEL -->|"Connection refused"| CAIDO["El address TCP cambio o el agente esta abajo<br/>pedirle el puerto nuevo a Plataforma"]
    CAIDO --> SHIP
    TUNEL -->|"Si"| CHECK

    CHECK{"CONTENCION DEL PUERTO<br/>el 80 esta libre?"}
    CHECK -->|"No, lo tiene la otra app"| COLISION
    CHECK -->|"Si"| STARTP

    COLISION["COLISION PASIVA - se provoca a proposito en la demo<br/>arrancar sin frenar al anterior da Address already in use<br/>la app nueva NO toma el puerto y la vieja sigue sirviendo"] --> STOP

    STOP["3 - STOP coordinado<br/>identificar el PID que tiene el 80 y mandarle SIGTERM<br/>nunca kill -9: cortaria las peticiones en vuelo<br/>el graceful shutdown drena y recien ahi libera el puerto"] --> STARTP

    STARTP["4 - START<br/>levantar con nohup en segundo plano<br/>para que sobreviva al cierre de la sesion SSH"] --> VERIFY

    VERIFY{"5 - VERIFY, DESDE OTRA CASA<br/>curl a la URL publica con el header de ngrok"}
    VERIFY -->|"404 ERR_NGROK_3200"| E404["El tunel HTTP esta caido<br/>es del lado de Plataforma"]
    VERIFY -->|"502"| E502["El tunel vive pero nadie escucha en 80<br/>se cayo el Start"]
    E502 --> STARTP
    VERIFY -->|"Llega HTML en vez de JSON"| EHTML["Falta el header ngrok-skip-browser-warning<br/>es la pantalla de aviso del plan free"]
    EHTML --> VERIFY
    VERIFY -->|"200 con la app y la version nuevas"| OK(["Deploy verificado<br/>anotar el downtime entre el SIGTERM y el primer 200"])

    style CHECK fill:#f96,stroke:#333,stroke-width:3px,color:#111
    style COLISION fill:#f66,stroke:#333,stroke-width:2px,color:#111
```

Los comandos exactos de cada paso:

> **Datos de acceso.** El host y el puerto del túnel de deploy, el usuario y su contraseña **no se
> versionan**: los publica el equipo Plataforma por Discord y cambian cada vez que reinician el
> agente de ngrok. Los `<PLACEHOLDER>` de abajo se reemplazan al momento de desplegar.


```bash
# 1 · Build  (en la máquina de quien despliega)
python3 -m venv .venv
source .venv/bin/activate          # Linux / macOS
# .venv\Scripts\Activate.ps1       # Windows (PowerShell)
pip install -r requirements.txt

# 2 · Ship
scp -P <PUERTO_NGROK> Clase01/app.py requirements.txt <USUARIO>@<HOST_TCP_NGROK>:/home/<USUARIO>/

# 3 · Stop  (dentro del servidor)
ssh -p <PUERTO_NGROK> <USUARIO>@<HOST_TCP_NGROK>
ps aux | grep -E 'java|python3'
kill <PID>

# 4 · Start
nohup python3 ~/app-python.py 80 > ~/python.log 2>&1 &

# 5 · Verify  (desde otra casa)
curl -s -H "ngrok-skip-browser-warning: 1" https://<DOMINIO>.ngrok-free.dev/
curl -s -H "ngrok-skip-browser-warning: 1" https://<DOMINIO>.ngrok-free.dev/health
```

En la demo se provoca la **colisión pasiva a propósito** (arrancar sin coordinar el Stop), se la
reconoce por el `Address already in use`, y se muestra que la app anterior siguió sirviendo sin
enterarse. Recién después va el traspaso ordenado: Stop → Start → Verify.

---

## 🌟 Aportes Propios Justificados

> ℹ️ **Dos de los tres aportes entraron al contrato común** (ver [`CONTRATO.md`](CONTRATO.md)):
> el rate limiting y la ruta `/slow` ahora los implementan las dos apps, así que están siempre
> activos.
>
> El **checksum** quedó afuera, porque App Java no lo expone: con el balanceador repartiendo,
> el campo aparecería o no según quién atienda. Se enciende con una variable:
>
> ```bash
> python3 Clase01/app.py 8080                  # respuesta del contrato
> TP_CHECKSUM=on python3 Clase01/app.py 8080   # con el checksum expuesto
> ```
>
> Los ejemplos del Aporte 2 asumen `TP_CHECKSUM=on`.

---

### Aporte 1: Graceful Shutdown & Drenado de Conexiones

#### ¿En qué consiste?
Implementación de un manejador de señales a nivel del Sistema Operativo (`signal.SIGTERM` y `signal.SIGINT`) en el servidor `ThreadingHTTPServer`. 

Cuando el proceso recibe una señal de detención (`kill <PID>` o `Ctrl+C`):
1. **Deja de aceptar nuevas conexiones** cerrando el listener del socket TCP de inmediato (liberando el puerto para el siguiente despliegue).
2. **Drena las conexiones activas**: Espera a que las peticiones HTTP que ya se encontraban en curso (en ejecución en sus respectivas hebras) terminen de procesarse y responder al cliente.
3. **Apaga el proceso de forma limpia**. Quedan sockets en `TIME_WAIT` — es inevitable en TCP —
   pero el servidor activa `SO_REUSEADDR`, así que el siguiente deploy toma el 8080 igual,
   sin esperar el minuto de espera del kernel.

---

#### ¿Cómo probarlo?

1. **Iniciar el servidor en una terminal**:
   ```bash
   python3 Clase01/app.py 8080
   ```
   *Anotar el PID que imprime en pantalla (ejemplo: PID 13058).*

2. **Lanzar una petición en vuelo (lenta) desde otra terminal**:
   ```bash
   curl -i http://<IP_ADDRESS>:8080/slow
   ```
   *(Esta petición tarda 4 segundos en responder).*

3. **Inmediatamente (dentro de los 4 segundos), enviar la señal `SIGTERM` desde una tercera terminal**:
   ```bash
   # Reemplazar <PID> por el PID real de tu servidor
   kill -15 <PID>
   ```

4. **Resultado Observado**:
   * **En la terminal del `curl`**: La petición **NO se corta**. Espera sus 4 segundos y recibe un `200 OK` completo con el JSON de respuesta.
   * **En la terminal del servidor**: Se observa el log:
     ```text
     [ Graceful Shutdown ] Recibida señal SIGTERM (señal 15). Drenando conexiones y apagando servidor de forma limpia...
     [/slow] Petición lenta finalizada.
     [ Graceful Shutdown ] Puerto TCP liberado y servidor detenido exitosamente.
     ```
   * El puerto 8080 queda inmediatamente disponible para que otro integrante pueda levantar su app sin sufrir la colisión pasiva (`Address already in use`).

---

### Aporte 2: Hash de Integridad del Código en Tiempo de Ejecución (SHA-256)

#### ¿En qué consiste?
Al arrancar, la aplicación lee su propio archivo fuente (`app.py`), calcula su checksum criptográfico SHA-256 utilizando la librería estándar (`hashlib`) y lo expone en el campo `"checksum"` de las respuestas `GET /` y `GET /health`.

---

#### ¿Cómo probarlo?

1. **Consultar el hash remoto desplegado**:
   ```bash
   curl -s http://<IP_ADDRESS>:8080/health
   ```
   *Respuesta recibida:*
   ```json
   {
     "status": "ok",
     "app": "python",
     "version": 1,
     "checksum": "a3f8b1c4e5..."
   }
   ```

2. **Verificar localmente con sha256sum**:
   ```bash
   sha256sum Clase01/app.py
   ```
   Comprobar que el hash obtenido localmente coincide exactamente con el valor devuelto por el servidor remoto, confirmando la integridad.

---

### Aporte 3: Rate Limiting con Ventana Deslizante (Protección contra Sobrecarga)

#### ¿En qué consiste?

> **Apagado por defecto.** App Java no limita, así que con el balanceador repartiendo entre las
> dos implementaciones el mismo cliente recibiría `429` de una réplica y `200` de otra. Quedó
> como extensión fuera del contrato (§9 de [`CONTRATO.md`](CONTRATO.md)): se enciende con
> `TP_RATE_LIMIT=on`.

Limitador de tasa por algoritmo de ventana deslizante (`Sliding Window`). Cada cliente (IP)
puede realizar un máximo de **100 peticiones cada 60 segundos**; al superarlo, el servidor
responde `429 Too Many Requests`. El código no clava el número: lo toma del enumerado
`http.HTTPStatus` de la biblioteca estándar, que es la traducción del RFC de HTTP.

El default está elegido para no chocar con el health check del balanceador, que consulta
`/health` de forma periódica desde una única IP: con un límite muy bajo recibiría `429`,
interpretaría que la instancia está caída y sacaría de rotación réplicas sanas.

**Cubre todas las rutas, `/health` incluida.** Un endpoint de salud sin límite es el más fácil
de usar para terminar de tirar abajo un servicio ya degradado, y además suele ser el más público:
el resto de las rutas puede no estar difundido.

**Dónde vive el contador.** El limitador tiene dos backends intercambiables:

| Backend | Cuándo se usa | Alcance del límite |
| :--- | :--- | :--- |
| `memoria` | Sin `TP_REDIS_URL` | Por instancia — cada réplica cuenta por su cuenta |
| `redis` | Con `TP_REDIS_URL` definida | Por servicio — todas las réplicas comparten el contador |

Con réplicas detrás de un balanceador el contador en memoria deja de servir: dos instancias
llevan cuentas separadas y el límite efectivo se duplica. El backend Redis resuelve eso
guardando cada IP como un *sorted set* cuyo score es el instante de la petición. El chequeo
y el alta van en un script Lua para que sean **una sola operación atómica**: si se resolvieran
en dos viajes, dos réplicas podrían leer el mismo conteo y ambas dejar pasar la petición que
debía cortarse — el mismo problema de exclusión mutua que resuelve el `Lock` entre hebras,
pero entre procesos que no comparten memoria.

Si `TP_REDIS_URL` está definida pero Redis no responde, la app **arranca igual** con el
contador local y lo deja dicho en el log: un Redis caído no debería dejar el servicio afuera.

Variables de entorno: `TP_RATE_LIMIT` (`off`), `TP_REDIS_URL`, `TP_RATE_LIMIT_MAX` (100),
`TP_RATE_LIMIT_WINDOW` (60). Sin `TP_RATE_LIMIT=on` las otras tres no hacen nada.

---

#### ¿Cómo probarlo?

**Una sola instancia** (contador local):

1. **Encender la extensión y bajar el límite** para poder mostrarlo sin tirar 100 peticiones:
   ```bash
   TP_RATE_LIMIT=on TP_RATE_LIMIT_MAX=5 TP_RATE_LIMIT_WINDOW=10 python3 Clase01/app.py 8080
   ```
   El arranque confirma que quedó activa:
   ```
   [rate-limit] 5 peticiones cada 10s por IP, backend 'memoria', todas las rutas (extensión propia, fuera del contrato)
   ```

2. **Enviar ráfaga de peticiones continuas**:
   ```bash
   for i in {1..6}; do curl -s -o /dev/null -w "%{http_code}\n" http://<IP_ADDRESS>:8080/health; done
   ```

3. **Resultado Observado**:
   * Peticiones 1 a 5: `200`
   * Petición 6: `429` con el JSON de error:
     ```json
     {
       "error": "Demasiadas peticiones (429 Too Many Requests)",
       "mensaje": "Se superó el límite de 5 peticiones cada 10 segundos.",
       "ip": "<IP_ADDRESS>"
     }
     ```

**Dos réplicas compartiendo el contador** (es la prueba que importa para el balanceo):

```bash
docker run -d --rm --name tp-redis -p 6399:6379 redis:7-alpine
export TP_RATE_LIMIT=on                              # la extensión está apagada por defecto
export TP_REDIS_URL="redis://127.0.0.1:6399/0"
export TP_RATE_LIMIT_MAX=5 TP_RATE_LIMIT_WINDOW=10   # para que corte rápido en la demo
python3 Clase01/app.py 8101 &
python3 Clase01/app.py 8102 &

# Alternar entre las dos, como repartiría un balanceador
for i in {1..4}; do
  curl -s -o /dev/null -w "A %{http_code}\n" http://127.0.0.1:8101/health
  curl -s -o /dev/null -w "B %{http_code}\n" http://127.0.0.1:8102/health
done
```

Resultado esperado: **cinco `200` en total entre ambas** y `429` desde cualquiera de las dos
a partir de la sexta — no cinco por cada una.

## 🤝 Entrega 2 — Clase 2

La Clase 1 fue una app corriendo en un servidor. La Clase 2 la convierte en un **servicio
distribuido**: réplicas repartidas entre las casas del grupo, un balanceador que reparte el
tráfico, estado compartido en una base y despliegues que no cortan el servicio.

El cambio de fondo es que las réplicas dejan de ser "nuestra app". Para un cliente que entra por
el balanceador, una réplica de App Python y una de App Java tienen que ser **indistinguibles**.

### El contrato con App Java

Al comparar las dos implementaciones lado a lado aparecieron **ocho divergencias**: desde el
formato del timestamp de arranque hasta qué código devuelve `/echo` sin `ping`. Cualquiera de
ellas rompe a un cliente que reciba respuestas de una réplica u otra según a quién derivó el
balanceador.

Están todas resueltas en **[`CONTRATO.md`](CONTRATO.md)** (v1.1), que es la especificación que
cumplen las dos apps. Ante una diferencia, **manda el contrato**, no este README.

### Extensiones apagadas por defecto

Los aportes propios de la Clase 1 son cosas que **sólo tiene App Python**. Con el balanceador
repartiendo entre implementaciones distintas, una extensión encendida hace que el servicio
responda distinto según quién atendió — justo lo que el contrato evita. Por eso quedan detrás de
un interruptor, apagadas salvo que se pidan:

| Variable | Qué enciende | Default |
| :--- | :--- | :--- |
| `TP_CHECKSUM` | El campo `checksum` (SHA-256 del fuente) en `/` y `/health` | `off` |
| `TP_RATE_LIMIT` | El límite de peticiones por IP y las respuestas `429` | `off` |

Se encienden con `on`, `1`, `true`, `si` o `sí`; cualquier otro valor las deja apagadas.

```bash
# Comportamiento del contrato — la respuesta es indistinguible de App Java
python3 Clase01/app.py 8080

# Con las dos extensiones encendidas, para mostrarlas en la demo
TP_CHECKSUM=on TP_RATE_LIMIT=on python3 Clase01/app.py 8080
```

Cada extensión encendida deja constancia en el arranque, así que se ve de un vistazo con qué
configuración quedó levantada una réplica:

```
[rate-limit] 100 peticiones cada 60s por IP, backend 'memoria', todas las rutas (extensión propia, fuera del contrato)
[checksum] expuesto en / y /health (extensión propia, fuera del contrato)
```

### Variables de entorno

| Variable | Para qué | Default |
| :--- | :--- | :--- |
| `PORT` | Puerto de escucha. El primer argumento de línea de comandos le gana. | `8080` |
| `HOST_NAME` | Identidad de la instancia, en el campo `host` de `GET /`. | *hostname* de la máquina |
| `TP_CHECKSUM` | Enciende la extensión del checksum. | `off` |
| `TP_RATE_LIMIT` | Enciende la extensión del rate limiting. | `off` |
| `TP_RATE_LIMIT_MAX` | Peticiones permitidas por ventana. | `100` |
| `TP_RATE_LIMIT_WINDOW` | Duración de la ventana, en segundos. | `60` |
| `TP_REDIS_URL` | Contador del rate limiting compartido entre réplicas. | vacío → contador local |

`TP_REDIS_URL` lleva la contraseña de Redis adentro, así que **no se versiona**: va en el `.env`
de cada nodo, que está en el `.gitignore`.

### Estado

| | Punto | Estado |
| :--- | :--- | :--- |
| ✅ | Contrato acordado y App Python al día con la v1.1 | Verificado corriendo las dos apps lado a lado |
| ✅ | `GET /slow` y graceful shutdown | Vienen de la Clase 1; se reusan para probar el deploy sin downtime |
| ⬜ | `/personas` sobre Redis (§6 del contrato) | Pendiente |
| ⬜ | Bitácora a disco, variable `CASA` (§8 del contrato) | Pendiente |
| ⬜ | `deploy.sh` blue-green con rollback | Pendiente |
| ⬜ | Dónde corre Redis y qué red une las casas | A definir con el grupo |

---

## Mejoras al Enunciado

Tres huecos de la consigna que encontramos montando esto.

### 1. Pide exclusión mutua, pero no da con qué construirla
**Qué:** exigir una cuenta de sistema por equipo y un archivo de dueño en el servidor (PID, equipo, hora de arranque) que haya que leer antes de frenar nada.
**Por qué:** los tres equipos entramos con el mismo usuario, así que no existe la noción de "dueño" de un proceso: cualquiera puede matar cualquier cosa y nadie puede probar quién fue. La consigna pregunta si se puede lograr que sólo el dueño mate su proceso, pero el armado que habilita lo vuelve imposible.

### 2. El pipeline no tiene salida de emergencia
**Qué:** un sexto paso obligatorio, Rollback, y que el Ship deje los artefactos versionados en vez de sobrescribir.
**Por qué:** la secuencia termina en Verify y no dice qué hacer si Verify falla. Para ese momento el proceso viejo ya está muerto y el artefacto anterior pisado (le pasó a Java, la v2 sobrescribió a la v1), así que producción queda caída sin camino de vuelta.

### 3. Las preguntas de seguridad miran el disco, no el tráfico
**Qué:** que el contrato incluya una ruta que maneje un dato sensible (un token en un header) y que cada equipo explique, salto por salto, quién puede leerlo.
**Por qué:** la consigna pregunta qué le impide a Plataforma leer el código ajeno, pero el agujero más grande es otro: toda la conectividad pasa por el túnel que ellos montan, y su inspector les muestra cada petición y respuesta completas de las dos apps en texto plano.

---

## Preguntas de Análisis Distribuidos

El puerto TCP de producción es el recurso crítico y el único árbitro físico que garantiza la exclusión mutua es el kernel del servidor al procesar la syscall `bind()`, rebotando cualquier intento concurrente con el error `Address already in use`. En la arquitectura de esta tarea la exclusión mutua es de carácter centralizado, ya que no existe un protocolo de consenso distribuido entre las casas de los integrantes y toda la contención se resuelve en el único servidor de Plataforma. La coordinación entre equipos fuera del sistema operativo se sostiene de forma puramente social mediante acuerdos por canal de chat.

Si dos equipos intentan desplegar al mismo tiempo se produce una condición de carrera no determinista dictada por el scheduling del kernel y la latencia de la red. En una colisión pasiva la aplicación que primero logra ejecutar la llamada `bind()` se queda con el puerto, mientras que la segunda falla inmediatamente al recibir el error de dirección en uso y finaliza. En una colisión activa donde se ejecutan comandos de detención sin coordinación previa, los procesos pueden matarse entre sí o corromper los archivos transferidos si comparten la misma ruta de subida.

El pipeline manual carece de atomicidad y transaccionalidad, por lo que una caída de conectividad en pleno despliegue deja al sistema en un estado inconsistente. Si la falla ocurre durante la transferencia del artefacto el archivo queda incompleto en disco pero el servicio anterior continúa respondiendo. Si el corte sucede entre el frenado de la app anterior y la inicialización de la nueva, el puerto queda libre sin ningún proceso escuchando y se genera una denegación de servicio total. Si la conexión cae durante la ejecución y la app no fue desvinculada del pseudo-terminal remoto, la señal enviada por la sesión SSH terminada mata al proceso nuevo.

Los pasos que evidenciaron la necesidad de automatización fueron stop, kill process, y levantar el proceso con controles de exclusión mutua.

---

## Preguntas Picantes

Técnicamente a Mateo y Juan no les impide absolutamente nada leer, modificar o tumbar las aplicaciones ajenas. Al ser los administradores del servidor y del host tienen privilegios de superusuario que les permiten acceder al disco rígido, inspeccionar la memoria RAM de los procesos en ejecución, modificar los archivos subidos vía SSH o matar cualquier proceso con un comando. Tampoco existe cifrado en reposo o aislamiento que proteja el código frente al root del sistema. En un entorno de producción real nadie le confiaría código o datos sensibles a la máquina de un tercero sin garantías de computación confidencial o enclaves seguros; en este escenario la seguridad no se apoya en ningún control técnico sino en la pura confianza social entre compañeros.

Cualquier equipo puede frenar la aplicación del otro simplemente porque todos acceden a través de la misma cuenta de sistema o con permisos suficientes para enviar señales a la tabla de procesos. Para evitar que un usuario apague el servicio ajeno por error o con mala intención se requiere implementar cuentas del sistema operativo independientes para cada equipo. El kernel de Linux prohíbe que un usuario no privilegiado le envíe señales como SIGTERM o SIGKILL a procesos que pertenecen a otro identificador de usuario. De este modo, la única forma de liberar el puerto de manera ordenada sin dar permisos cruzados de detención es mediante un proceso intermediario supervisor o un contrato de orchestración que valide la identidad del solicitante.

Al publicar la computadora hogareña a internet mediante túneles o apertura de puertos, la máquina queda expuesta a escaneos automáticos de vulnerabilidades, ataques de fuerza bruta contra el puerto SSH y posibles ejecuciones remotas de código si las aplicaciones web contienen fallas. El peligro no se limita a esa PC sino que se extiende a toda la red local de la casa, ya que si un atacante logra comprometer el servidor puede realizar movimientos laterales hacia otros dispositivos conectados a la misma red WiFi o LAN. La responsabilidad legal y operativa recae de forma exclusiva sobre el dueño del equipo y titular del contrato de internet, cuya dirección IP pública es la que queda registrada ante cualquier actividad maliciosa originada desde su nodo.

Para realizar el despliegue los equipos ingresan al servidor con acceso a una shell interactiva del sistema operativo. Si las credenciales otorgadas cuentan con permisos amplios o acceso al grupo de sudo o Docker, los integrantes no solo pueden desplegar su aplicación sino también inspeccionar archivos privados en el directorio del sistema, listar variables de entorno globales, consumir recursos de CPU y memoria provocando una denegación de servicio local, o incluso realizar un escape de contenedor hacia la máquina host de Windows. La gestión segura de accesos exigiría aplicar el principio de menor privilegio limitando a los usuarios mediante entornos restringidos y directorios aislados sin visión del resto del sistema.

Cualquier credencial, token o contraseña que utilice la aplicación queda expuesta a la vista de los administradores y de cualquiera con acceso al servidor si se almacena en el código fuente, en archivos de configuración o en variables de entorno. Adicionalmente existe una trampa en el tráfico de red: al utilizar túneles de ngrok administrados por Plataforma, el inspector de tráfico integrado expone en texto plano el contenido de cada petición y respuesta HTTP. Toda clave enviada en los encabezados o en el cuerpo de las peticiones queda registrada y legible en la consola local del inspector para quien monta la infraestructura.