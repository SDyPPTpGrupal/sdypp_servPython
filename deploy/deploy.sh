#!/usr/bin/env bash
#
# Despliegue blue-green de la App Python, sin downtime y con vuelta atrás.
#
# Corre dentro del contenedor CI/CD del equipo Plataforma, que llega a las
# máquinas de cada casa por SSH sobre Tailscale. En el diagrama de la Etapa 2 es
# /bin/deploy/python/deploy.sh.
#
#   ./deploy.sh desplegar casa-tomas    # blue-green completo, aborta solo si falla
#   ./deploy.sh rollback  casa-tomas    # vuelve al color anterior
#   ./deploy.sh estado    casa-tomas    # qué corre hoy en ese nodo
#
# La idea en una línea: la versión nueva se levanta AL LADO de la que está
# sirviendo, en otro puerto. Sólo si pasa el chequeo de salud se le pide al
# balanceador que apunte ahí. La anterior queda viva, así volver atrás es un
# comando y no un deploy en reversa.

set -euo pipefail

# --- Configuración ---------------------------------------------------------
# Todo es sobreescribible por variable de entorno para no tener que tocar el
# script en la demo.

PUERTO_BLUE="${PUERTO_BLUE:-8080}"
PUERTO_GREEN="${PUERTO_GREEN:-8081}"

# Cuánto se espera a que la versión nueva se declare sana antes de abortar.
# El HEALTHCHECK de la imagen corre cada 10 s con 5 s de gracia inicial, así que
# el primer veredicto no llega antes de los ~10 s: 30 intentos de 2 s dan un
# minuto de margen, suficiente sin colgar la demo si algo salió mal.
INTENTOS_SALUD="${INTENTOS_SALUD:-30}"
ESPERA_SALUD="${ESPERA_SALUD:-2}"

# Directorio del proyecto en la máquina destino. El .env con TP_REDIS_URL vive
# ahí y el deploy NO lo toca: es del nodo, no del release.
DIR_REMOTO="${DIR_REMOTO:-\$HOME/sdypp}"

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR_ESTADO="${DIR_ESTADO:-$(dirname "${BASH_SOURCE[0]}")/estado}"

# --- Salida ----------------------------------------------------------------

paso()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info()  { printf '    %s\n' "$*"; }
error() { printf '\033[31m!!! %s\033[0m\n' "$*" >&2; }

# --- Estado ----------------------------------------------------------------
# Sin esto el rollback no sabe a dónde volver. Se guarda en el CI/CD, que es
# quien orquesta: un archivo por nodo.

archivo_estado() { echo "$DIR_ESTADO/$1.env"; }

color_activo() {
    local archivo; archivo="$(archivo_estado "$1")"
    [[ -f "$archivo" ]] && grep -oP '(?<=^COLOR_ACTIVO=).*' "$archivo" || echo "blue"
}

guardar_estado() {
    mkdir -p "$DIR_ESTADO"
    cat > "$(archivo_estado "$1")" <<EOF
COLOR_ACTIVO=$2
COLOR_ANTERIOR=$3
VERSION=$4
DESPLEGADO=$(date -Iseconds)
EOF
}

puerto_de() { [[ "$1" == "blue" ]] && echo "$PUERTO_BLUE" || echo "$PUERTO_GREEN"; }
opuesto_de() { [[ "$1" == "blue" ]] && echo "green" || echo "blue"; }

# --- Lo que depende del balanceador ----------------------------------------
# PENDIENTE: Plataforma todavía no definió cómo se le pide al balanceador que
# cambie de destino (es el paso 5 del blue-green, y en el diagrama de la Etapa 2
# es la flecha que falta entre el CI/CD y el BA).
#
# Todo lo demás del script no depende de esto: cuando lo definan se completan
# estas dos funciones y no se toca nada más. Las tres opciones sobre la mesa:
#
#   1. Endpoint de administración en el BA  -> un curl desde acá. Es la mejor:
#      no necesita acceso a la máquina de Plataforma.
#   2. Archivo de configuración + SIGHUP    -> exige acceso al filesystem del BA.
#   3. Descubrimiento automático por health -> rompe el blue-green: la verde
#      entraría en rotación antes de que la verifiquemos.

BALANCEADOR="${BALANCEADOR:-}"

