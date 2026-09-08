#!/usr/bin/env bash
#
# Despliegue blue-green de la App Python, en un comando.
#
#   ./deploy/deploy.sh desplegar   # build, blue-green y conmutación
#   ./deploy/deploy.sh rollback    # vuelve a la versión anterior
#   ./deploy/deploy.sh estado      # qué corre en esta casa
#
# CADA CASA DESPLIEGA SU PROPIA RÉPLICA, en su propia máquina. No hay SSH, no hay
# servidor de despliegue, no hay artefactos que viajen: el script construye la
# imagen acá, levanta la versión nueva al lado de la que sirve y le avisa al
# balanceador.
#
# Que no haya SSH no es una simplificación: es la decisión de seguridad más
# grande del pipeline. Si nadie despliega en la máquina de otro, ninguna casa
# necesita tener la llave de las demás, y una credencial filtrada no compromete
# al grupo entero. Lo único que cada casa expone al tailnet es su réplica.
#
# La versión nueva se levanta AL LADO de la que sirve, en otro puerto. Sólo si
# queda sana se le pide al balanceador que cambie de destino. Si falla, no se
# conmuta y la vieja sigue sirviendo: nadie llega a ver la versión rota. Y la
# vieja no se baja al terminar, así volver atrás es un comando y no un deploy en
# reversa.
#
# El precio de que cada casa construya lo suyo: dos casas pueden terminar con
# imágenes distintas de la misma versión (otro pull de la imagen base, otra
# caché, otro momento). Por eso el tag lleva el commit, que es lo único que
# después permite comparar de qué fuente salió cada réplica.

set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- Configuración ---------------------------------------------------------
# Todo tiene default: el comando corre sin pasarle nada. Lo único que conviene
# fijar por casa es CASA, que es la etiqueta que sale en la bitácora.

# El nombre de esta casa. Sale en cada línea de la bitácora de la réplica y da
# nombre al archivo de estado. Si no se pasa, se deriva del hostname: funciona,
# pero queda feo, así que conviene ponerlo.
CASA="${CASA:-casa-$(hostname -s | tr '[:upper:]' '[:lower:]')}"

# Blue-green por puerto en la misma máquina: la que sirve y la que se está
# probando conviven, que es lo que hace posible el rollback instantáneo.
PUERTO_BLUE="${PUERTO_BLUE:-8080}"
PUERTO_GREEN="${PUERTO_GREEN:-8081}"

# Cuánto se espera a que la versión nueva se declare sana antes de abortar. El
# HEALTHCHECK de la imagen corre cada 10 s con 5 s de gracia, así que el primer
# veredicto no llega antes de los ~10 s: 30 intentos de 2 s dan un minuto de
# margen, suficiente sin colgar la demo si algo salió mal.
INTENTOS_SALUD="${INTENTOS_SALUD:-30}"
ESPERA_SALUD="${ESPERA_SALUD:-2}"

# El directorio de la casa: el .env con TP_REDIS_URL y las bitácoras. Vive fuera
# del repo porque lleva la contraseña de la base, y el deploy nunca lo toca: es
# del nodo, no del release.
DIR_LOCAL="${DIR_LOCAL:-$HOME/sdypp}"

# Dónde se recuerda qué color sirve y con qué versión. Sin esto el rollback no
# sabe a dónde volver.
DIR_ESTADO="${DIR_ESTADO:-$RAIZ/deploy/estado}"

IMAGEN="${IMAGEN:-sdypp-app-python}"

# El plano de control del balanceador. Es lo único que este script necesita de
# afuera; si no está, el deploy hace todo menos conmutar y lo dice.
BALANCEADOR="${BALANCEADOR:-}"

# --- Salida ----------------------------------------------------------------

paso()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info()  { printf '    %s\n' "$*"; }
aviso() { printf '\033[33m    ! %s\033[0m\n' "$*"; }
error() { printf '\033[31m!!! %s\033[0m\n' "$*" >&2; }

# --- Estado ----------------------------------------------------------------

archivo_estado() { echo "$DIR_ESTADO/$CASA.env"; }

leer_estado() {
    local archivo; archivo="$(archivo_estado)"
    [[ -f "$archivo" ]] && grep -oP "(?<=^$1=).*" "$archivo" || echo "${2:-}"
}

