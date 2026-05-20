#!/usr/bin/env bash
# start.sh — OpenHost supervisor for GraphHopper.
#
# Manages up to three processes:
#   1. GraphHopper          (127.0.0.1:8989)  — only after setup
#   2. Admin UI             (127.0.0.1:8091)
#   3. Auth proxy           (0.0.0.0:8080)
#
# On first boot (no .setup_complete sentinel), starts in setup mode:
# only the admin UI and auth proxy run, and all requests redirect to
# the region picker. Once the user picks a region and the graph builds,
# the sentinel is written and the container restarts into normal mode.

set -euo pipefail

APP_DATA="${OPENHOST_APP_DATA_DIR:-/data/app_data/graphhopper}"
GH_DIR="/graphhopper"

GH_HOST="127.0.0.1"
GH_PORT=8989
PROXY_PORT=8080

GH_PID=""
PROXY_PID=""
ADMIN_PID=""

SETUP_SENTINEL="${APP_DATA}/.setup_complete"

cleanup() {
    echo "[start.sh] Shutting down..."
    [ -n "$PROXY_PID" ] && kill "$PROXY_PID" 2>/dev/null || true
    [ -n "$ADMIN_PID" ] && kill "$ADMIN_PID" 2>/dev/null || true
    [ -n "$GH_PID" ] && kill "$GH_PID" 2>/dev/null || true
    wait
}
trap cleanup EXIT SIGTERM SIGINT

mkdir -p "$APP_DATA"

# --- Migrate: if graph already exists from a previous install, mark setup done ---
GRAPH_DIR="${APP_DATA}/graph-cache"
if [ ! -f "$SETUP_SENTINEL" ] && [ -d "$GRAPH_DIR" ] && [ -n "$(ls -A "$GRAPH_DIR" 2>/dev/null)" ]; then
    echo "[start.sh] Existing graph found — migrating to setup_complete"
    touch "$SETUP_SENTINEL"
fi

# --- Setup mode: skip download/build, just run admin + proxy ---
if [ ! -f "$SETUP_SENTINEL" ]; then
    echo "[start.sh] First boot — starting in setup mode (no region selected)"
    export GRAPHHOPPER_SETUP_MODE=1

    echo "[start.sh] Starting admin UI on 127.0.0.1:8091..."
    python3 /opt/openhost/admin.py &
    ADMIN_PID=$!

    echo "[start.sh] Starting auth proxy on 0.0.0.0:${PROXY_PORT}..."
    python3 /opt/openhost/auth_proxy.py &
    PROXY_PID=$!

    echo "[start.sh] Setup mode ready. Visit the app to select a region."
    wait -n "$ADMIN_PID" "$PROXY_PID"
    EXIT_CODE=$?
    echo "[start.sh] Child exited (code=$EXIT_CODE)."
    exit "$EXIT_CODE"
fi

# --- Normal mode: region already configured ---

# Region configuration
REGION_CONF="${APP_DATA}/region.conf"
PBF_URL="https://download.geofabrik.de/north-america/us/california-latest.osm.pbf"

if [ -f "$REGION_CONF" ]; then
    # shellcheck source=/dev/null
    source "$REGION_CONF"
    echo "[start.sh] Region: $PBF_URL"
fi

PBF_FILE="${APP_DATA}/region.osm.pbf"
GRAPH_DIR="${APP_DATA}/graph-cache"

# --- Download PBF if not present ---
if [ ! -f "$PBF_FILE" ]; then
    echo "[start.sh] Downloading: $PBF_URL"
    wget -q --show-progress -O "${PBF_FILE}.tmp" "$PBF_URL"
    mv "${PBF_FILE}.tmp" "$PBF_FILE"
    echo "[start.sh] Download complete: $(du -h "$PBF_FILE" | cut -f1)"
fi

# --- Java heap: read container memory limit from cgroup ---
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

# --- Use upstream config with overrides via system properties ---
GH_CONFIG="${GH_DIR}/config-example.yml"

# --- Build graph if not present ---
if [ ! -d "$GRAPH_DIR" ] || [ -z "$(ls -A "$GRAPH_DIR" 2>/dev/null)" ]; then
    echo "[start.sh] Building routing graph (first boot, may take 10-60 min)..."
    cd "$GH_DIR"
    java $JAVA_OPTS \
        -Ddw.graphhopper.datareader.file="$PBF_FILE" \
        -Ddw.graphhopper.graph.location="$GRAPH_DIR" \
        -jar graphhopper-web-*.jar import "$GH_CONFIG" 2>&1 || {
        echo "[start.sh] Graph build failed."
        rm -rf "$GRAPH_DIR"
        exit 1
    }
    echo "[start.sh] Graph complete: $(du -sh "$GRAPH_DIR" | cut -f1)"
fi

# --- Start GraphHopper ---
echo "[start.sh] Starting GraphHopper on ${GH_HOST}:${GH_PORT}..."
cd "$GH_DIR"
java $JAVA_OPTS \
    "-Ddw.graphhopper.datareader.file=$PBF_FILE" \
    "-Ddw.graphhopper.graph.location=$GRAPH_DIR" \
    -jar graphhopper-web-*.jar server "$GH_CONFIG" &
GH_PID=$!

echo "[start.sh] Waiting for GraphHopper..."
for i in $(seq 1 120); do
    if curl -sf "http://${GH_HOST}:${GH_PORT}/health" >/dev/null 2>&1; then
        echo "[start.sh] GraphHopper is ready."
        break
    fi
    if ! kill -0 "$GH_PID" 2>/dev/null; then
        echo "[start.sh] ERROR: GraphHopper died during startup."
        exit 1
    fi
    [ "$i" -eq 120 ] && echo "[start.sh] WARNING: GraphHopper slow to start."
    sleep 1
done

# --- Start admin UI ---
echo "[start.sh] Starting admin UI on 127.0.0.1:8091..."
python3 /opt/openhost/admin.py &
ADMIN_PID=$!

# --- Start auth proxy ---
echo "[start.sh] Starting auth proxy on 0.0.0.0:${PROXY_PORT}..."
python3 /opt/openhost/auth_proxy.py &
PROXY_PID=$!

echo "[start.sh] All services started. GH=$GH_PID ADMIN=$ADMIN_PID PROXY=$PROXY_PID"

wait -n "$GH_PID" "$ADMIN_PID" "$PROXY_PID"
EXIT_CODE=$?
echo "[start.sh] Child exited (code=$EXIT_CODE)."
exit "$EXIT_CODE"