conmutar_a() {
    local nodo="$1" puerto="$2"
    if [[ -z "$BALANCEADOR" ]]; then
        error "No hay BALANCEADOR configurado: no se puede conmutar."
        info  "La versión nueva quedó levantada y sana en $nodo:$puerto, pero el"
        info  "tráfico sigue yendo a la anterior. Falta cerrar con Plataforma"
        info  "cómo se le pide el cambio de destino."
        return 1
    fi
    # TODO(Plataforma): reemplazar por el mecanismo que definan.
    curl -fsS -X POST "$BALANCEADOR/admin/backend" \
         -H 'Content-Type: application/json' \
         -d "{\"backend\": \"$nodo:$puerto\"}" >/dev/null
}

destino_actual() {
    # Poder LEER a quién está apuntando el balanceador, no sólo cambiarlo: si el
    # rollback confía sólo en nuestro archivo de estado, alcanza con que alguien
    # haya conmutado a mano para que volvamos al backend equivocado.
    [[ -n "$BALANCEADOR" ]] && curl -fsS "$BALANCEADOR/admin/backend" || true
}

# --- Pasos del pipeline ----------------------------------------------------

version_del_fuente() {
    grep -oP '(?<=^VERSION = )\d+' "$RAIZ/Clase01/app.py"
}

build() {
    paso "BUILD — imagen de la App Python"
    # El .proto se compila dentro del Dockerfile (etapa builder), así que un
    # error de sintaxis en el contrato o en el código revienta acá, en el CI/CD,
    # y no en la máquina de una casa con el deploy a medio hacer.
    docker build -t "sdypp-app-python:$TAG" "$RAIZ"
    info "sdypp-app-python:$TAG"
}

ship() {
    local nodo="$1"
    paso "SHIP — llevar la imagen y el compose a $nodo"
    # Se manda la imagen ya construida, no el código: así las cuatro réplicas
    # corren exactamente el mismo binario y ninguna máquina tiene que compilar.
    # Comprimida porque son unos 200 MB por la red de casa.
    docker save "sdypp-app-python:$TAG" | gzip -1 \
        | ssh "$nodo" "gunzip | docker load" >/dev/null
    ssh "$nodo" "mkdir -p $DIR_REMOTO/logs/blue $DIR_REMOTO/logs/green"
    scp -q "$RAIZ/docker-compose.nodo.yml" "$nodo:$DIR_REMOTO/"
    info "imagen cargada en $nodo"
}

levantar() {
    local nodo="$1" color="$2" puerto="$3"
    paso "ARRIBA — levantar la $color en $nodo:$puerto"
    # Proyecto de compose propio por color: los dos contenedores conviven en la
    # misma máquina sin pisarse el nombre. La que está sirviendo no se toca.
    ssh "$nodo" "cd $DIR_REMOTO && \
        COLOR=$color PUERTO=$puerto CASA=$nodo TAG=$TAG \
        docker compose --env-file .env -f docker-compose.nodo.yml -p sdypp-$color up -d" >/dev/null
    info "contenedor sdypp-$color-app-1 arriba"
}

bajar() {
    local nodo="$1" color="$2"
    # Las mismas variables que en `levantar`: el compose las interpola también
    # para bajar, y sin ellas falla con "falta TAG" en vez de borrar nada. Si esto
    # se traga el error, un deploy abortado deja el contenedor roto dando vueltas.
    ssh "$nodo" "cd $DIR_REMOTO && \
        COLOR=$color PUERTO=$(puerto_de "$color") CASA=$nodo TAG=$TAG \
        docker compose --env-file .env -f docker-compose.nodo.yml -p sdypp-$color down" \
        >/dev/null || error "no se pudo bajar la $color en $nodo: revisar a mano"
}

verificar_salud() {
    local nodo="$1" color="$2" version_esperada="$3"
    paso "VERIFY — ¿la $color está sana y es la versión nueva?"

    # No hace falta un cliente gRPC en el CI/CD: el HEALTHCHECK de la imagen ya
    # consulta grpc.health.v1.Health desde adentro del contenedor, y Docker
    # guarda el veredicto. Acá sólo se lo pregunta.
    local estado
    for ((i = 1; i <= INTENTOS_SALUD; i++)); do
        estado="$(ssh "$nodo" \
            "docker inspect --format '{{.State.Health.Status}}' sdypp-$color-app-1 2>/dev/null" \
            || echo "sin-contenedor")"
        case "$estado" in
            healthy) info "sana tras $((i * ESPERA_SALUD))s"; break ;;
            unhealthy) error "el contenedor se declaró unhealthy"; return 1 ;;
        esac
        [[ $i -eq $INTENTOS_SALUD ]] && { error "no se puso sana en $((INTENTOS_SALUD * ESPERA_SALUD))s (último estado: $estado)"; return 1; }
        sleep "$ESPERA_SALUD"
    done

    # Un 'healthy' no alcanza: si el ship falló a medias, el contenedor puede
    # estar sano corriendo la versión ANTERIOR y daríamos por bueno un deploy
    # que nunca subió.
    local version_real
    version_real="$(ssh "$nodo" "docker logs sdypp-$color-app-1 2>&1 | grep -oP '(?<=version=)\\d+' | head -1")"
    if [[ "$version_real" != "$version_esperada" ]]; then
        error "la $color responde version=$version_real y se esperaba $version_esperada"
        return 1
    fi
    info "version=$version_real, la que se acaba de desplegar"
}

