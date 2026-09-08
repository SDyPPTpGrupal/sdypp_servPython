#!/usr/bin/env bash
#
# Despliegue blue-green de la App Python, en un comando.
#
#   ./deploy.sh desplegar casa-meizers   # build, blue-green y conmutación
#   ./deploy.sh rollback  casa-meizers   # vuelve al color anterior
#   ./deploy.sh estado    casa-meizers   # qué corre en cada nodo
#
# CADA CASA DESPLIEGA SU PROPIA RÉPLICA. Se corre en la máquina de la casa, con
# CASA_LOCAL puesto a su nombre: ahí el script construye la imagen y maneja los
# contenedores localmente, sin SSH. Es lo que pide el enunciado —"deploy.sh: el
# pipeline de la Clase 1 en un comando"— y tiene una consecuencia grande de
# seguridad: si nadie despliega en la máquina de otro, no hace falta que ninguna
# casa tenga la llave de las demás.
#
# El modo remoto sigue estando: un nodo que no sea CASA_LOCAL se alcanza por SSH
# igual que antes. Sirve para desplegar varias casas de una, y es lo que usaba el
# contenedor CI/CD.
#
# La versión nueva se levanta AL LADO de la que sirve, en otro puerto. Sólo si
# TODAS quedan sanas se le pide al balanceador que cambie de destino. Si una sola
# falla, no se conmuta ninguna y las viejas siguen sirviendo: nadie llega a ver la
# versión rota.
#
# En paralelo y no de a un nodo por vez porque el deploy secuencial deja un rato
# con una sola réplica vieja en rotación: si esa se cae justo ahí, no queda nada
# sirviendo. Conmutando todo junto, en cada instante hay una versión completa.
#
# El precio de que cada casa construya lo suyo: dos casas pueden terminar con
# imágenes distintas de la misma versión (otro pull de la imagen base, otra
# caché). Por eso el tag lleva el commit, para poder comparar al menos qué fuente
# construyó cada una. En modo remoto eso no pasa: se manda la imagen ya armada.

set -euo pipefail

# --- Configuración ---------------------------------------------------------
# Todo sobreescribible por variable de entorno para no tener que tocar el
# script en la demo.

PUERTO_BLUE="${PUERTO_BLUE:-8080}"
PUERTO_GREEN="${PUERTO_GREEN:-8081}"

# Cuánto se espera a que una versión nueva se declare sana antes de abortar.
# El HEALTHCHECK de la imagen corre cada 10 s con 5 s de gracia inicial, así que
# el primer veredicto no llega antes de los ~10 s: 30 intentos de 2 s dan un
# minuto de margen, suficiente sin colgar la demo si algo salió mal.
INTENTOS_SALUD="${INTENTOS_SALUD:-30}"
ESPERA_SALUD="${ESPERA_SALUD:-2}"

# Directorio del proyecto en la máquina destino. El .env con TP_REDIS_URL vive
# ahí y el deploy NO lo toca: es del nodo, no del release.
DIR_REMOTO="${DIR_REMOTO:-\$HOME/sdypp}"

# Dónde deja publicar.sh el artefacto y dónde lo busca el watcher.
DIR_ENTRANTE="${DIR_ENTRANTE:-/bin/deploy/python}"
ARTEFACTO="${ARTEFACTO:-$DIR_ENTRANTE/artefacto.tar.gz}"

DIR_ESTADO="${DIR_ESTADO:-$(dirname "${BASH_SOURCE[0]}")/estado}"

# SSH con timeout. Sin esto, una casa apagada no da error: deja el deploy colgado
# esperando un TCP que nunca abre, y el operador no sabe si tarda o si murió.
# Con ConnectTimeout el nodo caído falla rápido y el todo-o-nada puede abortar.
# BatchMode evita que se quede pidiendo una passphrase que nadie va a escribir.
ESPERA_CONEXION="${ESPERA_CONEXION:-8}"
SSH_OPCIONES=(-o "ConnectTimeout=$ESPERA_CONEXION" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)

