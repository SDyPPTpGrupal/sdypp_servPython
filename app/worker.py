"""Worker consumidor de la cola replicada, para sdypp_servPython.

Implementa el contrato publicado en `docs/contrato-worker.md` del repo
`sdypp_balanceador` (rama `feature/desacople`): el worker habla DIRECTO con el
clúster de colas, nunca con el balanceador. Seed list, caché de master, un
salto de `421` y backoff mientras no hay líder — la misma máquina de estados
que describe `specs/queue-client-failover`.

Las cinco operaciones del contrato las resuelve la misma clase `Servicio` que
atiende gRPC (ver `app.py`): acá sólo cambia el transporte. Si cada camino
validara por su cuenta, la misma alta daría un código por un camino y otro por
el otro, y el contrato dejaría de valer apenas cambia el transporte.
"""

import http.client
import json
import logging
import random
import threading
import time
from urllib.parse import urlsplit

import contrato_pb2 as pb

logger = logging.getLogger(__name__)

BACKOFF_INICIAL = 0.1
BACKOFF_MAXIMO = 1.0


class DummyAbort(Exception):
    """Lo que levanta `DummyContext.abort()` — imita `grpc.RpcError` lo justo
    para que `Servicio` no note que no lo llamó un servidor gRPC de verdad."""

    def __init__(self, code, details):
        self.code = code
        self.details = details
        super().__init__(f"{code}: {details}")


class DummyContext:
    """Un `ServicerContext` de mentira. `Servicio` sólo usa dos cosas de él:
    el id de correlación (para la bitácora) y `abort()` para las validaciones
    del contrato."""

    def __init__(self, request_id: str):
        self._request_id = request_id

    def invocation_metadata(self):
        return [("x-request-id", self._request_id)]

    def abort(self, code, details):
        raise DummyAbort(code, details)


class ColaNoDisponible(Exception):
    """La cola no respondió, o respondió algo que no se entiende."""


def _normalizar(url):
    return (url or "").strip().rstrip("/")


