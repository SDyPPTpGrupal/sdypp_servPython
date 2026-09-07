# Imagen de la App Python (contrato v2.0, gRPC).
#
# Multi-etapa: los stubs de Protobuf se generan en el build y grpcio-tools —que
# pesa más que todo lo demás junto— no viaja a la imagen final.

# --- etapa 1: generar los stubs desde contrato.proto ---
FROM python:3.13-slim AS builder

WORKDIR /build
COPY requirements-build.txt .
RUN pip install --no-cache-dir -r requirements-build.txt

COPY contrato.proto .
RUN python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. contrato.proto

# --- etapa 2: la imagen que corre ---
FROM python:3.13-slim

# Sin esto el log del contenedor queda bufferizado y `docker logs` no muestra nada
# hasta juntar varios KB, justo cuando se lo mira para ver si el deploy salió bien.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Argentina/Buenos_Aires

# tzdata: sin esto el contenedor corre en UTC y el campo `arrancado` sale con
# offset +00:00 mientras una app fuera de contenedor lo devuelve con -03:00. La
# zona es contrato (CONTRATO.md §1): las bitácoras de las tres casas se cruzan
# entre sí y con la del balanceador.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Las dependencias antes que el código: así un cambio en app.py no invalida la
# capa de pip y el build vuelve a tardar segundos en vez de minutos.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=builder /build/contrato_pb2.py /build/contrato_pb2_grpc.py ./
COPY Clase01/app.py Clase01/healthcheck.py ./

# Usuario sin privilegios: si alguien se escapa del proceso, no es root del
# contenedor.
#
# El uid es 1000 a propósito, no uno alto: la bitácora se escribe en un bind mount
# del disco del host (CONTRATO.md §9), y el uid de adentro tiene que coincidir con
# el del dueño de ese directorio afuera o el proceso no puede escribir. 1000 es el
# primer usuario en cualquier Linux de escritorio. Por eso el directorio se crea
# desde el host ANTES de levantar: si lo crea Docker, queda de root y no hay
# permiso.
RUN useradd --system --uid 1000 --user-group sdypp \
    && mkdir -p /app/logs \
    && chown -R sdypp:sdypp /app
USER sdypp

EXPOSE 8080

# No sirve un curl: se llama al servicio estándar de health de gRPC.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "healthcheck.py"]

# Docker manda SIGTERM al parar; el proceso lo atrapa, se declara NOT_SERVING y
# drena los RPC en vuelo. El --stop-timeout con que se lo levanta tiene que ser
# mayor que el grace del servidor, o llega el SIGKILL en medio del drenado.
STOPSIGNAL SIGTERM

CMD ["python", "app.py"]