# El nombre de ESTA casa. Cuando el nodo a desplegar es ella, los comandos corren
# acá mismo en vez de salir por SSH: es la máquina propia, no hay red de por
# medio, y así no hace falta ninguna credencial.
#
# Vacío = todo es remoto, que es el comportamiento de antes.
CASA_LOCAL="${CASA_LOCAL:-}"

es_local() { [[ -n "$CASA_LOCAL" && "$1" == "$CASA_LOCAL" ]]; }

# La raíz del repo, para construir la imagen y leer la versión del contrato.
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Ejecuta un comando en un nodo, sea la casa propia o una ajena. Todas las
# llamadas del script pasan por acá, así el resto no se entera de la diferencia.
ssh() {
    local destino="$1"; shift
    if es_local "$destino"; then
        bash -c "$*"
    else
        command ssh "${SSH_OPCIONES[@]}" "$destino" "$@"
    fi
}

# --- Salida ----------------------------------------------------------------

paso()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info()  { printf '    %s\n' "$*"; }
aviso() { printf '\033[33m    ! %s\033[0m\n' "$*"; }
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
VERSION_ANTERIOR=$5
DESPLEGADO=$(date -Iseconds)
EOF
}

# Se guardan las dos versiones, no sólo la que sirve, porque el rollback vuelve a
# un color que sigue vivo al lado: si no supiéramos qué versión corre ese color,
# después de volver atrás el estado diría que sirve una versión que ya no sirve.
version_guardada() {
    local archivo; archivo="$(archivo_estado "$1")"
    [[ -f "$archivo" ]] && grep -oP '(?<=^VERSION=).*' "$archivo" || echo ""
}

version_anterior_guardada() {
    local archivo; archivo="$(archivo_estado "$1")"
    [[ -f "$archivo" ]] && grep -oP '(?<=^VERSION_ANTERIOR=).*' "$archivo" || echo ""
}

# El color es del despliegue, no de cada nodo: si los nodos estuvieran en
# colores distintos, "conmutar todo junto" no querría decir nada. Que diverjan
# es señal de que alguien intervino a mano, y ahí frenar es lo correcto.
color_activo_comun() {
    local primero="" nodo color
    for nodo in "$@"; do
        color="$(color_activo "$nodo")"
        if [[ -z "$primero" ]]; then
            primero="$color"
        elif [[ "$color" != "$primero" ]]; then
            error "los nodos no están en el mismo color (hay $primero y $color)."
            error "revisar a mano con: $0 estado $*"
            return 1
        fi
    done
    echo "$primero"
}

puerto_de() { [[ "$1" == "blue" ]] && echo "$PUERTO_BLUE" || echo "$PUERTO_GREEN"; }
opuesto_de() { [[ "$1" == "blue" ]] && echo "green" || echo "blue"; }

# --- Lo que depende del balanceador ----------------------------------------
# En la reunión con Plataforma se acordó que la conmutación es un endpoint HTTP
# privado del balanceador, con whitelist de la IP del CI/CD. Falta cerrar la
# forma exacta del pedido; cuando la definan se completan estas dos funciones y
# no se toca nada más.
#
# Ojo que en la Etapa 2 el balanceador ya no tiene UN destino sino una LISTA, así
# que conmutar es editarla: primero se agregan las nuevas y después se quitan las
# viejas. En ese orden, porque al revés hay un instante con menos réplicas en
# rotación.

BALANCEADOR="${BALANCEADOR:-}"

