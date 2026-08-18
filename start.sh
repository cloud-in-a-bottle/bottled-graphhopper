#!/usr/bin/env bash
# start.sh — OpenHost supervisor for GraphHopper.
#
# Runs as a persistent supervisor loop, so the container NEVER depends on an
# external relaunch to change modes.
#
# Processes managed per iteration:
#   1. GraphHopper          (127.0.0.1:8989)  — only in normal mode
#   2. Admin UI             (127.0.0.1:8091)
#   3. Auth proxy           (0.0.0.0:8080)
#
# On first boot (no .setup_complete sentinel) it runs in *setup mode*: only the
# admin UI and auth proxy run, and all requests redirect to the region picker.
# When the owner picks a region and the graph finishes building, admin.py drops
# a ".restart_requested" flag file. The supervisor tears down the setup-mode
# processes and re-enters the loop in *normal mode* (GraphHopper + admin +
# proxy) — all in-process, without exiting the container. The same flag drives
# a later region change. We deliberately do NOT kill PID 1 to restart, because
# a stopped container is not reliably relaunched by the runtime.

set -uo pipefail

APP_DATA="${OPENHOST_APP_DATA_DIR:-/data/app_data/graphhopper}"
GH_DIR="/graphhopper"

GH_HOST="127.0.0.1"
GH_PORT=8989
PROXY_PORT=8080

GH_PID=""
PROXY_PID=""
ADMIN_PID=""

SETUP_SENTINEL="${APP_DATA}/.setup_complete"
RESTART_FLAG="${APP_DATA}/.restart_requested"
GRAPH_DIR="${APP_DATA}/graph-cache"

