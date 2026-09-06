import hashlib
import json
import os
import platform
import signal
import socket
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# --- Configuración de la aplicación ---
# Los valores de esta sección son contrato: ver CONTRATO.md. La App Java devuelve
# las mismas claves, con los mismos tipos y en el mismo orden.
APP_NAME = "python"
LENGUAJE = f"Python {platform.python_version()}"
# Un objeto por integrante: el legajo es un campo propio y no un dato embutido en
# el string del nombre, que obligaría al cliente a parsear por paréntesis (D-1).
EQUIPO = [
    {"nombre": "Tomás", "apellido": "Resnik", "legajo": 190168},
    {"nombre": "Mateo", "apellido": "Nomico", "legajo": 168102},
    {"nombre": "Salvador", "apellido": "Baez", "legajo": 195157},
]
VERSION = 1
MENSAJE = "hola mundo python"

# Metadatos del entorno y arranque.
# HOST_NAME identifica la instancia; el nombre de la variable es contrato (D-3).
HOST = os.environ.get("HOST_NAME", socket.gethostname())
# Precisión de segundos, sin fracción: el formato es contrato (D-4).
ARRANCADO = datetime.now().astimezone().replace(microsecond=0).isoformat()

# Rutas del contrato y métodos admitidos por cada una. Lo que no está acá es 404;
# lo que está pero con otro método es 405 (D-6 y D-7).
RUTAS = {
    "/": {"GET"},
    "/health": {"GET"},
    "/slow": {"GET"},
    "/echo": {"POST"},
    "/personas": {"GET", "POST"},
}


