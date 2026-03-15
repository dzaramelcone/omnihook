#!/usr/bin/env bash
# SessionStart hook: ensure omnihook is running. Idempotent — safe to call repeatedly.
set -e

PORT=9100
PID_FILE="$HOME/.claude/omnihook/omnihook.pid"
LOG_FILE="$HOME/.claude/omnihook/omnihook.log"

# Fast path: already running and healthy
if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
            exit 0
        fi
    fi
fi

mkdir -p "$(dirname "$LOG_FILE")"

# Start omnihook (detached, survives parent exit)
cd "${CLAUDE_PROJECT_DIR:-.}"
nohup uv run python -m omnihook >> "$LOG_FILE" 2>&1 &

# Wait for health (up to 5s)
for _ in $(seq 1 20); do
    if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        exit 0
    fi
    sleep 0.25
done

echo "omnihook failed to start within 5s" >&2
exit 1