stop_children() {
    for pid in "$PROXY_PID" "$ADMIN_PID" "$GH_PID"; do
        [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    done
    for pid in "$PROXY_PID" "$ADMIN_PID" "$GH_PID"; do
        [ -n "$pid" ] && wait "$pid" 2>/dev/null || true
    done
    GH_PID=""
    ADMIN_PID=""
    PROXY_PID=""
}

cleanup() {
    echo "[start.sh] Shutting down..."
    stop_children
}
# A real container stop (SIGTERM from the runtime) tears everything down and
# exits. Only admin.py's flag file cycles the supervisor without exiting.
trap 'cleanup; exit 0' SIGTERM SIGINT
trap cleanup EXIT

mkdir -p "$APP_DATA"

# --- Migrate: existing graph from a previous install → mark setup done ---
if [ ! -f "$SETUP_SENTINEL" ] && [ -d "$GRAPH_DIR" ] && [ -n "$(ls -A "$GRAPH_DIR" 2>/dev/null)" ]; then
    echo "[start.sh] Existing graph found — migrating to setup_complete"
    touch "$SETUP_SENTINEL"
fi

# --- Block until a child dies or a restart is requested. -------------------
# Returns 0 when a restart flag appears, 1 when a managed child exits.
supervise() {
    while true; do
        if [ -f "$RESTART_FLAG" ]; then
            echo "[start.sh] Restart requested."
            return 0
        fi
        for pid in "$GH_PID" "$ADMIN_PID" "$PROXY_PID"; do
            if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
                echo "[start.sh] Managed process $pid exited."
                return 1
            fi
        done
        sleep 2
    done
}

start_setup_mode() {
    echo "[start.sh] Setup mode — no region selected yet."
    echo "[start.sh] Starting admin UI on 127.0.0.1:8091..."
    GRAPHHOPPER_SETUP_MODE=1 python3 /opt/openhost/admin.py &
    ADMIN_PID=$!
    echo "[start.sh] Starting auth proxy on 0.0.0.0:${PROXY_PORT}..."
    GRAPHHOPPER_SETUP_MODE=1 python3 /opt/openhost/auth_proxy.py &
    PROXY_PID=$!
    echo "[start.sh] Setup mode ready. Visit the app to select a region."
}

# Returns 0 on success, 1 if the (fallback) download/build failed.
start_normal_mode() {
    local REGION_CONF="${APP_DATA}/region.conf"
    PBF_URL="https://download.geofabrik.de/north-america/us/california-latest.osm.pbf"
    if [ -f "$REGION_CONF" ]; then
        # shellcheck source=/dev/null
        source "$REGION_CONF"
        echo "[start.sh] Region: $PBF_URL"
    fi

    local PBF_FILE="${APP_DATA}/region.osm.pbf"

    # --- Java heap: read container memory limit from cgroup ---
    local CGROUP_LIMIT TOTAL_MB HEAP_MB
    CGROUP_LIMIT=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || echo "0")
    if [ "$CGROUP_LIMIT" = "max" ] || [ "$CGROUP_LIMIT" -le 0 ] 2>/dev/null; then
        TOTAL_MB=8192
    else
        TOTAL_MB=$((CGROUP_LIMIT / 1048576))
    fi
    HEAP_MB=$((TOTAL_MB * 75 / 100))
    [ "$HEAP_MB" -lt 1024 ] && HEAP_MB=1024
    JAVA_OPTS="-Xmx${HEAP_MB}m -Xms$((HEAP_MB / 2))m"
    export JAVA_OPTS
    echo "[start.sh] Java heap: ${HEAP_MB}m"

    local GH_CONFIG="${GH_DIR}/config-example.yml"

    # --- Start admin + proxy FIRST so 0.0.0.0:8080 always has a listener. ---
    # During download/build/graph-load, GraphHopper is not up yet; the proxy's
    # /healthz returns 503 "starting" instead of the gateway seeing no listener.
    echo "[start.sh] Starting admin UI on 127.0.0.1:8091..."
    python3 /opt/openhost/admin.py &
    ADMIN_PID=$!
    echo "[start.sh] Starting auth proxy on 0.0.0.0:${PROXY_PORT}..."
    python3 /opt/openhost/auth_proxy.py &
    PROXY_PID=$!

    # --- Download PBF if missing (fallback; admin.py normally does this) ---
    if [ ! -f "$PBF_FILE" ]; then
        echo "[start.sh] Downloading: $PBF_URL"
        if wget -q -O "${PBF_FILE}.tmp" "$PBF_URL"; then
            mv "${PBF_FILE}.tmp" "$PBF_FILE"
            echo "[start.sh] Download complete: $(du -h "$PBF_FILE" | cut -f1)"
        else
            echo "[start.sh] Download failed."
            rm -f "${PBF_FILE}.tmp"
            return 1
        fi
    fi

    # --- Build graph if missing (fallback) ---
    if [ ! -d "$GRAPH_DIR" ] || [ -z "$(ls -A "$GRAPH_DIR" 2>/dev/null)" ]; then
        echo "[start.sh] Building routing graph (first boot, may take 10-60 min)..."
        if ! ( cd "$GH_DIR" && java $JAVA_OPTS \
                -Ddw.graphhopper.datareader.file="$PBF_FILE" \
                -Ddw.graphhopper.graph.location="$GRAPH_DIR" \
                -jar graphhopper-web-*.jar import "$GH_CONFIG" 2>&1 ); then
            echo "[start.sh] Graph build failed."
            rm -rf "$GRAPH_DIR"
            return 1
        fi
        echo "[start.sh] Graph complete: $(du -sh "$GRAPH_DIR" | cut -f1)"
    fi

    # --- Start GraphHopper ---
    echo "[start.sh] Starting GraphHopper on ${GH_HOST}:${GH_PORT}..."
    ( cd "$GH_DIR" && exec java $JAVA_OPTS \
        "-Ddw.graphhopper.datareader.file=$PBF_FILE" \
        "-Ddw.graphhopper.graph.location=$GRAPH_DIR" \
        -jar graphhopper-web-*.jar server "$GH_CONFIG" ) &
    GH_PID=$!

    echo "[start.sh] All services started. GH=$GH_PID ADMIN=$ADMIN_PID PROXY=$PROXY_PID"
    return 0
}

# --- Supervisor loop -------------------------------------------------------
while true; do
    rm -f "$RESTART_FLAG"

    if [ ! -f "$SETUP_SENTINEL" ]; then
        start_setup_mode
    else
        if ! start_normal_mode; then
            echo "[start.sh] Normal-mode startup failed; retrying in 10s."
            stop_children
            sleep 10
            continue
        fi
    fi

    supervise || echo "[start.sh] Cycling supervisor."
    stop_children
    rm -f "$RESTART_FLAG"
    sleep 1
done
