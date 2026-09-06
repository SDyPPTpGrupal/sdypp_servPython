"""App Python — servidor gRPC del contrato v2.1.

El esquema formal está en contrato.proto; las reglas que el .proto no puede
expresar (validación, orden de los chequeos, semántica de los errores) están en
CONTRATO.md. Ante una diferencia, manda el contrato.

Se ejecuta desde la raíz del repositorio:

    python3 -m grpc_tools.protoc -I. --python_out=Clase01 --grpc_python_out=Clase01 contrato.proto
    python3 Clase01/app.py 8080
"""

import os
import platform
import signal
import socket
import sys
import threading
import urllib.parse
from concurrent import futures
from datetime import datetime

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contrato_pb2 as pb
import contrato_pb2_grpc as pb_grpc

# --- Configuración de la aplicación ---
# Los valores de esta sección son contrato: ver CONTRATO.md. La App Java devuelve
# los mismos campos, con los mismos números de campo del .proto.
APP_NAME = "python"
LENGUAJE = f"Python {platform.python_version()}"
# Un Integrante por persona: el legajo es un campo propio y no un dato embutido en
# el string del nombre, que obligaría al cliente a parsear por paréntesis.
EQUIPO = [
    {"nombre": "Tomás", "apellido": "Resnik", "legajo": 190168},
    {"nombre": "Mateo", "apellido": "Nomico", "legajo": 168102},
    {"nombre": "Salvador", "apellido": "Baez", "legajo": 195157},
]
VERSION = 1
MENSAJE = "hola mundo python"

# Metadatos del entorno y arranque.
HOST = os.environ.get("HOST_NAME", socket.gethostname())
CASA = os.environ.get("CASA", "casa-desconocida")
# Precisión de segundos, sin fracción: el formato es contrato.
ARRANCADO = datetime.now().astimezone().replace(microsecond=0).isoformat()

# Hebras que atienden RPCs a la vez.
WORKERS = int(os.environ.get("TP_WORKERS", 10))

REDIS_URL = os.environ.get("TP_REDIS_URL", "")


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


def _ahora_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


# --- Estado compartido: personas sobre la base (CONTRATO.md §6) ---
# Los datos no viven en la memoria de ninguna instancia: las réplicas quedan
# stateless, cualquiera puede atender cualquier RPC y la que se muere no se lleva
# nada consigo. La base corre en una máquina aparte, compartida con la App Java.

LEGAJO_MIN = 1
LEGAJO_MAX = 2147483647  # el tope de int32, que es el tipo del campo en el .proto
NOMBRE_MAX = 120


class BaseNoDisponible(Exception):
    """La base compartida no respondió. Se traduce en UNAVAILABLE."""


