#!/usr/bin/env bash
# SessionStart hook: ensure omnihook is running. Idempotent — safe to call repeatedly.
set -e

PORT="${OMNIHOOK_PORT:-9100}"
PID_FILE="$HOME/.claude/omnihook/omnihook.pid"
LOG_FILE="$HOME/.claude/omnihook/omnihook.log"

# Fast path: already running and healthy
if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
            echo "[omnihook] active — omnihook status | omnihook disable | omnihook machine"
            exit 0
        fi
    fi
fi

mkdir -p "$(dirname "$LOG_FILE")"

# Start omnihook from install dir (detached, survives parent exit)
OMNIHOOK_DIR="$HOME/.claude/omnihook-src"
if [[ ! -d "$OMNIHOOK_DIR" ]]; then
    echo "omnihook not installed — run quickstart.sh first" >&2
    exit 1
fi
cd "$OMNIHOOK_DIR"
uv sync --quiet 2>>"$LOG_FILE"
nohup uv run omnihook-server >> "$LOG_FILE" 2>&1 &
disown

# Wait for health (up to 10s)
for _ in $(seq 1 40); do
    if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        echo "[omnihook] started — omnihook status | omnihook disable | omnihook machine"
        exit 0
    fi
    sleep 0.25
done

echo "omnihook failed to start within 10s" >&2
exit 1