color_activo()  { leer_estado COLOR_ACTIVO blue; }
version_activa()   { leer_estado VERSION; }
version_anterior() { leer_estado VERSION_ANTERIOR; }

# Se guardan las DOS versiones, no sólo la que sirve, porque el rollback vuelve a
# un color que sigue vivo al lado: si no supiéramos qué versión corre ese color,
# después de volver atrás el estado diría que sirve una que ya no sirve.
guardar_estado() {
    mkdir -p "$DIR_ESTADO"
    cat > "$(archivo_estado)" <<EOF
COLOR_ACTIVO=$1
COLOR_ANTERIOR=$2
VERSION=$3
VERSION_ANTERIOR=$4
DESPLEGADO=$(date -Iseconds)
EOF
}

puerto_de()  { [[ "$1" == "blue" ]] && echo "$PUERTO_BLUE" || echo "$PUERTO_GREEN"; }
opuesto_de() { [[ "$1" == "blue" ]] && echo "green" || echo "blue"; }
contenedor_de() { echo "sdypp-$1-app-1"; }

# --- El balanceador --------------------------------------------------------

# La dirección con la que esta casa se anuncia. La sabe sola: es su IP del
# tailnet. Anunciarse con el nombre de la casa no sirve —no resuelve por DNS— y
# el síntoma es de los peores: la réplica entra al pool y el balanceador nunca
# logra chequearla, aunque esté perfectamente sana.
direccion_propia() {
    local propia
    propia="$(tailscale ip -4 2>/dev/null | head -1 || true)"
    if [[ -z "$propia" ]]; then
        error "no pude averiguar la IP de Tailscale de esta casa."
        info  "¿Está corriendo? 'tailscale status'. Si sabés cuál es: DIRECCION=100.x.y.z $0 ..."
        return 1
    fi
    echo "$propia"
}

conmutar_a() {
    local nuevo="$1" viejo="$2"
    if [[ -z "$BALANCEADOR" ]]; then
        error "No hay BALANCEADOR configurado: no se puede conmutar."
        info  "La versión nueva quedó levantada y sana, pero el tráfico sigue"
        info  "yendo a la anterior. Volvé a correrlo con BALANCEADOR=http://IP:PUERTO"
        return 1
    fi
    # Primero se agrega y después se quita, en ese orden: al revés hay un instante
    # con menos réplicas en rotación de las que debería haber.
    curl -fsS -X POST "$BALANCEADOR/admin/backends" \
         -H 'Content-Type: application/json' \
         -d "{\"agregar\": [\"$nuevo\"], \"quitar\": [\"$viejo\"]}" >/dev/null
}

destino_actual() {
    # Poder LEER a quién apunta el balanceador, no sólo cambiarlo: si el rollback
    # confiara sólo en nuestro archivo de estado, alcanzaría con que alguien haya
    # conmutado a mano para que volvamos al backend equivocado.
    [[ -n "$BALANCEADOR" ]] && curl -fsS "$BALANCEADOR/admin/backends" || true
}

# --- La imagen -------------------------------------------------------------

# Versión del contrato + commit. El commit importa porque cada casa construye su
# propia imagen: es lo único que después permite comparar si dos réplicas
# salieron del mismo fuente.
calcular_tag() {
    local version commit sufijo=""
    version="$(grep -oP '(?<=^VERSION = )\d+' "$RAIZ/app/app.py")"
    commit="$(git -C "$RAIZ" rev-parse --short HEAD 2>/dev/null || echo local)"
    # Marcar el working tree sucio evita el peor rato de la demo: un contenedor
    # que dice v2-abc123 corriendo código que no está en ningún commit.
    [[ -n "$(git -C "$RAIZ" status --porcelain 2>/dev/null)" ]] && sufijo="-sucio"
    echo "v${version}-${commit}${sufijo}"
}

construir() {
    paso "BUILD — construir la imagen"
    TAG="${TAG:-$(calcular_tag)}"
    [[ "$TAG" == *-sucio ]] && aviso "hay cambios sin commitear: el tag no describe ningún commit"
    docker build -t "$IMAGEN:$TAG" "$RAIZ" >/dev/null
    info "$IMAGEN:$TAG ($(docker images --format '{{.Size}}' "$IMAGEN:$TAG"))"
}

