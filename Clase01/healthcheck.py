"""Chequeo de salud del contenedor.

Con gRPC no sirve un curl: no hay HTTP que consultar. Esto llama al servicio
estándar grpc.health.v1.Health contra la propia instancia y traduce el resultado a
un código de salida, que es lo que entiende el HEALTHCHECK de Docker.

Sale 0 si la instancia está SERVING, 1 en cualquier otro caso.
"""

import os
import sys

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

PUERTO = os.environ.get("PORT", "8080")

try:
    with grpc.insecure_channel(f"127.0.0.1:{PUERTO}") as canal:
        respuesta = health_pb2_grpc.HealthStub(canal).Check(
            health_pb2.HealthCheckRequest(service=""), timeout=2
        )
    sys.exit(0 if respuesta.status == health_pb2.HealthCheckResponse.SERVING else 1)
except Exception as e:
    print(f"health: {e}", file=sys.stderr)
    sys.exit(1)