# --- Comandos --------------------------------------------------------------

desplegar() {
    local nodo="$1"
    local activo nuevo puerto_nuevo version
    activo="$(color_activo "$nodo")"
    nuevo="$(opuesto_de "$activo")"
    puerto_nuevo="$(puerto_de "$nuevo")"
    version="$(version_del_fuente)"

    echo "Nodo $nodo · sirviendo $activo · se despliega $nuevo en :$puerto_nuevo · version $version"

    build
    ship "$nodo"
    levantar "$nodo" "$nuevo" "$puerto_nuevo"

    if ! verificar_salud "$nodo" "$nuevo" "$version"; then
        paso "ABORTA — se baja la $nuevo y NO se conmuta"
        bajar "$nodo" "$nuevo"
        # La que estaba sirviendo nunca se tocó: para un cliente no pasó nada.
        error "Deploy abortado. Sigue sirviendo la $activo y nadie vio la versión rota."
        return 1
    fi

    paso "CONMUTAR — mandar el tráfico a la $nuevo"
    if ! conmutar_a "$nodo" "$puerto_nuevo"; then
        return 1
    fi
    info "el balanceador ahora apunta a $nodo:$puerto_nuevo"

    guardar_estado "$nodo" "$nuevo" "$activo" "$version"

    paso "LISTO"
    info "Sirviendo la $nuevo (version $version) en :$puerto_nuevo"
    info "La $activo sigue viva en :$(puerto_de "$activo") — el rollback es un comando"
}

rollback() {
    local nodo="$1"
    local activo anterior puerto_anterior
    activo="$(color_activo "$nodo")"
    anterior="$(opuesto_de "$activo")"
    puerto_anterior="$(puerto_de "$anterior")"

    paso "ROLLBACK — volver a la $anterior en $nodo:$puerto_anterior"

    # La anterior sigue corriendo, así que esto es sólo cambiar el destino: no
    # hay que reconstruir, ni copiar, ni levantar nada.
    local estado
    estado="$(ssh "$nodo" "docker inspect --format '{{.State.Health.Status}}' sdypp-$anterior-app-1 2>/dev/null" || echo "sin-contenedor")"
    if [[ "$estado" != "healthy" ]]; then
        error "la $anterior no está sana ($estado): no hay a dónde volver"
        return 1
    fi

    conmutar_a "$nodo" "$puerto_anterior"
    guardar_estado "$nodo" "$anterior" "$activo" "$(version_del_fuente)"
    info "el tráfico volvió a la $anterior"
}

estado() {
    local nodo="$1"
    local activo; activo="$(color_activo "$nodo")"
    echo "── $nodo ──"
    echo "   color activo según el CI/CD: $activo"
    echo "   destino del balanceador:     $(destino_actual || echo '(no configurado)')"
    ssh "$nodo" "docker ps --filter name=sdypp- --format '   {{.Names}}\t{{.Status}}\t{{.Ports}}'" 2>/dev/null \
        || echo "   (no se pudo consultar el nodo)"
}

# --- Punto de entrada ------------------------------------------------------

comando="${1:-}"
nodo="${2:-}"

if [[ -z "$comando" || -z "$nodo" ]]; then
    grep '^#' "${BASH_SOURCE[0]}" | sed -n '2,20p' | sed 's/^# \?//'
    exit 2
fi

TAG="v$(version_del_fuente)-$(git -C "$RAIZ" rev-parse --short HEAD 2>/dev/null || echo local)"

case "$comando" in
    desplegar) desplegar "$nodo" ;;
    rollback)  rollback  "$nodo" ;;
    estado)    estado    "$nodo" ;;
    *) error "comando desconocido: $comando"; exit 2 ;;
esac