conmutar_a() {
    local nuevos="$1" viejos="$2"
    if [[ -z "$BALANCEADOR" ]]; then
        error "No hay BALANCEADOR configurado: no se puede conmutar."
        info  "Las versiones nuevas quedaron levantadas y sanas, pero el tráfico"
        info  "sigue yendo a las anteriores. Falta cerrar con Plataforma la forma"
        info  "del pedido de cambio de destino."
        return 1
    fi
    # TODO(Plataforma): reemplazar por el mecanismo que definan.
    curl -fsS -X POST "$BALANCEADOR/admin/backends" \
         -H 'Content-Type: application/json' \
         -d "{\"agregar\": [$nuevos], \"quitar\": [$viejos]}" >/dev/null
}

destino_actual() {
    # Poder LEER a quién está apuntando el balanceador, no sólo cambiarlo: si el
    # rollback confía sólo en nuestro archivo de estado, alcanza con que alguien
    # haya conmutado a mano para que volvamos al backend equivocado.
    [[ -n "$BALANCEADOR" ]] && curl -fsS "$BALANCEADOR/admin/backends" || true
}

# A qué dirección llega el balanceador para hablarle a una casa.
#
# Nosotros llamamos a los nodos por su nombre (casa-salvador), que es lo que va a
# la bitácora y a los archivos de estado, pero el balanceador abre un socket y
# necesita algo que resuelva. CASAS traduce lo uno en lo otro:
#
#   CASAS="casa-salvador=salvador@100.91.134.43 casa-mateon=mateo@100.78.246.64"
#
# Es la misma variable con la que el CI/CD arma su ~/.ssh/config, así que el
# usuario y la dirección de cada casa se declaran en un solo lugar. Sin mapa, el
# nombre del nodo ya se toma como dirección (que es como venía andando).
direccion_de() {
    local par
    for par in ${CASAS:-}; do
        if [[ "${par%%=*}" == "$1" ]]; then
            par="${par#*=}"
            echo "${par##*@}"
            return
        fi
    done

    # La casa propia sabe su dirección sin que se la digan: es su IP del tailnet.
    # Sin esto se anunciaría al balanceador con su nombre de casa, que no resuelve
    # por DNS, y entraría al pool como caída — el síntoma es una réplica que el
    # balanceador nunca logra chequear aunque esté perfectamente sana.
    if es_local "$1"; then
        local propia
        propia="$(tailscale ip -4 2>/dev/null | head -1 || true)"
        if [[ -n "$propia" ]]; then
            echo "$propia"
            return
        fi
        aviso "no pude averiguar la IP de Tailscale de esta casa; se anuncia como '$1'" >&2
    fi

    echo "$1"
}

# Arma la lista JSON de backends de un color, para el pedido de conmutación.
lista_backends() {
    local color="$1"; shift
    local puerto; puerto="$(puerto_de "$color")"
    local nodo salida=""
    for nodo in "$@"; do
        salida+="${salida:+, }\"$(direccion_de "$nodo"):$puerto\""
    done
    echo "$salida"
}

# --- El artefacto ----------------------------------------------------------

# El tag: versión del contrato + commit. El commit importa porque cada casa
# construye su propia imagen, así que es lo único que permite comparar después si
# dos réplicas salieron del mismo fuente.
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
    paso "BUILD — construir la imagen en esta casa"
    TAG="$(calcular_tag)"
    [[ "$TAG" == *-sucio ]] && aviso "hay cambios sin commitear: el tag no describe ningún commit"
    docker build -t "$IMAGEN:$TAG" "$RAIZ" >/dev/null
    info "$IMAGEN:$TAG ($(docker images --format '{{.Size}}' "$IMAGEN:$TAG"))"
}

