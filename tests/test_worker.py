import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app")))
import contrato_pb2 as pb
from worker import ConsumidorWorker, DummyContext


class TestWorker(unittest.TestCase):

    def test_dummy_context(self):
        ctx = DummyContext("req-123")
        meta = ctx.invocation_metadata()
        self.assertEqual(meta, [("x-request-id", "req-123")])

    def test_worker_procesar_echo(self):
        mock_servicio = MagicMock()
        mock_servicio.Echo.return_value = pb.PongRespuesta(
            pong="hola", servido_por="python", version=3
        )

        worker = ConsumidorWorker("http://localhost:8080", mock_servicio, "python", "casa-tomas")
        worker.responder_tarea = MagicMock()

        tarea = {
            "request_id": "req-1",
            "operacion": "POST /echo",
            "datos": {"ping": "hola"},
        }

        worker.procesar_y_responder(tarea)

        mock_servicio.Echo.assert_called_once()
        worker.responder_tarea.assert_called_once_with(
            "req-1", 200, {"pong": "hola", "servidoPor": "python", "version": 3}
        )

    def test_worker_procesar_identidad(self):
        mock_servicio = MagicMock()
        mock_servicio.Identidad.return_value = pb.Instancia(
            app="python",
            lenguaje="Python 3.12",
            equipo=[pb.Integrante(nombre="Tomás", apellido="Resnik", legajo=190168)],
            version=3,
            mensaje="hola mundo",
            host="host1",
            arrancado="2026-09-18T20:00:00-03:00",
        )

        worker = ConsumidorWorker("http://localhost:8080", mock_servicio, "python", "casa-tomas")
        worker.responder_tarea = MagicMock()

        tarea = {
            "request_id": "req-2",
            "operacion": "GET /",
            "datos": {},
        }

        worker.procesar_y_responder(tarea)

        mock_servicio.Identidad.assert_called_once()
        worker.responder_tarea.assert_called_once_with(
            "req-2",
            200,
            {
                "app": "python",
                "lenguaje": "Python 3.12",
                "equipo": [{"nombre": "Tomás", "apellido": "Resnik", "legajo": 190168}],
                "version": 3,
                "mensaje": "hola mundo",
                "host": "host1",
                "arrancado": "2026-09-18T20:00:00-03:00",
                "servidoPor": "python",
            },
        )


if __name__ == "__main__":
    unittest.main()
