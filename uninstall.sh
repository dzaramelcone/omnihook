#!/usr/bin/env bash
# omnihook uninstall — stops the server, removes hooks config, cleans up state.
# Usage: curl -fsSL https://raw.githubusercontent.com/dzaramelcone/omnihook/main/uninstall.sh | bash
set -e

PORT="${OMNIHOOK_PORT:-9100}"
PID_FILE="$HOME/.claude/omnihook/omnihook.pid"
INSTALL_DIR="$HOME/.claude/omnihook-src"
STATE_DIR="$HOME/.claude/omnihook"
HOOKS_DIR=".claude/hooks"
SETTINGS=".claude/settings.json"

echo "==> Stopping omnihook..."

# Kill server if running
if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    kill "$PID" 2>/dev/null && echo "    Stopped (pid $PID)" || echo "    Not running"
fi

# Also check port
lsof -ti:"$PORT" 2>/dev/null | xargs kill 2>/dev/null || true

echo "==> Removing hooks config from $SETTINGS..."

if [[ -f "$SETTINGS" ]]; then
    python3 -c "
import json
from pathlib import Path

p = Path('$SETTINGS')
s = json.loads(p.read_text())
port = '$PORT'
hooks = s.get('hooks', {})
changed = False

# Remove any hook entries that point to omnihook
for event in list(hooks.keys()):
    filtered = []
    for group in hooks[event]:
        inner = group.get('hooks', [])
        inner = [h for h in inner if not (
            h.get('url', '').startswith('http://127.0.0.1:$PORT') or
            'ensure_omnihook' in h.get('command', '')
        )]
        if inner:
            group['hooks'] = inner
            filtered.append(group)
    if filtered:
        hooks[event] = filtered
        changed = True
    else:
        del hooks[event]
        changed = True

# Remove allowedHttpHookUrls entry
allowed = s.get('allowedHttpHookUrls', [])
needle = f'127.0.0.1:{port}'
new_allowed = [u for u in allowed if needle not in u]
if len(new_allowed) != len(allowed):
    s['allowedHttpHookUrls'] = new_allowed
    changed = True
if not s.get('allowedHttpHookUrls'):
    s.pop('allowedHttpHookUrls', None)

if changed:
    p.write_text(json.dumps(s, indent=2) + '\n')
    print('    Cleaned $SETTINGS')
else:
    print('    No omnihook entries found in $SETTINGS')
"
fi

echo "==> Removing launcher hook..."
rm -f "$HOOKS_DIR/ensure_omnihook.sh" && echo "    Removed $HOOKS_DIR/ensure_omnihook.sh" || true

echo "==> Removing state directory..."
rm -rf "$STATE_DIR" && echo "    Removed $STATE_DIR"

echo "==> Removing source..."
rm -rf "$INSTALL_DIR" && echo "    Removed $INSTALL_DIR"

echo "==> Uninstalling package..."
pip uninstall -y omnihook 2>/dev/null || uv pip uninstall omnihook 2>/dev/null || true

echo "==> Done. omnihook fully removed."
