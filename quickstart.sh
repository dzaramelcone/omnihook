#!/usr/bin/env bash
# omnihook quickstart — run this once to install and configure everything.
# Usage: curl -fsSL https://raw.githubusercontent.com/dzaramelcone/omnihook/main/quickstart.sh | bash
set -e

REPO="${OMNIHOOK_REPO:-https://github.com/dzaramelcone/omnihook.git}"
INSTALL_DIR="$HOME/.claude/omnihook-src"
PORT="${OMNIHOOK_PORT:-9100}"

echo "==> Installing omnihook..."

# Clone or update
if [[ -d "$INSTALL_DIR" ]]; then
    git -C "$INSTALL_DIR" pull -q
else
    git clone -q "$REPO" "$INSTALL_DIR"
fi

echo "==> Configuring hooks..."

# Write settings to GLOBAL ~/.claude/settings.json (not CWD-relative)
SETTINGS="$HOME/.claude/settings.json"
if [[ -f "$SETTINGS" ]]; then
    cp "$SETTINGS" "${SETTINGS}.bak"
    echo "    Backed up $SETTINGS"
fi

python3 - "$SETTINGS" "$INSTALL_DIR/example-settings.json" "$PORT" "$INSTALL_DIR" <<'PYEOF'
import json, sys, copy
from pathlib import Path

settings_path = Path(sys.argv[1])
example_path = Path(sys.argv[2])
port = sys.argv[3]
install_dir = sys.argv[4]

existing = json.loads(settings_path.read_text()) if settings_path.exists() else {}
example = json.loads(example_path.read_text())

hooks = existing.setdefault("hooks", {})
for event, configs in example["hooks"].items():
    normalized = []
    for group in configs:
        group = copy.deepcopy(group)
        for hook in group.get("hooks", []):
            if hook.get("type") == "http":
                hook["url"] = f"http://127.0.0.1:{port}/hook"
            if hook.get("type") == "command" and "ensure_omnihook.sh" in hook.get("command", ""):
                hook["command"] = f"{install_dir}/ensure_omnihook.sh"
        normalized.append(group)
    if event not in hooks:
        hooks[event] = normalized

allowed = existing.setdefault("allowedHttpHookUrls", [])
url = f"http://127.0.0.1:{port}/*"
if url not in allowed:
    allowed.append(url)

settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(json.dumps(existing, indent=2) + "\n")
PYEOF

echo "==> Pre-building environment..."
cd "$INSTALL_DIR"
uv sync --quiet

echo "==> Installing CLI..."
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/omnihook" << CLIEOF
#!/usr/bin/env bash
exec uv run --project "$INSTALL_DIR" omnihook "\$@"
CLIEOF
chmod +x "$HOME/.local/bin/omnihook"

echo "==> Starting omnihook..."

mkdir -p "$HOME/.claude/omnihook"
nohup uv run omnihook-server >> "$HOME/.claude/omnihook/omnihook.log" 2>&1 &
disown

# Wait for health (up to 10s)
for _ in $(seq 1 40); do
    if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        echo "==> omnihook running on :$PORT"
        echo "==> Open Claude Code in any project directory. Hooks are active."
        exit 0
    fi
    sleep 0.25
done

echo "==> omnihook failed to start. Check ~/.claude/omnihook/omnihook.log"
exit 1