cargar_artefacto() {
    # TAG por variable de entorno: sirve para reintentar un despliegue con una
    # imagen que ya está cargada, sin volver a construirla ni leer el .tar.gz.
    if [[ -n "${TAG:-}" ]]; then
        info "usando la imagen ya cargada: $IMAGEN:$TAG"
        return 0
    fi

    # Con CASA_LOCAL la imagen se construye acá: el fuente está en esta máquina y
    # no hay artefacto que esperar.
    if [[ -n "$CASA_LOCAL" ]]; then
        construir
        return 0
    fi

    if [[ ! -f "$ARTEFACTO" ]]; then
        error "no existe el artefacto $ARTEFACTO"
        info  "lo deja publicar.sh desde la máquina del desarrollador"
        return 1
    fi

    # El nombre y la versión viajan adentro del tar, así que no hay que pasarlos
    # por separado ni leer el código fuente: por eso el mismo script sirve para
    # Java sin cambiarle una línea.
    local cargada
    cargada="$(docker load -i "$ARTEFACTO" | grep -oP '(?<=Loaded image: ).*' | head -1)"
    if [[ -z "$cargada" ]]; then
        error "docker load no devolvió el nombre de la imagen"
        return 1
    fi
    IMAGEN="${cargada%:*}"
    TAG="${cargada##*:}"
    info "$IMAGEN:$TAG"
}

# La versión sale del tag (vN-commit), no de app/app.py: el CI/CD no tiene el fuente.
version_del_tag() {
    local v="${TAG#v}"
    v="${v%%-*}"
    [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo ""
}

# --- Pasos sobre un nodo ---------------------------------------------------

ship() {
    local nodo="$1"
    # Los directorios de la bitácora hacen falta siempre. El .env no se toca: es
    # del nodo, no del release, y lleva la contraseña de la base.
    ssh "$nodo" "mkdir -p $DIR_REMOTO/logs/blue $DIR_REMOTO/logs/green"

    if es_local "$nodo"; then
        paso "SHIP — nada que llevar: la imagen se construyó en esta casa"
        return 0
    fi

    paso "SHIP — llevar la imagen a $nodo"
    # Se manda la imagen ya construida, no el código: así las réplicas corren
    # exactamente el mismo binario y ninguna máquina de casa compila nada.
    # Comprimida porque son unos 200 MB por la red de casa.
    docker save "$IMAGEN:$TAG" | gzip -1 \
        | ssh "$nodo" "gunzip | docker load" >/dev/null
    info "imagen cargada en $nodo"
}

levantar() {
    local nodo="$1" color="$2" puerto="$3"
    paso "ARRIBA — levantar la $color en $nodo:$puerto"
    # docker run y no compose: en la casa hay UN contenedor, sin red compartida ni
    # dependencias, así que un manifiesto sería una pieza más para mantener igual
    # en cinco máquinas. Además el nombre lo fija --name y no lo deriva compose,
    # que lo arma distinto según su versión (sdypp-blue_app_1 en v1, con guiones
    # en v2): todo el resto del script consulta este nombre exacto.
    #
    # El nombre puede quedar ocupado por un deploy anterior que se abortó, así que
    # se borra antes. Es el color que NO está sirviendo: no hay nada que perder.
    #
    # Las -e explícitas pisan lo que venga en el --env-file, que es lo que se
    # quiere: HOST_NAME lleva el color y el .env sólo aporta TP_REDIS_URL.
    ssh "$nodo" "docker rm -f sdypp-$color-app-1 >/dev/null 2>&1; \
        docker run -d \
            --name sdypp-$color-app-1 \
            --restart unless-stopped \
            --stop-timeout 15 \
            -p $puerto:8080 \
            --env-file $DIR_REMOTO/.env \
            -e HOST_NAME=$nodo-$color \
            -e CASA=$nodo \
            -v $DIR_REMOTO/logs/$color:/app/logs \
            $IMAGEN:$TAG" >/dev/null
    info "contenedor sdypp-$color-app-1 arriba"
}

bajar() {
    local nodo="$1" color="$2"
    # En un aborto se baja el color nuevo en TODOS los nodos, incluidos los que no
    # llegaron a levantarlo. Ahí `docker rm` falla porque no hay nada que borrar, y
    # eso no es un problema: se distingue del fallo real en el mensaje.
    ssh "$nodo" "docker rm -f sdypp-$color-app-1" >/dev/null 2>&1 \
        || info "$nodo: no había $color que bajar"
}

verificar_salud() {
    local nodo="$1" color="$2" version_esperada="$3"
    paso "VERIFY — ¿la $color de $nodo está sana y es la versión nueva?"

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
            unhealthy) error "$nodo: el contenedor se declaró unhealthy"; return 1 ;;
        esac
        [[ $i -eq $INTENTOS_SALUD ]] && { error "$nodo: no se puso sana en $((INTENTOS_SALUD * ESPERA_SALUD))s (último estado: $estado)"; return 1; }
        sleep "$ESPERA_SALUD"
    done

    # Un 'healthy' no alcanza: si el ship falló a medias, el contenedor puede
    # estar sano corriendo la versión ANTERIOR y daríamos por bueno un deploy
    # que nunca subió.
    if [[ -z "$version_esperada" ]]; then
        aviso "el tag $TAG no tiene versión numérica: sólo se verificó la salud"
        return 0
    fi
    local version_real
    version_real="$(ssh "$nodo" "docker logs sdypp-$color-app-1 2>&1 | grep -oP '(?<=version=)\\d+' | head -1")"
    if [[ "$version_real" != "$version_esperada" ]]; then
        error "$nodo: la $color responde version=$version_real y se esperaba $version_esperada"
        return 1
    fi
    info "version=$version_real, la que se acaba de desplegar"
}