version_del_tag() {
    local v="${TAG#v}"; v="${v%%-*}"
    [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo ""
}

# --- Los contenedores ------------------------------------------------------

levantar() {
    local color="$1" puerto="$2" contenedor; contenedor="$(contenedor_de "$color")"
    paso "ARRIBA — levantar la $color en :$puerto"

    # docker run y no compose: acá hay UN contenedor, sin red compartida ni
    # dependencias, así que un manifiesto sería una pieza más para mantener igual
    # en cada casa. Además el nombre lo fija --name y no lo deriva compose, que lo
    # arma distinto según su versión: todo el resto del script consulta este
    # nombre exacto.
    #
    # El nombre puede estar ocupado por un deploy anterior que se abortó, así que
    # se borra antes. Es el color que NO está sirviendo: no hay nada que perder.
    docker rm -f "$contenedor" >/dev/null 2>&1 || true

    mkdir -p "$DIR_LOCAL/logs/blue" "$DIR_LOCAL/logs/green"

    # Las -e explícitas pisan lo que venga en el --env-file, que es lo que se
    # quiere: HOST_NAME lleva el color y el .env sólo aporta TP_REDIS_URL.
    #
    # --stop-timeout 15 tiene que ser mayor que el grace del servidor, o llega el
    # SIGKILL en medio del drenado de las peticiones en vuelo.
    # El `if` explícito no es adorno: esta función se llama desde una condición
    # (`if ! levantar ...`), y ahí bash desactiva `set -e`. Sin esto, un docker
    # run que falla no aborta nada: la función sigue, informa "arriba" y el deploy
    # se va a VERIFY a esperar un minuto por un contenedor que no existe.
    if ! docker run -d \
        --name "$contenedor" \
        --restart unless-stopped \
        --stop-timeout 15 \
        -p "$puerto:8080" \
        --env-file "$DIR_LOCAL/.env" \
        -e "HOST_NAME=$CASA-$color" \
        -e "CASA=$CASA" \
        -v "$DIR_LOCAL/logs/$color:/app/logs" \
        "$IMAGEN:$TAG" >/dev/null
    then
        error "no se pudo levantar $contenedor en :$puerto"
        info  "si dice 'address already in use', ese puerto ya lo usa otra cosa"
        info  "en esta máquina: cambialo con PUERTO_BLUE / PUERTO_GREEN"
        return 1
    fi

    info "contenedor $contenedor arriba"
}

bajar() {
    local contenedor; contenedor="$(contenedor_de "$1")"
    docker rm -f "$contenedor" >/dev/null 2>&1 \
        || info "no había $1 que bajar"
}

salud_de() {
    # tail -1 porque si el contenedor no existe, inspect deja una línea vacía
    # antes de fallar y el estado saldría con un salto de línea adentro.
    local estado
    estado="$(docker inspect --format '{{.State.Health.Status}}' "$(contenedor_de "$1")" 2>/dev/null | tail -1)"
    echo "${estado:-sin-contenedor}"
}

verificar_salud() {
    local color="$1" version_esperada="$2" contenedor; contenedor="$(contenedor_de "$color")"
    paso "VERIFY — ¿la $color está sana y es la versión nueva?"

    # No hace falta un cliente gRPC: el HEALTHCHECK de la imagen ya consulta
    # grpc.health.v1.Health desde adentro del contenedor y Docker guarda el
    # veredicto. Acá sólo se lo pregunta.
    local estado i
    for ((i = 1; i <= INTENTOS_SALUD; i++)); do
        estado="$(salud_de "$color")"
        case "$estado" in
            healthy)   info "sana tras $((i * ESPERA_SALUD))s"; break ;;
            unhealthy) error "el contenedor se declaró unhealthy"; return 1 ;;
        esac
        if [[ $i -eq $INTENTOS_SALUD ]]; then
            error "no se puso sana en $((INTENTOS_SALUD * ESPERA_SALUD))s (último estado: $estado)"
            return 1
        fi
        sleep "$ESPERA_SALUD"
    done

    # Un 'healthy' no alcanza: si el build no rehizo lo que creíamos, el
    # contenedor puede estar sano corriendo la versión ANTERIOR y daríamos por
    # bueno un deploy que nunca cambió nada.
    if [[ -z "$version_esperada" ]]; then
        aviso "el tag $TAG no tiene versión numérica: sólo se verificó la salud"
        return 0
    fi
    local version_real
    version_real="$(docker logs "$contenedor" 2>&1 | grep -oP '(?<=version=)\d+' | head -1)"
    if [[ "$version_real" != "$version_esperada" ]]; then
        error "la $color responde version=$version_real y se esperaba $version_esperada"
        return 1
    fi
    info "version=$version_real, la que se acaba de desplegar"
}