class ClienteCola:
    """El cliente HTTP contra el clúster de colas.

    Todo lo que depende de la especificación del otro equipo está acá adentro:
    las rutas, el token, el redirect. El resto del worker habla de tareas, no
    de HTTP.

    Cuatro estados, ninguno especial (specs/queue-client-failover): sin
    master → master cacheado → un salto tras un 421 → sin líder (backoff).
    """

    def __init__(self, urls, token, consumidor, timeout=10.0):
        self.seeds = [_normalizar(u) for u in urls if _normalizar(u)]
        if not self.seeds:
            raise ValueError("la seed list de la cola está vacía")
        self.token = token
        self.consumidor = consumidor
        self.timeout = timeout
        self._master = None
        # Arranque desde un offset rotado: si N réplicas arrancan juntas, no
        # todas martillan el mismo nodo 0 de la seed list para descubrir.
        self._giro = random.randrange(len(self.seeds))

    # ------------------------------------------------------------- interno
    def _pedir(self, url, metodo, ruta, cuerpo=None, timeout=None):
        partes = urlsplit(url)
        con = http.client.HTTPConnection(partes.hostname, partes.port,
                                         timeout=timeout or self.timeout)
        try:
            cabeceras = {"Accept": "application/json"}
            crudo = None
            if cuerpo is not None:
                crudo = json.dumps(cuerpo).encode("utf-8")
                cabeceras["Content-Type"] = "application/json"
            if self.token:
                cabeceras["X-Cola-Token"] = self.token
            con.request(metodo, ruta, body=crudo, headers=cabeceras)
            r = con.getresponse()
            # Un 204 no trae cuerpo, y no hay que leerlo: con keep-alive los
            # bytes de más quedan en el buffer y salen como el próximo
            # request — el mismo motivo que ya documenta `servidor.py`.
            datos = {}
            bruto = r.read()
            if r.status != 204 and bruto:
                try:
                    datos = json.loads(bruto.decode("utf-8"))
                except ValueError:
                    datos = {}
            return r.status, datos
        except (OSError, http.client.HTTPException) as e:
            raise ColaNoDisponible(f"{url}: {e}") from e
        finally:
            con.close()

    def _descubrir(self):
        """Recorre la seed list buscando quién manda. None si no hay master."""
        n = len(self.seeds)
        for i in range(n):
            url = self.seeds[(self._giro + i) % n]
            try:
                codigo, salud = self._pedir(url, "GET", "/health", timeout=3.0)
            except ColaNoDisponible:
                continue
            if codigo != 200:
                continue
            if salud.get("rol") == "master":
                self._giro = (self._giro + i) % n
                return url
            conocido = _normalizar(salud.get("masterConocido"))
            if conocido and conocido in self.seeds:
                return conocido
        return None

    def _validar(self, url):
        """Un `421` trae la URL del master, y llegó por la red: sólo se sigue
        si ya está en la seed list. Sin esta regla, un nodo comprometido
        desviaría todo el tráfico del worker al host que quisiera."""
        u = _normalizar(url)
        return u if u and u in self.seeds else None

    def _llamar(self, metodo, ruta, cuerpo=None, presupuesto=30.0, timeout=None):
        """Resuelve el ruteo entero: descubrimiento, un salto de `421`,
        backoff mientras no hay líder. Devuelve `(codigo, datos)` ya
        definitivo — nunca un `421` crudo."""
        limite = time.monotonic() + presupuesto
        espera = BACKOFF_INICIAL
        while time.monotonic() < limite:
            if self._master is None:
                self._master = self._descubrir()
                if self._master is None:
                    time.sleep(min(espera, max(limite - time.monotonic(), 0)))
                    espera = min(espera * 2, BACKOFF_MAXIMO) * random.uniform(0.8, 1.2)
                    continue
            try:
                codigo, datos = self._pedir(self._master, metodo, ruta, cuerpo, timeout)
            except ColaNoDisponible:
                # Estado de entrega desconocido: no se reenvía, se redescubre.
                self._master = None
                continue
            if codigo == 421 and datos.get("error") == "no-soy-master":
                nuevo = self._validar(datos.get("master"))
                self._master = nuevo
                if nuevo:
                    continue          # un solo salto
                time.sleep(espera)
                espera = min(espera * 2, BACKOFF_MAXIMO)
                continue
            if codigo == 503 and datos.get("error") == "recuperando":
                time.sleep(0.2)
                continue
            return codigo, datos
        return 503, {"error": "el sistema de colas no responde"}

    # ------------------------------------------------------------------ API
    def tomar(self, espera_s):
        """El próximo pedido, o `None` si no hubo nada en `espera_s` segundos."""
        codigo, datos = self._llamar(
            "POST", "/pedidos/tomar", {"consumidor": self.consumidor, "espera": espera_s},
            presupuesto=espera_s + 15, timeout=espera_s + 10)
        if codigo == 200:
            return datos
        if codigo != 204:
            logger.warning("tomar devolvió %s: %s", codigo, datos)
        return None

    def responder(self, id, estado, contenido, app):
        """Devuelve la tarea resuelta. `True` si la cola la aceptó.

        Un `409` es "no reintentar", nunca "repetir la ejecución": puede ser
        que ya la contestó otra réplica, o que el balanceador no está
        recolectando. Ninguno de los dos se arregla insistiendo.
        """
        codigo, datos = self._llamar("POST", "/respuestas", {
            "id": id, "estado": estado, "contenido": contenido,
            "atendidoPor": self.consumidor, "app": app})
        if codigo == 202:
            return True
        if codigo == 409:
            logger.info("respuesta descartada por la cola: %s", datos)
            return False
        logger.warning("responder devolvió %s: %s", codigo, datos)
        return False

    def devolver(self, id):
        """Suelta un pedido sin resolver, al apagarse.

        Es una optimización, no una garantía: si esto no llega, el
        recuperador de la cola lo hace igual cuando vence la reserva, sólo
        más tarde.
        """
        try:
            self._llamar("POST", "/pedidos/devolver",
                        {"id": id, "consumidor": self.consumidor}, presupuesto=5)
        except Exception as e:                                  # noqa: BLE001
            logger.info("no se pudo devolver %s: %s", id, e)


