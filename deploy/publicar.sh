#!/usr/bin/env bash
#
# Publica una versión de la App Python al contenedor CI/CD.
#
# Es el paso anterior al deploy.sh: acá se construye la imagen y se sube; allá se
# distribuye a las casas. La frontera entre los dos es un archivo .tar.gz.
#
#   ./publicar.sh            # construir, probar, empaquetar y subir
#   ./publicar.sh --local    # todo menos subir, para probar sin molestar a nadie
#
# El build ocurre acá y no en el CI/CD a propósito: el contrato.proto se compila
# dentro del Dockerfile, así que un error de contrato revienta en esta terminal y
# no en un contenedor ajeno con el deploy ya arrancado.

set -euo pipefail

# --- Configuración ---------------------------------------------------------
# Todo sobreescribible por variable de entorno: en la demo no se edita el script.

IMAGEN="${IMAGEN:-sdypp-app-python}"
DESTINO_SSH="${DESTINO_SSH:-deploy-python@casa-juan}"
DIR_ENTRANTE="${DIR_ENTRANTE:-/bin/deploy/python}"

# El watcher del CI/CD mira este nombre. PENDIENTE de cerrar con Plataforma:
# si prefieren un nombre por versión en vez de uno fijo, se cambia acá y nada más.
NOMBRE_REMOTO="${NOMBRE_REMOTO:-artefacto.tar.gz}"

DIR_SALIDA="${DIR_SALIDA:-/tmp}"
INTENTOS_SALUD="${INTENTOS_SALUD:-20}"
ESPERA_SALUD="${ESPERA_SALUD:-2}"

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- Salida ----------------------------------------------------------------

paso()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info()  { printf '    %s\n' "$*"; }
aviso() { printf '\033[33m    ! %s\033[0m\n' "$*"; }
error() { printf '\033[31m!!! %s\033[0m\n' "$*" >&2; }

# --- Versión ---------------------------------------------------------------

calcular_tag() {
    local version commit sufijo=""
    version="$(grep -oP '(?<=^VERSION = )\d+' "$RAIZ/Clase01/app.py")"
    commit="$(git -C "$RAIZ" rev-parse --short HEAD 2>/dev/null || echo local)"

    # Si el working tree está sucio, el commit del tag describe algo que no es lo
    # que se está subiendo. Marcarlo evita el peor rato de la demo: un contenedor
    # que dice v2-abc123 corriendo código que no está en ningún lado.
    if [[ -n "$(git -C "$RAIZ" status --porcelain 2>/dev/null)" ]]; then
        sufijo="-sucio"
    fi

    echo "v${version}-${commit}${sufijo}"
}

# --- Pasos -----------------------------------------------------------------

construir() {
    paso "BUILD — construir la imagen"
    docker build -t "$IMAGEN:$TAG" "$RAIZ"
    info "$IMAGEN:$TAG ($(docker images --format '{{.Size}}' "$IMAGEN:$TAG"))"
}

probar() {
    paso "PROBAR — ¿la imagen arranca y se declara sana?"
    # Sin puertos publicados y sin base: alcanza para saber que el servidor gRPC
    # levanta y responde el health estándar. Los RPC de personas van a decir
    # UNAVAILABLE sin TP_REDIS_URL, que es la respuesta correcta del contrato.
    # Sin --rm a propósito: si el proceso se muere, Docker borraría el contenedor
    # y con él los logs, que son lo único que dice POR QUÉ se murió. Se limpia con
    # el trap, que corre tanto si sale bien como si sale mal.
    local contenedor estado=""
    contenedor="$(docker run -d "$IMAGEN:$TAG")"
    trap 'docker rm -f "$contenedor" >/dev/null 2>&1 || true' RETURN

    for ((i = 1; i <= INTENTOS_SALUD; i++)); do
        # tail -1 porque si el contenedor ya no está, inspect deja una línea vacía
        # antes de fallar y el estado saldría con un salto de línea adentro.
        estado="$(docker inspect --format '{{.State.Health.Status}}' "$contenedor" 2>/dev/null | tail -1)"
        [[ -z "$estado" ]] && estado="muerto"
        case "$estado" in
            healthy|unhealthy|muerto) break ;;
        esac
        sleep "$ESPERA_SALUD"
    done

    if [[ "$estado" != "healthy" ]]; then
        error "la imagen no se puso sana (estado: $estado). No se publica."
        info "últimas líneas del contenedor:"
        docker logs "$contenedor" 2>&1 | tail -15 | sed 's/^/      /' || true
        return 1
    fi

    info "sana — el servidor gRPC levanta y responde grpc.health.v1.Health"
}

empaquetar() {
    paso "EMPAQUETAR — imagen a archivo"
    # El tag viaja adentro del tar: el CI/CD lo recupera con `docker load` y no
    # hay que pasárselo por separado.
    docker save "$IMAGEN:$TAG" | gzip -1 > "$ARTEFACTO"
    info "$ARTEFACTO ($(du -h "$ARTEFACTO" | cut -f1))"
}

subir() {
    paso "SUBIR — dejar el artefacto en el CI/CD"
    # A un nombre temporal primero y después renombrar: el rename es atómico, así
    # que el watcher nunca ve un .tar.gz a medio copiar. Un scp directo sobre el
    # nombre final dispara el deploy con el archivo incompleto.
    scp -q "$ARTEFACTO" "$DESTINO_SSH:$DIR_ENTRANTE/entrante.tmp"
    ssh "$DESTINO_SSH" "mv $DIR_ENTRANTE/entrante.tmp $DIR_ENTRANTE/$NOMBRE_REMOTO"
    info "$DESTINO_SSH:$DIR_ENTRANTE/$NOMBRE_REMOTO"
    info "el watcher del CI/CD dispara el deploy solo"
}

# --- Punto de entrada ------------------------------------------------------

case "${1:-}" in
    --local) SUBIR=no ;;
    "")      SUBIR=si ;;
    *)
        awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"
        exit 2
        ;;
esac

TAG="$(calcular_tag)"
ARTEFACTO="$DIR_SALIDA/$IMAGEN-$TAG.tar.gz"

echo "Publicando $IMAGEN:$TAG"
[[ "$TAG" == *-sucio ]] && aviso "hay cambios sin commitear: el tag no describe ningún commit"

construir
probar
empaquetar

if [[ "$SUBIR" == "no" ]]; then
    paso "LISTO (modo --local, no se subió nada)"
    info "para desplegar a mano desde acá:"
    info "  TAG=$TAG ./deploy/deploy.sh desplegar casa-tomas"
    exit 0
fi

subir

paso "LISTO"
info "versión $TAG publicada"