# --- Comandos --------------------------------------------------------------

desplegar() {
    local activo nuevo puerto_nuevo version direccion

    activo="$(color_activo)"
    nuevo="$(opuesto_de "$activo")"
    puerto_nuevo="$(puerto_de "$nuevo")"
    direccion="${DIRECCION:-$(direccion_propia)}"

    if [[ ! -f "$DIR_LOCAL/.env" ]]; then
        error "no existe $DIR_LOCAL/.env"
        info  "es donde vive TP_REDIS_URL. Sin eso la réplica arranca sin base y"
        info  "los RPC de personas responden UNAVAILABLE para siempre."
        return 1
    fi

    construir
    version="$(version_del_tag)"

    echo
    echo "Casa: $CASA ($direccion)"
    echo "Sirviendo $activo · se despliega $nuevo en :$puerto_nuevo · $IMAGEN:$TAG"

    if ! levantar "$nuevo" "$puerto_nuevo" || ! verificar_salud "$nuevo" "$version"; then
        paso "ABORTA"
        bajar "$nuevo"
        error "Deploy abortado. Sigue sirviendo la $activo en :$(puerto_de "$activo")."
        error "Nadie vio la versión rota."
        return 1
    fi

    paso "CONMUTAR — mandar el tráfico a la $nuevo"
    info "agregar: $direccion:$puerto_nuevo"
    info "quitar:  $direccion:$(puerto_de "$activo")"
    conmutar_a "$direccion:$puerto_nuevo" "$direccion:$(puerto_de "$activo")" || return 1

    guardar_estado "$nuevo" "$activo" "$version" "$(version_activa)"

    paso "LISTO"
    info "Sirviendo la $nuevo (version $version) en :$puerto_nuevo"
    info "La $activo sigue viva en :$(puerto_de "$activo") — el rollback es un comando"
}

rollback() {
    local activo anterior puerto_anterior direccion estado

    activo="$(color_activo)"
    anterior="$(opuesto_de "$activo")"
    puerto_anterior="$(puerto_de "$anterior")"
    direccion="${DIRECCION:-$(direccion_propia)}"

    paso "ROLLBACK — volver a la $anterior en :$puerto_anterior"

    # La anterior sigue corriendo, así que esto es sólo cambiar el destino: no hay
    # que reconstruir, ni copiar, ni levantar nada. Pero se verifica que esté sana
    # antes de mandarle tráfico.
    estado="$(salud_de "$anterior")"
    if [[ "$estado" != "healthy" ]]; then
        error "no hay a dónde volver: la $anterior está '$estado'"
        return 1
    fi
    info "la $anterior está sana"

    conmutar_a "$direccion:$puerto_anterior" "$direccion:$(puerto_de "$activo")" || return 1

    # Las dos versiones se intercambian, igual que los colores: la que servía
    # pasa a ser la anterior y viceversa.
    guardar_estado "$anterior" "$activo" "$(version_anterior)" "$(version_activa)"
    info "el tráfico volvió a la $anterior"
}

estado() {
    echo "casa:           $CASA"
    echo "color activo:   $(color_activo)  (version $(version_activa))"
    echo "color anterior: $(opuesto_de "$(color_activo)")  (version $(version_anterior))"
    echo
    echo "contenedores:"
    docker ps --filter name=sdypp- --format '   {{.Names}}\t{{.Status}}\t{{.Ports}}'
    echo
    echo "backends del balanceador:"
    destino_actual | sed 's/^/   /' || echo "   (no configurado)"
}

# --- Punto de entrada ------------------------------------------------------

case "${1:-}" in
    desplegar) desplegar ;;
    rollback)  rollback ;;
    estado)    estado ;;
    *)
        # Corta en la primera línea que no es comentario: así la ayuda es la
        # cabecera completa y nada más, aunque la cabecera cambie de largo.
        awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"
        exit 2
        ;;
esac
