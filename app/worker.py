"""Worker consumidor de la cola del balanceador para sdypp_servPython."""

import logging
import threading
import time
import urllib.parse
import urllib.request
import json
import grpc

import contrato_pb2 as pb

logger = logging.getLogger(__name__)


class DummyAbort(Exception):
    def __init__(self, code, details):
        self.code = code
        self.details = details
        super().__init__(f"{code}: {details}")


class DummyContext:
    def __init__(self, request_id: str):
        self._request_id = request_id

    def invocation_metadata(self):
        return [("x-request-id", self._request_id)]

    def abort(self, code, details):
        raise DummyAbort(code, details)


CODIGOS_HTTP = {
    grpc.StatusCode.OK: 200,
    grpc.StatusCode.INVALID_ARGUMENT: 400,
    grpc.StatusCode.NOT_FOUND: 404,
    grpc.StatusCode.ALREADY_EXISTS: 409,
    grpc.StatusCode.PERMISSION_DENIED: 403,
    grpc.StatusCode.UNAUTHENTICATED: 401,
    grpc.StatusCode.DEADLINE_EXCEEDED: 504,
    grpc.StatusCode.UNAVAILABLE: 503,
}


class ConsumidorWorker(threading.Thread):
    """Hilo consumidor que pide tareas al balanceador y las procesa localmente."""

    def __init__(self, balanceador_url: str, servicio_impl, app_nombre: str, casa_nombre: str, numero: int = 1):
        super().__init__(name=f"worker-{app_nombre}-{numero}", daemon=True)
        self.balanceador_url = balanceador_url.rstrip("/")
        self.servicio = servicio_impl
        self.app_nombre = app_nombre
        self.casa_nombre = casa_nombre
        self.activo = True

    def run(self):
        while self.activo:
            try:
                tarea = self.pedir_tarea()
                if not tarea:
                    time.sleep(0.3)
                    continue

                self.procesar_y_responder(tarea)
            except Exception as e:
                print(f"[{self.name}] Error en bucle de worker: {e}")
                time.sleep(1)

    def pedir_tarea(self):
        url = f"{self.balanceador_url}/cola/pop"
        datos = json.dumps({"timeout": 2.0}).encode("utf-8")
        req = urllib.request.Request(
            url, data=datos, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read().decode("utf-8"))
                    return body.get("tarea")
        except Exception:
            return None
        return None

    def procesar_y_responder(self, tarea: dict):
        request_id = tarea["request_id"]
        operacion = tarea["operacion"]
        datos = tarea.get("datos", {})

        context = DummyContext(request_id)
        codigo_http = 200
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
            elif operacion == "GET /health":
                res = self.servicio.Salud(pb.SaludPedido(), context)
                cuerpo = {
                    "status": "SANO" if res.status == pb.EstadoSalud.SANO else "NO_SANO",
                    "app": res.app,
                    "version": res.version,
                }
            elif operacion == "POST /echo":
                ping_str = datos.get("ping", "")
                res = self.servicio.Echo(pb.PingPedido(ping=ping_str), context)
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
                    pb.NuevaPersona(nombre=str(datos.get("nombre", "")), legajo=int(datos.get("legajo", 0))),
                    context,
                )
                codigo_http = 201
                cuerpo = {
                    "servidoPor": res.servido_por,
                    "persona": {
                        "id": res.persona.id,
                        "nombre": res.persona.nombre,
                        "legajo": res.persona.legajo,
                    },
                }
            else:
                codigo_http = 404
                cuerpo = {"error": "operacion desconocida"}
        except DummyAbort as e:
            codigo_http = CODIGOS_HTTP.get(e.code, 500)
            cuerpo = {"error": e.details}
        except Exception as e:
            codigo_http = 500
            cuerpo = {"error": str(e)}

        self.responder_tarea(request_id, codigo_http, cuerpo)

    def responder_tarea(self, request_id: str, codigo_http: int, cuerpo: dict):
        url = f"{self.balanceador_url}/cola/completar"
        payload = {
            "request_id": request_id,
            "servido_por": f"{self.app_nombre}@{self.casa_nombre}",
            "codigo_http": codigo_http,
            "cuerpo": cuerpo,
        }
        datos = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=datos, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=4.0):
                pass
        except Exception as e:
            print(f"[{self.name}] Error enviando respuesta al balanceador: {e}")