def calculate_checksum() -> str:
    """Calcula el hash SHA-256 del propio archivo fuente en tiempo de arranque."""
    try:
        app_path = os.path.abspath(__file__)
        with open(app_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception as e:
        return f"error: {str(e)}"

CHECKSUM = calculate_checksum()

def _encendido(variable: str) -> bool:
    """Lee una variable de entorno como interruptor. Apagado si no está definida."""
    return os.environ.get(variable, "off").strip().lower() in ("on", "1", "true", "si", "sí")


def _url_sin_credenciales(url: str) -> str:
    """Recorta la contraseña de una URL antes de escribirla en el log.

    TP_REDIS_URL lleva la credencial adentro y el log va a parar a un archivo del
    disco de la casa: escribirla entera la filtraría a cualquiera que lo lea.
    """
    try:
        partes = urllib.parse.urlsplit(url)
        puerto = f":{partes.port}" if partes.port else ""
        return f"{partes.scheme}://{partes.hostname or '?'}{puerto}"
    except Exception:
        return "(url ilegible)"

# --- Extensión propia, fuera del contrato ---
# El checksum no existe en la App Java (D-2), así que con el balanceador repartiendo
# entre réplicas de los dos lenguajes el campo aparecería o no según quién atienda.
# Queda apagado por defecto; se enciende con TP_CHECKSUM=on para mostrarlo en la demo.
CHECKSUM_EXPUESTO = _encendido("TP_CHECKSUM")

# --- Rate Limiting (Ventana Deslizante) — extensión propia, fuera del contrato ---
# La App Java no limita, así que con el balanceador repartiendo entre réplicas de
# los dos lenguajes el mismo cliente recibiría 429 de una y 200 de otra. Queda
# apagado por defecto; se enciende con TP_RATE_LIMIT=on.
#
# Se aplica a TODAS las rutas, /health incluido. Un endpoint de salud sin límite es
# el más fácil de usar para terminar de tirar abajo un servicio ya degradado, y
# además suele ser el más público: el resto de las rutas puede no estar difundido.
#
# El default (100 cada 60s) está elegido para no chocar con el health check del
# balanceador, que consulta /health de forma periódica desde una única IP: si
# chequea más seguido recibe 429 y termina sacando de rotación réplicas sanas.
RATE_LIMIT_ACTIVO = _encendido("TP_RATE_LIMIT")
RATE_LIMIT_MAX = int(os.environ.get("TP_RATE_LIMIT_MAX", 100))
RATE_LIMIT_WINDOW = int(os.environ.get("TP_RATE_LIMIT_WINDOW", 60))
REDIS_URL = os.environ.get("TP_REDIS_URL", "")


class LimitadorEnMemoria:
    """Ventana deslizante en la RAM del proceso.

    Limitación conocida: el contador es local a la instancia. Con varias réplicas
    detrás de un balanceador cada una lleva su propia cuenta, así que el límite
    efectivo pasa a ser RATE_LIMIT_MAX por réplica y no por servicio.
    """

    nombre = "memoria"

    def __init__(self):
        self._historial = {}
        self._lock = threading.Lock()

    def excede(self, ip: str) -> bool:
        """Registra la petición y devuelve True si la IP superó el límite."""
        ahora = time.time()
        # ThreadingHTTPServer atiende cada petición en su propia hebra: sin el
        # lock, dos hebras pueden leer el mismo historial y dejar pasar de más.
        with self._lock:
            vigentes = [t for t in self._historial.get(ip, []) if ahora - t < RATE_LIMIT_WINDOW]
            if len(vigentes) >= RATE_LIMIT_MAX:
                self._historial[ip] = vigentes
                return True
            vigentes.append(ahora)
            self._historial[ip] = vigentes
            return False


# Consultar el contador y registrar la petición tienen que ser una sola operación
# atómica: si se resuelve en dos viajes a Redis, dos réplicas pueden leer el mismo
# conteo y ambas dejar pasar la petición que debía cortarse. Redis corre el script
# entero sin intercalar comandos de otros clientes, así que el chequeo y el alta
# no se pueden separar. Es el mismo problema de exclusión mutua del lock de arriba,
# pero entre procesos que no comparten memoria.
_LUA_VENTANA_DESLIZANTE = """
local clave   = KEYS[1]
local ahora   = tonumber(ARGV[1])
local ventana = tonumber(ARGV[2])
local maximo  = tonumber(ARGV[3])
local miembro = ARGV[4]

redis.call('ZREMRANGEBYSCORE', clave, 0, ahora - ventana)
if redis.call('ZCARD', clave) >= maximo then
  return 1
end
redis.call('ZADD', clave, ahora, miembro)
redis.call('EXPIRE', clave, ventana)
return 0
"""


class LimitadorRedis:
    """Ventana deslizante compartida por todas las réplicas del servicio.

    Cada IP es un sorted set cuyos miembros son las peticiones y cuyo score es el
    instante en que llegaron; la ventana se recorta por score en cada consulta.
    """

    nombre = "redis"

    def __init__(self, url: str):
        import redis  # dependencia externa, ver requirements.txt

        self._cliente = redis.Redis.from_url(
            url, socket_timeout=1, socket_connect_timeout=1
        )
        self._cliente.ping()  # falla acá si no hay Redis, no en la primera petición
        self._script = self._cliente.register_script(_LUA_VENTANA_DESLIZANTE)

    def excede(self, ip: str) -> bool:
        ahora = time.time()
        # El miembro tiene que ser único: dos peticiones en el mismo instante
        # comparten score y el sorted set descartaría una de las dos.
        miembro = f"{ahora}:{uuid.uuid4().hex}"
        excedido = self._script(
            keys=[f"ratelimit:{ip}"],
            args=[ahora, RATE_LIMIT_WINDOW, RATE_LIMIT_MAX, miembro],
        )
        return bool(excedido)


class LimitadorApagado:
    """Objeto nulo para cuando la extensión está apagada: nunca excede.

    Evita que el handler tenga que preguntar en cada petición si hay limitador.
    """

    nombre = "apagado"

    def excede(self, ip: str) -> bool:
        return False


def crear_limitador():
    """Elige el backend del rate limiting según la configuración.

    Sin TP_RATE_LIMIT=on no se limita nada: es una extensión que la App Java no
    tiene. Con la extensión encendida pero sin TP_REDIS_URL se usa el contador
    local, que alcanza para el desarrollo y para la demo de una sola instancia;
    con réplicas hay que apuntarlas todas al mismo Redis, si no el límite deja de
    ser del servicio.
    """
    if not RATE_LIMIT_ACTIVO:
        return LimitadorApagado()
    if not REDIS_URL:
        print("[rate-limit] contador local de esta instancia (sin TP_REDIS_URL)")
        return LimitadorEnMemoria()
    try:
        limitador = LimitadorRedis(REDIS_URL)
        print(f"[rate-limit] contador compartido en Redis ({_url_sin_credenciales(REDIS_URL)})")
        return limitador
    except Exception as e:
        # Preferimos degradar antes que no levantar: un Redis caído no debería
        # dejar el servicio afuera, pero tiene que quedar dicho en el log.
        print(f"[rate-limit] Redis no disponible ({e}); se usa el contador local de esta instancia")
        return LimitadorEnMemoria()


LIMITADOR = crear_limitador()


# --- Estado compartido: /personas sobre la base (§6 del contrato) ---
# Los datos no viven en la memoria de ninguna instancia: las réplicas quedan
# stateless, cualquiera puede atender cualquier petición y la que se muere no se
# lleva nada consigo. Es lo que hace que el balanceador pueda repartir sin que el
# cliente note quién lo atendió.

# Límites de validación (§6). El tope del legajo es el máximo de un entero de 32
# bits: Python maneja enteros de precisión ilimitada, pero un int de Java desborda,
# así que el contrato se fija en el más restrictivo de los dos lenguajes.
LEGAJO_MIN = 1
LEGAJO_MAX = 2147483647
NOMBRE_MAX = 120


class BaseNoDisponible(Exception):
    """La base compartida no respondió. Se traduce en un 503."""


# El alta tiene que ser atómica de punta a punta. Entre comprobar que el legajo no
# está registrado y escribirlo, otra réplica puede colarse con el mismo legajo; y
# entre pedir el id y usarlo, otra puede pedir el mismo. Redis corre el script
# entero sin intercalar comandos de otros clientes, así que las cinco operaciones
# valen por una. Es el mismo problema de exclusión mutua del rate limiting, y es lo
# que nos ahorra tener que coordinar las casas entre sí para dar de alta a alguien.
_LUA_ALTA_PERSONA = """
local clave_legajo = KEYS[1]
local nombre = ARGV[1]
local legajo = ARGV[2]

if redis.call('EXISTS', clave_legajo) == 1 then
  return -1
end

local id = redis.call('INCR', 'personas:seq')
redis.call('HSET', 'persona:' .. id, 'nombre', nombre, 'legajo', legajo)
redis.call('ZADD', 'personas:index', id, id)
redis.call('SET', clave_legajo, id)
return id
"""


class RepositorioPersonas:
    """Acceso a la base compartida con el esquema de claves de §6 del contrato.

    El esquema es contrato tanto como el JSON: si la App Java guardara la misma
    persona bajo otra clave o con otra estructura, las dos apps escribirían en la
    misma base sin encontrar lo del otro.
    """

    def __init__(self, url: str):
        import redis  # dependencia externa, ver requirements.txt

        self._cliente = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=1,
            socket_connect_timeout=1,
        )
        # register_script no viaja a Redis, sólo calcula el SHA del script. Así el
        # repositorio se construye aunque la base esté caída y empieza a funcionar
        # solo cuando vuelve, sin reiniciar la réplica.
        self._alta = self._cliente.register_script(_LUA_ALTA_PERSONA)

    def listar(self) -> list:
        """Personas ordenadas por id ascendente.

        El orden sale del sorted set: sin un orden fijo, dos réplicas devuelven el
        mismo conjunto en distinta secuencia y el servicio parece errático.
        """
        try:
            ids = self._cliente.zrange("personas:index", 0, -1)
            tuberia = self._cliente.pipeline()
            for identificador in ids:
                tuberia.hgetall(f"persona:{identificador}")
            registros = tuberia.execute()
        except Exception as e:
            raise BaseNoDisponible(e)

        personas = []
        for identificador, registro in zip(ids, registros):
            if not registro:
                continue  # el índice quedó apuntando a una persona borrada a mano
            personas.append({
                "id": int(identificador),
                "nombre": registro["nombre"],
                "legajo": int(registro["legajo"]),
            })
        return personas

    def crear(self, nombre: str, legajo: int):
        """Da de alta y devuelve el id, o None si el legajo ya estaba registrado.

        El id lo asigna la base (INCR), nunca la app: si lo calculara cada réplica
        contando lo que ya hay, dos altas simultáneas se pisarían.
        """
        try:
            resultado = self._alta(keys=[f"legajo:{legajo}"], args=[nombre, legajo])
        except Exception as e:
            raise BaseNoDisponible(e)
        return None if int(resultado) == -1 else int(resultado)