# Todo lo que se le hace a un nodo antes de conmutar. Corre en background, uno
# por nodo, con la salida a su propio archivo para que los logs no se entrelacen.
preparar() {
    local nodo="$1" color="$2" puerto="$3" version="$4"
    ship "$nodo"
    levantar "$nodo" "$color" "$puerto"
    verificar_salud "$nodo" "$color" "$version"
}

# --- Comandos --------------------------------------------------------------

desplegar() {
    local -a nodos=("$@")
    local activo nuevo puerto_nuevo version

    paso "CARGAR — leer el artefacto publicado"
    cargar_artefacto

    activo="$(color_activo_comun "${nodos[@]}")"
    nuevo="$(opuesto_de "$activo")"
    puerto_nuevo="$(puerto_de "$nuevo")"
    version="$(version_del_tag)"

    echo
    echo "Nodos: ${nodos[*]}"
    echo "Sirviendo $activo · se despliega $nuevo en :$puerto_nuevo · $IMAGEN:$TAG"

    # --- Fase 1: preparar todos los nodos a la vez ---
    paso "PREPARAR — los ${#nodos[@]} nodos en paralelo"
    local tmp; tmp="$(mktemp -d)"
    local -A pid_de=()
    local nodo
    for nodo in "${nodos[@]}"; do
        preparar "$nodo" "$nuevo" "$puerto_nuevo" "$version" > "$tmp/$nodo.log" 2>&1 &
        pid_de[$nodo]=$!
        info "$nodo lanzado (pid ${pid_de[$nodo]})"
    done

    local -a fallidos=()
    for nodo in "${nodos[@]}"; do
        wait "${pid_de[$nodo]}" || fallidos+=("$nodo")
    done

    # Los logs de cada nodo, ya ordenados: en paralelo se habrían mezclado.
    for nodo in "${nodos[@]}"; do
        printf '\n\033[1m──────── %s ────────\033[0m\n' "$nodo"
        cat "$tmp/$nodo.log"
    done
    rm -rf "$tmp"

    # --- Fase 2: conmutar, o abortar ---
    if [[ ${#fallidos[@]} -gt 0 ]]; then
        paso "ABORTA — fallaron: ${fallidos[*]}"
        # Se bajan TODAS las nuevas, no sólo las que fallaron: si quedara alguna
        # arriba, el próximo deploy la encontraría ocupando el puerto del color
        # que le toca usar.
        for nodo in "${nodos[@]}"; do
            bajar "$nodo" "$nuevo"
        done
        error "Deploy abortado. Sigue sirviendo la $activo en todos los nodos."
        error "Nadie vio la versión rota."
        return 1
    fi

    paso "CONMUTAR — mandar el tráfico a las $nuevo"
    local nuevos viejos
    nuevos="$(lista_backends "$nuevo" "${nodos[@]}")"
    viejos="$(lista_backends "$activo" "${nodos[@]}")"
    info "agregar: $nuevos"
    info "quitar:  $viejos"
    if ! conmutar_a "$nuevos" "$viejos"; then
        return 1
    fi

    for nodo in "${nodos[@]}"; do
        guardar_estado "$nodo" "$nuevo" "$activo" "$version" "$(version_guardada "$nodo")"
    done

    paso "LISTO"
    info "Sirviendo las $nuevo (version $version) en :$puerto_nuevo"
    info "Las $activo siguen vivas en :$(puerto_de "$activo") — el rollback es un comando"
}

rollback() {
    local -a nodos=("$@")
    local activo anterior puerto_anterior

    activo="$(color_activo_comun "${nodos[@]}")"
    anterior="$(opuesto_de "$activo")"
    puerto_anterior="$(puerto_de "$anterior")"

    paso "ROLLBACK — volver a las $anterior en :$puerto_anterior"

    # Las anteriores siguen corriendo, así que esto es sólo cambiar el destino:
    # no hay que reconstruir, ni copiar, ni levantar nada. Pero se verifica que
    # estén sanas en TODOS los nodos antes de mandarles tráfico.
    local nodo estado
    local -a sin_respaldo=()
    for nodo in "${nodos[@]}"; do
        estado="$(ssh "$nodo" "docker inspect --format '{{.State.Health.Status}}' sdypp-$anterior-app-1 2>/dev/null" || echo "sin-contenedor")"
        if [[ "$estado" != "healthy" ]]; then
            sin_respaldo+=("$nodo ($estado)")
        else
            info "$nodo: la $anterior está sana"
        fi
    done

    if [[ ${#sin_respaldo[@]} -gt 0 ]]; then
        error "no hay a dónde volver en: ${sin_respaldo[*]}"
        return 1
    fi

    conmutar_a "$(lista_backends "$anterior" "${nodos[@]}")" \
               "$(lista_backends "$activo" "${nodos[@]}")"

    # Las dos versiones se intercambian, igual que los colores: la que serví hasta
    # recién pasa a ser la anterior y viceversa. Antes acá se guardaba un color en
    # el campo de la versión, así que el estado quedaba diciendo VERSION=blue.
    for nodo in "${nodos[@]}"; do
        guardar_estado "$nodo" "$anterior" "$activo" \
                       "$(version_anterior_guardada "$nodo")" "$(version_guardada "$nodo")"
    done
    info "el tráfico volvió a las $anterior"
}

estado() {
    echo "destino del balanceador: $(destino_actual || echo '(no configurado)')"
    local nodo
    for nodo in "$@"; do
        echo
        echo "── $nodo ──"
        echo "   color activo según el CI/CD: $(color_activo "$nodo")"
        ssh "$nodo" "docker ps --filter name=sdypp- --format '   {{.Names}}\t{{.Status}}\t{{.Ports}}'" 2>/dev/null \
            || echo "   (no se pudo consultar el nodo)"
    done
}

# --- Punto de entrada ------------------------------------------------------

comando="${1:-}"
shift || true

if [[ -z "$comando" || $# -eq 0 ]]; then
    # Corta en la primera linea que no es comentario: asi la ayuda es la cabecera
    # completa y nada mas, aunque la cabecera cambie de largo.
    awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"
    exit 2
fi

IMAGEN="${IMAGEN:-sdypp-app-python}"

case "$comando" in
    desplegar) desplegar "$@" ;;
    rollback)  rollback  "$@" ;;
    estado)    estado    "$@" ;;
    *) error "comando desconocido: $comando"; exit 2 ;;
esac
