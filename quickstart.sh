#!/usr/bin/env bash
# omnihook quickstart — run this once to install and configure everything.
# Usage: curl -fsSL https://raw.githubusercontent.com/dzaramelcone/omnihook/main/quickstart.sh | bash
set -e

REPO="${OMNIHOOK_REPO:-https://github.com/dzaramelcone/omnihook.git}"
INSTALL_DIR="$HOME/.claude/omnihook-src"
HOOKS_DIR=".claude/hooks"
SETTINGS=".claude/settings.json"
PORT="${OMNIHOOK_PORT:-9100}"

echo "==> Installing omnihook..."

# Clone or update
if [[ -d "$INSTALL_DIR" ]]; then
    git -C "$INSTALL_DIR" pull -q
else
    git clone -q "$REPO" "$INSTALL_DIR"
fi

# No global install needed — we run directly from the source via uv

echo "==> Setting up launcher hook..."

mkdir -p "$HOOKS_DIR"
cp "$INSTALL_DIR/ensure_omnihook.sh" "$HOOKS_DIR/ensure_omnihook.sh"
chmod +x "$HOOKS_DIR/ensure_omnihook.sh"

# Merge hooks into settings.json
if [[ -f "$SETTINGS" ]]; then
    # Backup existing
    cp "$SETTINGS" "${SETTINGS}.bak"
    echo "    Backed up existing $SETTINGS to ${SETTINGS}.bak"
fi

python3 -c "
import json
import copy
from pathlib import Path

settings_path = Path('$SETTINGS')
example_path = Path('$INSTALL_DIR/example-settings.json')
port = '$PORT'

existing = json.loads(settings_path.read_text()) if settings_path.exists() else {}
example = json.loads(example_path.read_text())

hooks = existing.setdefault('hooks', {})
for event, configs in example['hooks'].items():
    normalized = []
    for group in configs:
        group = copy.deepcopy(group)
        for hook in group.get('hooks', []):
            if hook.get('type') == 'http':
                hook['url'] = f'http://127.0.0.1:{port}/hook'
            if hook.get('type') == 'command' and 'ensure_omnihook.sh' in hook.get('command', ''):
                hook['command'] = '\$CLAUDE_PROJECT_DIR/.claude/hooks/ensure_omnihook.sh'
        normalized.append(group)
    if event not in hooks:
        hooks[event] = normalized

allowed = existing.setdefault('allowedHttpHookUrls', [])
url = 'http://127.0.0.1:$PORT/*'
if url not in allowed:
    allowed.append(url)

settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(json.dumps(existing, indent=2) + '\n')
"

echo "==> Pre-building environment..."
cd "$INSTALL_DIR"
uv sync --quiet

echo "==> Starting omnihook..."

mkdir -p "$HOME/.claude/omnihook"
nohup uv run omnihook-server >> "$HOME/.claude/omnihook/omnihook.log" 2>&1 &

# Wait for health (up to 10s)
for _ in $(seq 1 40); do
    if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        echo "==> omnihook running on :$PORT"
        echo "==> Launching Claude Code..."
        exec claude
    fi
    sleep 0.25
done

echo "==> omnihook failed to start. Check ~/.claude/omnihook/omnihook.log"
exit 1