def crear_repositorio():
    """Prepara el acceso a la base. Sin TP_REDIS_URL, /personas responde 503.

    Un fallo acá (falta la librería, URL mal escrita) no tiene que impedir que la
    réplica arranque: las otras rutas no dependen de la base y el balanceador la
    tiene que poder seguir usando.
    """
    if not REDIS_URL:
        return None
    try:
        return RepositorioPersonas(REDIS_URL)
    except Exception as e:
        print(f"[personas] no se pudo preparar el acceso a la base ({e}); /personas responde 503")
        return None


REPOSITORIO = crear_repositorio()


def validar_persona(payload: dict):
    """Valida el cuerpo de POST /personas en el orden que fija §6 del contrato.

    Devuelve (datos, error). El orden es parte del contrato: ante un cuerpo con dos
    problemas a la vez, las dos implementaciones tienen que devolver el mismo error
    y no cada una el que detectó primero.
    """
    nombre = payload.get("nombre")
    legajo = payload.get("legajo")
    limpio = nombre.strip() if isinstance(nombre, str) else nombre

    # 2. Presencia. Un nombre que queda vacío después del trim cuenta como ausente,
    #    igual que la clave que no vino o la que vale null.
    if nombre is None or legajo is None or limpio == "":
        return None, "se requieren los campos nombre y legajo"

    # 3. Entero de verdad. En Python isinstance(True, int) es True, así que el
    #    booleano hay que descartarlo aparte. El string numérico y el decimal
    #    también caen acá: convertirlos obligaría a las dos apps a coincidir en qué
    #    hacer con "0100200", " 100200 " y "1e5".
    if isinstance(legajo, bool) or not isinstance(legajo, int):
        return None, "legajo debe ser numérico"

    # 4. Rango.
    if not LEGAJO_MIN <= legajo <= LEGAJO_MAX:
        return None, "legajo fuera de rango"

    # 5. Nombre string y acotado, medido sobre el valor ya trimeado.
    if not isinstance(limpio, str) or len(limpio) > NOMBRE_MAX:
        return None, "nombre inválido"

    # Los campos de más se ignoran, incluido un id que venga en el cuerpo: el id lo
    # asigna la base. Ignorarlos es lo que permite que el contrato crezca sin
    # romper a un cliente viejo.
    return {"nombre": limpio, "legajo": legajo}, None


