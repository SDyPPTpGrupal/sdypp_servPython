"""Pruebas del worker de cola.

Dos capas, como el propio módulo:

* `TestDespacho` — `procesar_y_responder`, mockeando `Servicio` y `cliente`.
  Es la frontera entre el JSON de la cola y los mensajes del contrato.
* `TestClienteCola` — el cliente HTTP contra una cola falsa que sí implementa
  las rutas, el token y el `421` de `docs/contrato-worker.md`. No es una cola
  de mentira con semántica inventada (eso ya causó el problema que esto
  reemplaza): reproduce el contrato real lo justo para ejercitar el cliente.
"""

import http.server
import json
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app")))
import grpc
import contrato_pb2 as pb
from worker import ClienteCola, ColaNoDisponible, ConsumidorWorker, DummyAbort, DummyContext


class TestDummyContext(unittest.TestCase):

    def test_lleva_el_id_de_correlacion(self):
        ctx = DummyContext("req-123")
        self.assertEqual(ctx.invocation_metadata(), [("x-request-id", "req-123")])

    def test_abort_levanta_dummy_abort(self):
        ctx = DummyContext("req-1")
        with self.assertRaises(DummyAbort) as cm:
            ctx.abort(grpc.StatusCode.INVALID_ARGUMENT, "el legajo es obligatorio")
        self.assertEqual(cm.exception.code, grpc.StatusCode.INVALID_ARGUMENT)
        self.assertEqual(cm.exception.details, "el legajo es obligatorio")


class TestDespacho(unittest.TestCase):
    """`procesar_y_responder`: mapea `operacion` a un RPC de `Servicio` y arma
    `estado`/`contenido` — nunca un código HTTP, eso es trabajo del balanceador."""

    def worker_con(self, servicio):
        w = ConsumidorWorker(cliente=MagicMock(), servicio_impl=servicio,
                             app_nombre="python", espera_s=20)
        return w

    def test_echo(self):
        servicio = MagicMock()
        servicio.Echo.return_value = pb.PongRespuesta(pong="hola", servido_por="python", version=3)
        worker = self.worker_con(servicio)

        worker.procesar_y_responder({"id": "p1", "operacion": "POST /echo",
                                     "parametros": {"ping": "hola"}})

        servicio.Echo.assert_called_once()
        worker.cliente.responder.assert_called_once_with(
            "p1", "OK", {"pong": "hola", "servidoPor": "python", "version": 3}, "python")

    def test_identidad(self):
        servicio = MagicMock()
        servicio.Identidad.return_value = pb.Instancia(
            app="python", lenguaje="Python 3.12",
            equipo=[pb.Integrante(nombre="Tomás", apellido="Resnik", legajo=190168)],
            version=3, mensaje="hola mundo", host="host1",
            arrancado="2026-09-18T20:00:00-03:00")
        worker = self.worker_con(servicio)

        worker.procesar_y_responder({"id": "p2", "operacion": "GET /", "parametros": {}})

        servicio.Identidad.assert_called_once()
        _, args, _ = worker.cliente.responder.mock_calls[0]
        self.assertEqual(args[0], "p2")
        self.assertEqual(args[1], "OK")
        self.assertEqual(args[2]["servidoPor"], "python")

    def test_crear_persona(self):
        servicio = MagicMock()
        servicio.CrearPersona.return_value = pb.RespuestaPersona(
            servido_por="python", persona=pb.Persona(id=7, nombre="Ada", legajo=1815))
        worker = self.worker_con(servicio)

        worker.procesar_y_responder({"id": "p3", "operacion": "POST /personas",
                                     "parametros": {"nombre": "Ada", "legajo": 1815}})

        servicio.CrearPersona.assert_called_once()
        args = worker.cliente.responder.call_args.args
        self.assertEqual(args[1], "OK")
        self.assertEqual(args[2]["persona"], {"id": 7, "nombre": "Ada", "legajo": 1815})

    def test_un_abort_del_servicio_pasa_el_nombre_del_codigo_gRPC(self):
        """El contrato es explícito: no se traduce a HTTP acá."""
        servicio = MagicMock()

        def crear(*_a, **_k):
            raise DummyAbort(grpc.StatusCode.ALREADY_EXISTS, "el legajo ya está registrado")
        servicio.CrearPersona.side_effect = crear
        worker = self.worker_con(servicio)

        worker.procesar_y_responder({"id": "p4", "operacion": "POST /personas",
                                     "parametros": {"nombre": "Ada", "legajo": 1815}})

        args = worker.cliente.responder.call_args.args
        self.assertEqual(args[1], "ALREADY_EXISTS")
        self.assertEqual(args[2], {"error": "el legajo ya está registrado"})

    def test_operacion_desconocida_no_llama_al_servicio_ni_explota(self):
        """`GET /health` no está en la tabla del contrato: la cola nunca la
        encola. Cualquier operación fuera de catálogo se responde
        UNIMPLEMENTED, no una excepción — el pedido llegó bien, el problema
        es el catálogo."""
        servicio = MagicMock()
        worker = self.worker_con(servicio)

        worker.procesar_y_responder({"id": "p5", "operacion": "GET /health", "parametros": {}})

        servicio.Identidad.assert_not_called()
        args = worker.cliente.responder.call_args.args
        self.assertEqual(args[1], "UNIMPLEMENTED")

    def test_una_excepcion_inesperada_no_tira_el_hilo(self):
        servicio = MagicMock()
        servicio.Echo.side_effect = RuntimeError("la base está caída")
        worker = self.worker_con(servicio)

        worker.procesar_y_responder({"id": "p6", "operacion": "POST /echo",
                                     "parametros": {"ping": "x"}})

        args = worker.cliente.responder.call_args.args
        self.assertEqual(args[1], "INTERNAL")


