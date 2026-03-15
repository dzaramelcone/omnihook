#!/usr/bin/env bash
# omnihook uninstall — stops the server, removes hooks config, cleans up state.
# Usage: curl -fsSL https://raw.githubusercontent.com/dzaramelcone/omnihook/main/uninstall.sh | bash
set -e

PORT="${OMNIHOOK_PORT:-9100}"
PID_FILE="$HOME/.claude/omnihook/omnihook.pid"
INSTALL_DIR="$HOME/.claude/omnihook-src"
STATE_DIR="$HOME/.claude/omnihook"

echo "==> Stopping omnihook..."

# Kill server if running
if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    kill "$PID" 2>/dev/null && echo "    Stopped (pid $PID)" || echo "    Not running"
fi

# Also check port (lsof on Unix, netstat fallback)
if command -v lsof &>/dev/null; then
    lsof -ti:"$PORT" 2>/dev/null | xargs kill 2>/dev/null || true
fi

# --- Clean omnihook entries from settings files ---

_clean_settings() {
    local settings_file="$1"
    local label="$2"
    [[ -f "$settings_file" ]] || return 0

    python3 - "$settings_file" "$PORT" <<'PYEOF'
import json, sys
from pathlib import Path

settings_path = Path(sys.argv[1])
port = sys.argv[2]
s = json.loads(settings_path.read_text())
hooks = s.get("hooks", {})
changed = False

for event in list(hooks.keys()):
    filtered = []
    for group in hooks[event]:
        inner = group.get("hooks", [])
        inner = [h for h in inner if not (
            f"127.0.0.1:{port}" in h.get("url", "") or
            "ensure_omnihook" in h.get("command", "")
        )]
        if inner:
            group["hooks"] = inner
            filtered.append(group)
    if filtered != hooks[event]:
        changed = True
    if filtered:
        hooks[event] = filtered
    else:
        del hooks[event]
        changed = True

allowed = s.get("allowedHttpHookUrls", [])
needle = f"127.0.0.1:{port}"
new_allowed = [u for u in allowed if needle not in u]
if len(new_allowed) != len(allowed):
    s["allowedHttpHookUrls"] = new_allowed
    changed = True
if not s.get("allowedHttpHookUrls"):
    s.pop("allowedHttpHookUrls", None)

if changed:
    settings_path.write_text(json.dumps(s, indent=2) + "\n")
    print(f"    Cleaned {settings_path}")
else:
    print(f"    No omnihook entries in {settings_path}")
PYEOF
}

echo "==> Removing hooks config..."

# Clean global settings
_clean_settings "$HOME/.claude/settings.json" "global"

# Clean project settings (CWD)
_clean_settings ".claude/settings.json" "project"
_clean_settings ".claude/settings.local.json" "project local"

echo "==> Removing state directory..."
rm -rf "$STATE_DIR" && echo "    Removed $STATE_DIR"

echo "==> Removing CLI..."
rm -f "$HOME/.local/bin/omnihook" && echo "    Removed ~/.local/bin/omnihook"

echo "==> Removing source..."
rm -rf "$INSTALL_DIR" && echo "    Removed $INSTALL_DIR"

echo "==> Done. omnihook fully removed."
