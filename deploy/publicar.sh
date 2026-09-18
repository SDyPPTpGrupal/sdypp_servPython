#!/usr/bin/env bash
#
# Publica una versión de la App Python: la construye, la prueba, la sube al
# registry de Datos y le avisa al CD con un manifiesto. El deploy en las casas
# lo hace el CD; acá no se toca ninguna casa ni el balanceador.
#
#   ./deploy/publicar.sh            # construir, probar, subir, avisar
#   ./deploy/publicar.sh --local    # construir y probar; no sube ni avisa
#
# El build ocurre acá y no en el CD a propósito: contrato.proto se compila dentro
# del Dockerfile, así que un error de contrato revienta en esta terminal y no en
# un contenedor ajeno con el deploy ya arrancado.
#
# Necesita: docker (con {"insecure-registries": ["100.91.228.65:5000"]} en
# /etc/docker/daemon.json), git, grpcurl, python3, y la clave ~/.ssh/id_deploy
# autorizada en el CD para deploy-python, con este Host en ~/.ssh/config:
#
#   Host cd
#       HostName 100.101.15.93
#       Port 2222
#       User deploy-python
#       IdentityFile ~/.ssh/id_deploy

set -euo pipefail

# --- Configuración ---------------------------------------------------------
# Todo sobreescribible por variable de entorno: en la demo no se edita el script.

REGISTRY="${REGISTRY:-100.91.228.65:5000}"
IMAGEN="${IMAGEN:-sdypp-app-python}"
CD_SSH="${CD_SSH:-cd}"
DIR_ENTRANTE="${DIR_ENTRANTE:-/cicd/python/entrante}"

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

leer_version() {
    grep -oP '(?<=^VERSION = )\d+' "$RAIZ/app/app.py"
}

calcular_tag() {
    local commit sufijo=""
    commit="$(git -C "$RAIZ" rev-parse --short=7 HEAD 2>/dev/null || echo local)"

    # Si el working tree está sucio, el commit del tag describe algo que no es lo
    # que se está subiendo. Marcarlo evita el peor rato de la demo: un contenedor
    # que dice v2-abc123 corriendo código que no está en ningún lado.
    if [[ -n "$(git -C "$RAIZ" status --porcelain 2>/dev/null)" ]]; then
        sufijo="-sucio"
    fi

    echo "v${VERSION}-${commit}${sufijo}"
}

# --- Pasos -----------------------------------------------------------------

comprobar_registry() {
    paso "REGISTRY — ¿responde $REGISTRY?"
    if ! curl -fsS --max-time 5 "http://$REGISTRY/v2/" >/dev/null; then
        error "el registry no responde. ¿Tailscale arriba? ¿Datos levantada? ($REGISTRY)"
        return 1
    fi
    # Dos publicaciones con la misma VERSION hacen que el CD no pueda distinguir
    # versión nueva de vieja cuando verifica Identidad.version en cada casa.
    local publicados
    publicados="$(curl -fsS --max-time 5 "http://$REGISTRY/v2/$IMAGEN/tags/list" 2>/dev/null \
        | python3 -c 'import sys,json; print(" ".join(t for t in (json.load(sys.stdin).get("tags") or []) if t.startswith(sys.argv[1] + "-")))' "v$VERSION" \
        2>/dev/null || true)"
    if [[ -n "$publicados" ]]; then
        aviso "ya hay una v$VERSION publicada ($publicados). Subí VERSION en app/app.py antes de publicar."
    else
        info "responde; v$VERSION todavía no está publicada"
    fi
}

construir() {
    paso "BUILD — construir la imagen"
    docker build -t "$NOMBRE" "$RAIZ"
    info "$NOMBRE ($(docker images --format '{{.Size}}' "$NOMBRE"))"
}