class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._despachar("GET")

    def do_POST(self):
        self._despachar("POST")

    def do_PUT(self):
        self._despachar("PUT")

    def do_DELETE(self):
        self._despachar("DELETE")

    def do_PATCH(self):
        self._despachar("PATCH")

    def _despachar(self, metodo: str):
        """Punto único de entrada: límite, ruta, método y recién ahí el handler."""
        # El rate limiting va primero, antes del 404 y del 405: si no, una ráfaga
        # contra rutas inexistentes no quedaría limitada.
        if self._rate_limited():
            return

        path = self._normalize_path()

        if path not in RUTAS:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "ruta no encontrada"})
            return
        if metodo not in RUTAS[path]:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "metodo no permitido"})
            return

        if path == "/":
            self._ruta_raiz()
        elif path == "/health":
            self._ruta_health()
        elif path == "/slow":
            self._ruta_slow()
        elif path == "/echo":
            self._ruta_echo()
        elif path == "/personas":
            if metodo == "GET":
                self._ruta_personas_listar()
            else:
                self._ruta_personas_alta()

    def _ruta_raiz(self):
        response_data = {
            "app": APP_NAME,
            "lenguaje": LENGUAJE,
            "equipo": EQUIPO,
            "version": VERSION,
            "mensaje": MENSAJE,
            "host": HOST,
            "arrancado": ARRANCADO,
        }
        if CHECKSUM_EXPUESTO:
            response_data["checksum"] = CHECKSUM
        self._send_json(HTTPStatus.OK, response_data)

    def _ruta_health(self):
        response_data = {
            "status": "ok",
            "app": APP_NAME,
            "version": VERSION,
        }
        if CHECKSUM_EXPUESTO:
            response_data["checksum"] = CHECKSUM
        self._send_json(HTTPStatus.OK, response_data)

    def _ruta_slow(self):
        """Petición deliberadamente lenta.

        Sirve para dos cosas de la demo: probar que el graceful shutdown drena las
        peticiones en vuelo, y tener tráfico en curso mientras se hace el deploy
        blue-green para mostrar que no se pierde ninguna.
        """
        print("[/slow] Procesando petición lenta (simulando tarea de 4 segundos)...")
        time.sleep(4)
        self._send_json(HTTPStatus.OK, {
            "status": "ok",
            "mensaje": "Petición lenta completada con éxito a pesar de recibir la orden de apagado.",
            "app": APP_NAME,
            "version": VERSION,
        })
        print("[/slow] Petición lenta finalizada.")

    def _ruta_echo(self):
        payload, error = self._leer_cuerpo_json()
        if error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": error})
            return

        # Sin el campo ping la petición es inválida: 400, no un pong vacío (D-5).
        if "ping" not in payload:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "se requiere el campo ping"})
            return

        self._send_json(HTTPStatus.OK, {
            "pong": payload["ping"],
            "servidoPor": APP_NAME,
            "version": VERSION,
        })

    def _ruta_personas_listar(self):
        """GET /personas — lo guardado en la base, ordenado por id."""
        if REPOSITORIO is None:
            self._base_no_disponible("no hay TP_REDIS_URL configurada")
            return
        try:
            personas = REPOSITORIO.listar()
        except BaseNoDisponible as e:
            self._base_no_disponible(e)
            return

        self._send_json(HTTPStatus.OK, {
            "servidoPor": APP_NAME,
            "personas": personas,
        })

    def _ruta_personas_alta(self):
        """POST /personas — alta con la validación y el orden de §6."""
        payload, error = self._leer_cuerpo_json()
        if error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": error})
            return

        datos, error = validar_persona(payload)
        if error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": error})
            return

        if REPOSITORIO is None:
            self._base_no_disponible("no hay TP_REDIS_URL configurada")
            return
        try:
            identificador = REPOSITORIO.crear(datos["nombre"], datos["legajo"])
        except BaseNoDisponible as e:
            self._base_no_disponible(e)
            return

        if identificador is None:
            self._send_json(HTTPStatus.CONFLICT, {"error": "el legajo ya está registrado"})
            return

        self._send_json(HTTPStatus.CREATED, {
            "servidoPor": APP_NAME,
            "id": identificador,
            "nombre": datos["nombre"],
            "legajo": datos["legajo"],
        })

    def _base_no_disponible(self, motivo):
        """503 de /personas.

        No hay degradación posible: sin base no hay datos. Devolver 200 con una
        lista vacía sería peor que fallar, porque el cliente no podría distinguir
        "no hay personas cargadas" de "no pude leerlas". Las demás rutas siguen
        respondiendo normal: no dependen de la base.
        """
        print(f"[personas] la base no respondió: {motivo}")
        self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "base de datos no disponible"})

    def _leer_cuerpo_json(self):
        """Lee el cuerpo como objeto JSON. Devuelve (payload, error).

        No se exige Content-Type: application/json — se parsea el cuerpo venga con
        el header que venga, porque un `curl -d` manda x-www-form-urlencoded por
        defecto y perder una petición válida por eso no le sirve a nadie.
        """
        try:
            largo = int(self.headers.get("Content-Length", 0))
        except ValueError:
            largo = 0
        if largo <= 0:
            return None, "cuerpo JSON inválido"

        try:
            payload = json.loads(self.rfile.read(largo).decode("utf-8"))
        except Exception:
            return None, "cuerpo JSON inválido"

        # Un array o un escalar son JSON válido pero no un cuerpo válido: los
        # campos se buscan por nombre.
        if not isinstance(payload, dict):
            return None, "cuerpo JSON inválido"
        return payload, None

    def _rate_limited(self) -> bool:
        """Aplica el límite a la petición en curso.

        Devuelve True si ya respondió 429 y el handler no tiene que seguir.
        """
        ip = self._client_ip()
        if not LIMITADOR.excede(ip):
            return False

        estado = HTTPStatus.TOO_MANY_REQUESTS
        self._send_json(estado, {
            "error": f"Demasiadas peticiones ({estado.value} {estado.phrase})",
            "mensaje": f"Se superó el límite de {RATE_LIMIT_MAX} peticiones cada {RATE_LIMIT_WINDOW} segundos.",
            "ip": ip,
        })
        return True

    def _normalize_path(self) -> str:
        """Normaliza la ruta ignorando query params y trailing slash."""
        path = self.path.split("?")[0].rstrip("/")
        return path if path else "/"

    def _client_ip(self) -> str:
        """IP real del cliente.

        La app se sirve detrás del balanceador y del túnel HTTP, que terminan la
        conexión y abren una nueva contra la app: sin esto, todas las peticiones
        parecerían venir de la misma IP y el rate limiting trataría a todas las
        casas como un único cliente. La IP de origen viaja en X-Forwarded-For;
        nos quedamos con la primera de la cadena, que es la del cliente original.
        """
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _send_json(self, status_code: int, data: dict):
        response_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} - {format % args}")