class ConsumidorWorker(threading.Thread):
    """Hilo consumidor que pide tareas a la cola y las procesa localmente."""

    def __init__(self, cliente: ClienteCola, servicio_impl, app_nombre: str,
                 espera_s: int, numero: int = 1):
        super().__init__(name=f"worker-{app_nombre}-{numero}", daemon=True)
        self.cliente = cliente
        self.servicio = servicio_impl
        self.app_nombre = app_nombre
        self.espera_s = espera_s
        self.activo = True

    def run(self):
        while self.activo:
            try:
                tarea = self.cliente.tomar(self.espera_s)
                if not tarea:
                    continue
                if not self.activo:
                    # Se pidió apagar justo mientras esperábamos: soltarla sin
                    # empezarla es mejor que hacerla esperar a que venza la
                    # reserva (docs/contrato-worker.md, POST /pedidos/devolver).
                    self.cliente.devolver(tarea["id"])
                    continue
                self.procesar_y_responder(tarea)
            except Exception as e:                               # noqa: BLE001
                print(f"[{self.name}] Error en bucle de worker: {e}")
                time.sleep(1)

    def detener(self):
        """Deja de tomar tareas nuevas. La que ya está resolviendo la
        termina: cortarle el hilo a una escritura que ya llegó a la base
        perdería la respuesta de algo que sí ocurrió."""
        self.activo = False

    def procesar_y_responder(self, tarea: dict):
        pedido_id = tarea["id"]
        operacion = tarea["operacion"]
        parametros = tarea.get("parametros") or {}

        context = DummyContext(pedido_id)
        estado = "OK"
        cuerpo = {}

        try:
            if operacion == "GET /":
                res = self.servicio.Identidad(pb.IdentidadPedido(), context)
                cuerpo = {
                    "app": res.app,
                    "lenguaje": res.lenguaje,
                    "equipo": [
                        {"nombre": i.nombre, "apellido": i.apellido, "legajo": i.legajo}
                        for i in res.equipo
                    ],
                    "version": res.version,
                    "mensaje": res.mensaje,
                    "host": res.host,
                    "arrancado": res.arrancado,
                    "servidoPor": res.app,
                }
            elif operacion == "POST /echo":
                res = self.servicio.Echo(pb.PingPedido(ping=str(parametros.get("ping", ""))), context)
                cuerpo = {
                    "pong": res.pong,
                    "servidoPor": res.servido_por,
                    "version": res.version,
                }
            elif operacion == "GET /personas":
                res = self.servicio.ListarPersonas(pb.ListarPersonasPedido(), context)
                cuerpo = {
                    "servidoPor": res.servido_por,
                    "personas": [
                        {"id": p.id, "nombre": p.nombre, "legajo": p.legajo}
                        for p in res.personas
                    ],
                }
            elif operacion == "POST /personas":
                res = self.servicio.CrearPersona(
                    pb.NuevaPersona(nombre=str(parametros.get("nombre", "")),
                                   legajo=int(parametros.get("legajo") or 0)),
                    context,
                )
                cuerpo = {
                    "servidoPor": res.servido_por,
                    "persona": {
                        "id": res.persona.id,
                        "nombre": res.persona.nombre,
                        "legajo": res.persona.legajo,
                    },
                }
            else:
                # No está en la tabla de docs/contrato-worker.md. `GET /health`
                # no es una de las cuatro: lo contesta el balanceador solo,
                # nunca llega por acá. Cualquier otra cosa es un catálogo
                # desactualizado, no un motivo para tirar una excepción.
                estado = "UNIMPLEMENTED"
                cuerpo = {"error": f"operación desconocida: {operacion!r}"}
        except DummyAbort as e:
            # El nombre del código gRPC, tal cual — no se traduce a HTTP acá:
            # de eso se encarga el balanceador (docs/contrato-worker.md).
            estado = e.code.name
            cuerpo = {"error": e.details}
        except Exception as e:                                   # noqa: BLE001
            estado = "INTERNAL"
            cuerpo = {"error": str(e)}

        self.cliente.responder(pedido_id, estado, cuerpo, self.app_nombre)