probar() {
    paso "PROBAR — ¿la imagen arranca, se declara sana y dice la versión que va en el manifiesto?"
    # Sin base: alcanza para saber que el servidor gRPC levanta y responde el
    # health estándar. Los RPC de personas dirían UNAVAILABLE, que es la respuesta
    # correcta del contrato. Publicado sólo en loopback y en un puerto que elige
    # Docker, para no chocar con lo que ya corre en esta máquina.
    # Sin --rm a propósito: si el proceso se muere, Docker borraría el contenedor
    # y con él los logs, que son lo único que dice POR QUÉ se murió. Se limpia con
    # el trap, que corre tanto si sale bien como si sale mal.
    local contenedor estado="" direccion version_real
    contenedor="$(docker run -d -p 127.0.0.1::8080 "$NOMBRE")"
    trap 'docker rm -f "$contenedor" >/dev/null 2>&1 || true' RETURN

    for ((i = 1; i <= INTENTOS_SALUD; i++)); do
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

    # La versión que declara la imagen tiene que ser la del manifiesto: es lo que
    # el CD compara en cada casa antes de conmutar.
    if ! command -v grpcurl >/dev/null; then
        aviso "sin grpcurl no se verifica Identidad.version; el CD lo va a verificar igual en cada casa"
        return 0
    fi
    direccion="$(docker port "$contenedor" 8080 | head -1)"
    version_real="$(grpcurl -plaintext -import-path "$RAIZ" -proto contrato.proto \
        "$direccion" sdypp.Servicio/Identidad \
        | python3 -c 'import sys,json; print(json.load(sys.stdin).get("version", 0))')"
    if [[ "$version_real" != "$VERSION" ]]; then
        error "la imagen dice version=$version_real y app/app.py dice $VERSION. No se publica."
        return 1
    fi
    info "Identidad.version = $VERSION"
}

subir() {
    paso "SUBIR — docker push al registry de Datos"
    docker push "$NOMBRE"
    # El digest identifica el contenido, no el nombre: el CD hace pull por digest,
    # así lo que se despliega es exactamente lo que se probó acá aunque alguien
    # vuelva a pushear el mismo tag.
    DIGEST="$(docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$NOMBRE" \
        | grep "^$REGISTRY/$IMAGEN@" | head -1 | cut -d@ -f2)"
    if [[ -z "$DIGEST" ]]; then
        error "no se pudo leer el digest después del push"
        return 1
    fi
    info "$NOMBRE"
    info "$DIGEST"
}

escribir_manifiesto() {
    paso "MANIFIESTO — lo que el CD va a desplegar"
    MANIFIESTO="$DIR_SALIDA/manifiesto-$IMAGEN-$TAG.json"
    # Formato de 00-COMUN.md §5.3: es contrato con el CD, no se le agregan campos.
    printf '{\n  "equipo": "python",\n  "imagen": "%s",\n  "digest": "%s",\n  "version": %s,\n  "commit": "%s",\n  "publicado_por": "%s",\n  "publicado_en": "%s"\n}\n' \
        "$NOMBRE" "$DIGEST" "$VERSION" "$COMMIT" "$(whoami)" "$(date -Iseconds)" > "$MANIFIESTO"
    sed 's/^/    /' "$MANIFIESTO"
}

avisar() {
    paso "AVISAR — dejar el manifiesto en el CD"
    # A un nombre temporal y después renombrar, en un solo ssh: el rename es
    # atómico y es lo que dispara al CD (mira el moved_to, nunca el close_write),
    # así que nunca ve un manifiesto a medio escribir. Un solo ssh con cat en vez
    # de scp + ssh: mismo resultado y no depende del subsistema sftp del CD.
    ssh -o ConnectTimeout=10 "$CD_SSH" \
        "cat > '$DIR_ENTRANTE/manifiesto.json.tmp' && mv '$DIR_ENTRANTE/manifiesto.json.tmp' '$DIR_ENTRANTE/manifiesto.json'" \
        < "$MANIFIESTO"
    info "$CD_SSH:$DIR_ENTRANTE/manifiesto.json"
    info "el CD dispara el deploy solo"
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

VERSION="$(leer_version)"
COMMIT="$(git -C "$RAIZ" rev-parse --short=7 HEAD 2>/dev/null || echo local)"
TAG="$(calcular_tag)"
NOMBRE="$REGISTRY/$IMAGEN:$TAG"
DIGEST=""
MANIFIESTO=""

echo "Publicando $NOMBRE"
[[ "$TAG" == *-sucio ]] && aviso "hay cambios sin commitear: el tag no describe ningún commit"

[[ "$SUBIR" == "si" ]] && comprobar_registry
construir
probar

if [[ "$SUBIR" == "no" ]]; then
    paso "LISTO (modo --local, no se subió nada)"
    info "para correrla acá:"
    info "  docker run -d --name sdypp-prueba -p 8090:8080 --env-file ~/sdypp/.env \\"
    info "      -e HOST_NAME=prueba -e CASA=casa-prueba $NOMBRE"
    exit 0
fi

subir
escribir_manifiesto
avisar

paso "LISTO"
info "imagen   $NOMBRE"
info "digest   $DIGEST"
info "versión  $VERSION (commit $COMMIT)"
info "el CD despliega en todas las casas, verifica y conmuta. Seguirlo: en Plataforma, docker logs -f sdypp-cd"