# El alta tiene que ser atómica de punta a punta. Entre comprobar que el legajo no
# está registrado y escribirlo, otra réplica puede colarse con el mismo legajo; y
# entre pedir el id y usarlo, otra puede pedir el mismo. Redis corre el script
# entero sin intercalar comandos de otros clientes, así que las cinco operaciones
# valen por una. Es lo que nos ahorra coordinar las casas entre sí para dar de alta
# a alguien: la pregunta 6 del enunciado.
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
    """Acceso a la base compartida con el esquema de claves de CONTRATO.md §6.

    El esquema es contrato tanto como el .proto: si la App Java guardara la misma
    persona bajo otra clave o con otra estructura, las dos apps escribirían en la
    misma base sin encontrar lo del otro.
    """

    def __init__(self, url: str):
        import redis  # dependencia externa, ver requirements.txt

        self._cliente = redis.Redis.from_url(
            url, decode_responses=True, socket_timeout=1, socket_connect_timeout=1
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
    """Prepara el acceso a la base. Sin TP_REDIS_URL las personas dan UNAVAILABLE.

    Un fallo acá (falta la librería, URL mal escrita) no tiene que impedir que la
    réplica arranque: los demás RPC no dependen de la base y el balanceador la
    tiene que poder seguir usando.
    """
    if not REDIS_URL:
        return None
    try:
        return RepositorioPersonas(REDIS_URL)
    except Exception as e:
        print(f"[personas] no se pudo preparar el acceso a la base ({e}); responde UNAVAILABLE")
        return None


REPOSITORIO = crear_repositorio()


def validar_persona(nombre: str, legajo: int):
    """Valida una alta en el orden que fija CONTRATO.md §7.

    Devuelve (nombre_limpio, error). El orden es parte del contrato: ante un
    mensaje con dos problemas a la vez, las dos implementaciones tienen que
    devolver el mismo error y no cada una el que detectó primero.

    En proto3 no se puede distinguir un campo ausente de uno vacío: el string que
    no se manda llega como "" y el int32 como 0. Las dos reglas de abajo cubren
    los dos casos con el mismo error, que es lo que se quiere.
    """
    limpio = nombre.strip()

    if not limpio:
        return None, "se requieren los campos nombre y legajo"

    if not LEGAJO_MIN <= legajo <= LEGAJO_MAX:
        return None, "legajo fuera de rango"

    if len(limpio) > NOMBRE_MAX:
        return None, "nombre inválido"

    return limpio, None


# --- Bitácora (CONTRATO.md §8) ---
# Una línea por RPC atendido, en el disco local del nodo. Un archivo por réplica:
# dos réplicas en el mismo nodo escribiendo el mismo archivo no se pueden
# distinguir después, y distinguirlas es lo que la auditoría tiene que demostrar.
DIRECTORIO_LOGS = os.environ.get("TP_LOGS", "logs")
ARCHIVO_BITACORA = os.path.join(DIRECTORIO_LOGS, f"bitacora-{HOST}.log")
_LOCK_BITACORA = threading.Lock()


def bitacora(rpc: str, codigo: str, identificador=None):
    """Registra una operación atendida.

    El formato es contrato: es lo que permite cruzar este archivo con el log del
    balanceador y auditar una operación puntual. Él registra a quién derivó, el
    nodo registra qué hizo.
    """
    linea = f"{_ahora_iso()} | {APP_NAME}@{CASA} | {rpc} | {codigo} | " \
            f"{'id=' + str(identificador) if identificador is not None else '-'}"
    try:
        with _LOCK_BITACORA:
            os.makedirs(DIRECTORIO_LOGS, exist_ok=True)
            with open(ARCHIVO_BITACORA, "a", encoding="utf-8") as f:
                f.write(linea + "\n")
    except Exception as e:
        # Que no se pueda escribir la bitácora no puede tumbar el servicio.
        print(f"[bitacora] no se pudo escribir: {e}")
    print(linea)


class Servicio(pb_grpc.ServicioServicer):
    """Los cinco RPC del contrato."""

    def Identidad(self, request, context):
        bitacora("Identidad", "OK")
        return pb.Instancia(
            app=APP_NAME,
            lenguaje=LENGUAJE,
            equipo=[pb.Integrante(**integrante) for integrante in EQUIPO],
            version=VERSION,
            mensaje=MENSAJE,
            host=HOST,
            arrancado=ARRANCADO,
        )

    def Salud(self, request, context):
        bitacora("Salud", "OK")
        return pb.EstadoSalud(status="ok", app=APP_NAME, version=VERSION)

    def Echo(self, request, context):
        # En proto3 no se distingue "no mandó ping" de "mandó ping vacío": los dos
        # llegan como "". El contrato trata los dos igual.
        if not request.ping:
            return self._fallar(context, grpc.StatusCode.INVALID_ARGUMENT,
                                "se requiere el campo ping", "Echo")
        bitacora("Echo", "OK")
        return pb.PongRespuesta(pong=request.ping, servido_por=APP_NAME, version=VERSION)

    def ListarPersonas(self, request, context):
        if REPOSITORIO is None:
            return self._sin_base(context, "ListarPersonas", "no hay TP_REDIS_URL configurada")
        try:
            personas = REPOSITORIO.listar()
        except BaseNoDisponible as e:
            return self._sin_base(context, "ListarPersonas", e)

        bitacora("ListarPersonas", "OK")
        return pb.ListaPersonas(
            servido_por=APP_NAME,
            personas=[pb.Persona(**persona) for persona in personas],
        )

    def CrearPersona(self, request, context):
        nombre, error = validar_persona(request.nombre, request.legajo)
        if error:
            return self._fallar(context, grpc.StatusCode.INVALID_ARGUMENT, error, "CrearPersona")

        if REPOSITORIO is None:
            return self._sin_base(context, "CrearPersona", "no hay TP_REDIS_URL configurada")
        try:
            identificador = REPOSITORIO.crear(nombre, request.legajo)
        except BaseNoDisponible as e:
            return self._sin_base(context, "CrearPersona", e)

        if identificador is None:
            return self._fallar(context, grpc.StatusCode.ALREADY_EXISTS,
                                "el legajo ya está registrado", "CrearPersona")

        bitacora("CrearPersona", "OK", identificador)
        return pb.RespuestaPersona(
            servido_por=APP_NAME,
            persona=pb.Persona(id=identificador, nombre=nombre, legajo=request.legajo),
        )

    def _fallar(self, context, codigo, mensaje, rpc):
        """Registra el fallo y aborta el RPC con el código del contrato."""
        bitacora(rpc, codigo.name)
        context.abort(codigo, mensaje)

    def _sin_base(self, context, rpc, motivo):
        """UNAVAILABLE de los RPC de personas.

        No hay degradación posible: sin base no hay datos. Devolver una lista vacía
        sería peor que fallar, porque el cliente no podría distinguir "no hay
        personas cargadas" de "no pude leerlas". Los demás RPC siguen respondiendo
        normal: no dependen de la base.
        """
        print(f"[personas] la base no respondió: {motivo}")
        return self._fallar(context, grpc.StatusCode.UNAVAILABLE,
                            "base de datos no disponible", rpc)


def servir(puerto: int = 8080):
    # El proceso arranca con `docker run`, y ahí stdout no es una terminal: Python
    # lo bufferiza por bloques y el log queda vacío hasta juntar varios KB. Sin
    # esto, el banner y los avisos del apagado no aparecen cuando hacen falta, que
    # es justo mientras se mira el log para ver si el deploy salió bien.
    sys.stdout.reconfigure(line_buffering=True)

    servidor = grpc.server(futures.ThreadPoolExecutor(max_workers=WORKERS))
    pb_grpc.add_ServicioServicer_to_server(Servicio(), servidor)

    # Health checking estándar de gRPC, además del RPC Salud del contrato: es lo que
    # entienden el HEALTHCHECK del contenedor y las herramientas del balanceador.
    servicio_salud = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(servicio_salud, servidor)
    servicio_salud.set("", health_pb2.HealthCheckResponse.SERVING)
    servicio_salud.set("sdypp.Servicio", health_pb2.HealthCheckResponse.SERVING)

    # Reflection: permite que otro equipo llame al servicio con grpcurl sin tener
    # el .proto. Es lo que hace posible que el verificador cruzado nos pruebe sin
    # que le pasemos los stubs.
    reflection.enable_server_reflection(
        (pb.DESCRIPTOR.services_by_name["Servicio"].full_name,
         health_pb2.DESCRIPTOR.services_by_name["Health"].full_name,
         reflection.SERVICE_NAME),
        servidor,
    )

    servidor.add_insecure_port(f"0.0.0.0:{puerto}")
    servidor.start()

    print(f"Servidor gRPC en 0.0.0.0:{puerto} (PID: {os.getpid()}) (Arrancado: {ARRANCADO})")
    print(f"[instancia] {APP_NAME}@{CASA} host={HOST} version={VERSION} workers={WORKERS}")
    if REPOSITORIO is None:
        print("[personas] sin TP_REDIS_URL: los RPC de personas responden UNAVAILABLE")
    else:
        print(f"[personas] base compartida en {_url_sin_credenciales(REDIS_URL)}")
    print(f"[bitacora] {ARCHIVO_BITACORA}")

    apagado = threading.Event()

    def detener(signum, frame):
        nombre = signal.Signals(signum).name
        print(f"\n[ Graceful Shutdown ] Recibida {nombre}. Marcando NOT_SERVING y drenando...")
        # Primero se declara no-sana: el balanceador la saca de rotación y deja de
        # mandarle RPCs nuevos mientras todavía está atendiendo los que tiene.
        servicio_salud.enter_graceful_shutdown()
        apagado.set()

    signal.signal(signal.SIGINT, detener)
    signal.signal(signal.SIGTERM, detener)

    apagado.wait()
    # stop() deja de aceptar RPCs nuevos y espera hasta `grace` a los en curso: sin
    # eso, las peticiones que estaban a mitad de camino se cortarían justo durante
    # el deploy, que es cuando el servicio tiene que seguir respondiendo.
    servidor.stop(grace=10).wait()
    print("[ Graceful Shutdown ] Puerto liberado y servidor detenido exitosamente.")


if __name__ == "__main__":
    # Puerto por argumento o por variable de entorno PORT. El default es 8080,
    # igual que el de la App Java: es el mismo contrato de invocación. En el
    # despliegue el puerto real se pasa explícito.
    puerto = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8080))
    servir(puerto)