def run(port: int = 8080):
    # En el deploy la réplica arranca con `nohup ... > python.log`, y ahí stdout no
    # es una terminal: Python lo bufferiza por bloques y el log queda vacío hasta
    # que se llenan varios KB. Sin esto, el banner de arranque y los avisos del
    # graceful shutdown no aparecen cuando hacen falta, que es justo mientras se
    # está mirando el log para ver si el deploy salió bien.
    sys.stdout.reconfigure(line_buffering=True)

    server_address = ("0.0.0.0", port)
    httpd = ThreadingHTTPServer(server_address, RequestHandler)

    # Manejador de señales para Graceful Shutdown (SIGINT y SIGTERM)
    def stop_server(signum, frame):
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        print(f"\n[ Graceful Shutdown ] Recibida señal {sig_name} (señal {signum}). Drenando conexiones y apagando servidor de forma limpia...")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop_server)
    signal.signal(signal.SIGTERM, stop_server)

    print(f"Servidor iniciado en http://0.0.0.0:{port} (PID: {os.getpid()}) (SHA256: {CHECKSUM[:12]}...) (Arrancado: {ARRANCADO})")
    if RATE_LIMIT_ACTIVO:
        print(f"[rate-limit] {RATE_LIMIT_MAX} peticiones cada {RATE_LIMIT_WINDOW}s por IP, backend '{LIMITADOR.nombre}', todas las rutas (extensión propia, fuera del contrato)")
    if CHECKSUM_EXPUESTO:
        print("[checksum] expuesto en / y /health (extensión propia, fuera del contrato)")
    if REPOSITORIO is None:
        print("[personas] sin TP_REDIS_URL: /personas responde 503 (§6 del contrato)")
    else:
        print(f"[personas] base compartida en {_url_sin_credenciales(REDIS_URL)}")
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        # Esperar a que las hebras de peticiones en curso (in-flight) finalicen
        main_thread = threading.main_thread()
        for t in threading.enumerate():
            if t is not main_thread and t.is_alive():
                t.join(timeout=10)
        print("[ Graceful Shutdown ] Puerto TCP liberado y servidor detenido exitosamente.")


if __name__ == "__main__":
    # Puerto por argumento o por variable de entorno PORT. El default es 8080,
    # igual que el de la App Java: es el mismo contrato de invocación. En el
    # despliegue el puerto real se pasa explícito.
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8080))
    run(port)