# =====================================================================
# La cola falsa: implementa las rutas, el token y el 421 reales.
# =====================================================================
class ManejadorColaFalsa(http.server.BaseHTTPRequestHandler):
    """Un nodo mínimo que respeta docs/contrato-worker.md lo justo para
    ejercitar ClienteCola: /health, /pedidos/tomar, /respuestas,
    /pedidos/devolver, con X-Cola-Token y el 421 de redirección."""

    def log_message(self, *_a):
        pass

    def _cuerpo(self):
        largo = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(largo)) if largo else {}

    def _responder(self, codigo, cuerpo=None):
        if codigo == 204:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        crudo = json.dumps(cuerpo or {}).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(crudo)))
        self.end_headers()
        self.wfile.write(crudo)

    def do_GET(self):
        estado = self.server.estado
        if self.path == "/health":
            with estado["lock"]:
                soy_master = estado["master"] == estado["yo"]
                self._responder(200, {
                    "rol": "master" if soy_master else "slave",
                    "masterConocido": estado["master"],
                })
            return
        self._responder(404, {"error": "no existe"})

    def do_POST(self):
        estado = self.server.estado
        if estado["token"] and self.headers.get("X-Cola-Token") != estado["token"]:
            self._cuerpo()
            self._responder(403, {"error": "token inválido"})
            return
        with estado["lock"]:
            soy_master = estado["master"] == estado["yo"]
        if not soy_master:
            self._cuerpo()
            self._responder(421, {"error": "no-soy-master", "master": estado["master"]})
            return

        cuerpo = self._cuerpo()
        if self.path == "/pedidos/tomar":
            with estado["lock"]:
                estado["tomadas"].append(cuerpo)
                if estado["pendiente"]:
                    self._responder(200, estado["pendiente"].pop(0))
                    return
            self._responder(204)
            return
        if self.path == "/respuestas":
            with estado["lock"]:
                estado["respuestas"].append(cuerpo)
            self._responder(202, {"resultado": "entregada"})
            return
        if self.path == "/pedidos/devolver":
            with estado["lock"]:
                estado["devueltas"].append(cuerpo)
            self._responder(200, {"resultado": "devuelto"})
            return
        self._responder(404, {"error": "no existe"})


