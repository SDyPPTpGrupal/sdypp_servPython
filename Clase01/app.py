import hashlib
import json
import os
import platform
import signal
import socket
import sys
import threading
import time
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# --- Configuración de la aplicación ---
# Los valores de esta sección son contrato: ver CONTRATO.md. La App Java devuelve
# las mismas claves, con los mismos tipos y en el mismo orden.
APP_NAME = "python"
LENGUAJE = f"Python {platform.python_version()}"
EQUIPO = [
    "Tomás",
    "Mateo",
    "Salvador",
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

# --- Extensión propia, fuera del contrato ---
# El checksum no existe en la App Java (D-2), así que con el balanceador repartiendo
# entre réplicas de los dos lenguajes el campo aparecería o no según quién atienda.
# Queda apagado por defecto; se enciende con TP_CHECKSUM=on para mostrarlo en la demo.
CHECKSUM_EXPUESTO = os.environ.get("TP_CHECKSUM", "off").strip().lower() in ("on", "1", "true", "si", "sí")

# --- Rate Limiting (Ventana Deslizante) ---
# Es parte del contrato: las dos implementaciones tienen que limitar igual, si no
# el mismo cliente recibiría 429 de una réplica y 200 de otra.
#
# Se aplica a TODAS las rutas, /health incluido. Un endpoint de salud sin límite es
# el más fácil de usar para terminar de tirar abajo un servicio ya degradado, y
# además suele ser el más público: el resto de las rutas puede no estar difundido.
#
# El default (100 cada 60s) está elegido para no chocar con el health check del
# balanceador, que consulta /health de forma periódica desde una única IP.
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


def crear_limitador():
    """Elige el backend del rate limiting según la configuración.

    Sin TP_REDIS_URL la app arranca igual con el contador local: alcanza para el
    desarrollo y para la demo de una sola instancia. Con réplicas hay que apuntar
    todas al mismo Redis, si no el límite deja de ser del servicio.
    """
    if not REDIS_URL:
        print("[rate-limit] contador local de esta instancia (sin TP_REDIS_URL)")
        return LimitadorEnMemoria()
    try:
        limitador = LimitadorRedis(REDIS_URL)
        print(f"[rate-limit] contador compartido en Redis ({REDIS_URL})")
        return limitador
    except Exception as e:
        # Preferimos degradar antes que no levantar: un Redis caído no debería
        # dejar el servicio afuera, pero tiene que quedar dicho en el log.
        print(f"[rate-limit] Redis no disponible ({e}); se usa el contador local de esta instancia")
        return LimitadorEnMemoria()


LIMITADOR = crear_limitador()


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
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "se requiere el campo ping"})
            return

        try:
            body = self.rfile.read(content_length)
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON inválido"})
            return

        # Sin el campo ping la petición es inválida: 400, no un pong vacío (D-5).
        if not isinstance(payload, dict) or "ping" not in payload:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "se requiere el campo ping"})
            return

        self._send_json(HTTPStatus.OK, {
            "pong": payload["ping"],
            "servidoPor": APP_NAME,
            "version": VERSION,
        })

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
    print(f"[rate-limit] {RATE_LIMIT_MAX} peticiones cada {RATE_LIMIT_WINDOW}s por IP, backend '{LIMITADOR.nombre}', todas las rutas")
    if CHECKSUM_EXPUESTO:
        print("[checksum] expuesto en / y /health (extensión propia, fuera del contrato)")
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