class ColaFalsa(http.server.ThreadingHTTPServer):
    def __init__(self, yo, token=""):
        super().__init__(("127.0.0.1", 0), ManejadorColaFalsa)
        self.estado = {
            "yo": yo, "master": yo, "token": token, "lock": threading.Lock(),
            "pendiente": [], "tomadas": [], "respuestas": [], "devueltas": [],
        }
        self.url = f"http://127.0.0.1:{self.server_port}"
        self.estado["yo"] = self.url
        self.estado["master"] = self.url

    def arrancar(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()


class TestClienteCola(unittest.TestCase):

    def setUp(self):
        self.cola = ColaFalsa(yo="self", token="t-consumidor")
        self.cola.arrancar()
        self.addCleanup(self.cola.shutdown)
        self.addCleanup(self.cola.server_close)
        self.cliente = ClienteCola([self.cola.url], "t-consumidor", "casa-tomas:8080", timeout=2.0)

    def test_tomar_cuando_no_hay_nada_devuelve_none(self):
        self.assertIsNone(self.cliente.tomar(0.2))

    def test_tomar_devuelve_el_pedido_y_manda_el_consumidor(self):
        self.cola.estado["pendiente"].append(
            {"id": "p1", "operacion": "GET /", "parametros": {}, "quedaMs": 5000, "intento": 1})

        pedido = self.cliente.tomar(0.2)

        self.assertEqual(pedido["id"], "p1")
        self.assertEqual(self.cola.estado["tomadas"][0]["consumidor"], "casa-tomas:8080")

    def test_token_invalido_no_hace_que_el_cliente_explote(self):
        cliente = ClienteCola([self.cola.url], "token-que-no-es", "casa-tomas:8080", timeout=2.0)
        self.assertIsNone(cliente.tomar(0.2))     # 403 se loguea, no se propaga

    def test_responder_manda_la_forma_exacta_del_contrato(self):
        ok = self.cliente.responder("p1", "OK", {"pong": "hola"}, "python")

        self.assertTrue(ok)
        enviado = self.cola.estado["respuestas"][0]
        self.assertEqual(enviado, {
            "id": "p1", "estado": "OK", "contenido": {"pong": "hola"},
            "atendidoPor": "casa-tomas:8080", "app": "python",
        })

    def test_devolver_manda_id_y_consumidor(self):
        self.cliente.devolver("p1")

        self.assertEqual(self.cola.estado["devueltas"][0],
                         {"id": "p1", "consumidor": "casa-tomas:8080"})

    def test_421_se_sigue_una_vez_y_no_llega_al_llamador(self):
        """El caso central de queue-client-failover: el nodo equivocado
        redirige, y el cliente reintenta en el master real sin que el
        llamador vea nunca el 421."""
        otra = ColaFalsa(yo="otra", token="t-consumidor")
        otra.arrancar()
        self.addCleanup(otra.shutdown)
        self.addCleanup(otra.server_close)

        # El primer nodo de la seed list no es el master; el segundo sí.
        with self.cola.estado["lock"]:
            self.cola.estado["master"] = otra.url
        otra.estado["pendiente"].append(
            {"id": "px", "operacion": "GET /", "parametros": {}, "quedaMs": 1000, "intento": 1})

        cliente = ClienteCola([self.cola.url, otra.url], "t-consumidor", "casa-tomas:8080", timeout=2.0)
        pedido = cliente.tomar(0.2)

        self.assertEqual(pedido["id"], "px")
        self.assertEqual(cliente._master, otra.url)

    def test_un_master_fuera_de_la_seed_list_se_ignora(self):
        """Seguridad (queue-client-failover): un 421 que apunta afuera de la
        seed list se trata como master: null, nunca se contacta ese host."""
        with self.cola.estado["lock"]:
            self.cola.estado["master"] = "http://evil.example:9999"

        cliente = ClienteCola([self.cola.url], "t-consumidor", "casa-tomas:8080", timeout=2.0)
        # Presupuesto corto: sin master válido, cae a 503 en vez de colgarse.
        codigo, datos = cliente._llamar("POST", "/pedidos/tomar",
                                        {"consumidor": "x", "espera": 0}, presupuesto=0.5)

        self.assertEqual(codigo, 503)
        self.assertIsNone(cliente._master)


if __name__ == "__main__":
    unittest.main()
