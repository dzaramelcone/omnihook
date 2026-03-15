# omnihook

A standalone hook server for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Routes all hook events through a single HTTP endpoint backed by a finite state machine with durable execution.

## Why

Claude Code hooks are powerful but stateless — each hook invocation is independent. Omnihook adds:

- **Session-scoped state** — track where you are in a workflow across hook calls
- **Finite state machine** — `dict[str, dict[str, Callable]]` maps `(state, event) → handler`. Declarative setup, imperative callbacks.
- **Crash-proof** — session state persisted atomically to disk before every response. Survives `SIGKILL` + restart.
- **Runtime mutation** — add states, swap handlers, hot-reload code, POST new handler source — no restart needed
- **Enable/disable** — global or per-session, toggle at runtime

Inspired by [Temporal](https://temporal.io)'s durable execution model.

## Quick start

One command from your project root — installs omnihook, sets up the SessionStart bootstrap hook, merges settings, and starts the server:

```bash
curl -fsSL https://raw.githubusercontent.com/dzaramelcone/omnihook/main/quickstart.sh | bash
```

Open Claude Code. Hooks are active.

### Manual install

```bash
pip install omnihook   # or: uv add omnihook
python -m omnihook     # starts on :9100
```

Then copy `example-settings.json` into `.claude/settings.json` and `ensure_omnihook.sh` into `.claude/hooks/`.

### Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/dzaramelcone/omnihook/main/uninstall.sh | bash
```

Stops the server, removes hooks from settings, cleans up state and source.

## Architecture

```
Claude Code                     omnihook (:9100)
    │                                │
    ├─ SessionStart ──POST /hook──→  │  idle ──→ active
    ├─ PreToolUse ───POST /hook──→   │  guard_secrets()
    ├─ PostToolUse ──POST /hook──→   │  lint_python()
    ├─ Stop ─────────POST /hook──→   │  (state-specific handler)
    └─ SessionEnd ───POST /hook──→   │  cleanup session
                                     │
              ┌─ /ctl/enable|disable  │  toggle on/off
              ├─ /ctl/machine         │  inspect/mutate FSM
              ├─ /ctl/rate-limit      │  per-session rate limiting
              ├─ /handlers            │  POST/GET/DELETE handler source
              └─ /health              │  liveness check
```

## State machine

The core is a `dict[str, dict[str, Handler]]`:

```python
MACHINE = {
    "idle": {
        "SessionStart": on_session_start,   # → "active"
        "PreToolUse":   guard_secrets,
        "PostToolUse":  lint_python,
        "Stop":         passthrough,
        "SessionEnd":   on_session_end,
    },
    "active": {
        "PreToolUse":   guard_secrets,
        "PostToolUse":  lint_python,
        "Stop":         passthrough,
        "SessionEnd":   on_session_end,
    },
}
```

Each handler is `(SessionState, HookInput) → (next_state | None, response_dict)`. Add your own states and handlers to build multi-step workflows.

## API

### Hook endpoint

| Method | Path | Description |
|--------|------|-------------|
| POST | `/hook` | Receives all Claude Code hook events |

### Control plane

| Method | Path | Description |
|--------|------|-------------|
| POST | `/ctl/enable` | Enable globally |
| POST | `/ctl/disable` | Disable globally (all hooks passthrough) |
| POST | `/ctl/enable/{session_id}` | Enable for one session |
| POST | `/ctl/disable/{session_id}` | Disable for one session |
| POST | `/ctl/rate-limit` | Set rate limit: `{"max_calls": 60, "window_sec": 10}` |
| GET | `/ctl/status` | Current state of everything |
| GET | `/health` | Liveness check |

### Machine mutation (live, no restart)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/ctl/machine` | Inspect machine + available handlers |
| PUT | `/ctl/machine/{state}/{event}` | Rewire: `{"handler": "my_fn"}` |
| DELETE | `/ctl/machine/{state}/{event}` | Remove handler (falls through to passthrough) |
| PUT | `/ctl/machine/{state}` | Add/replace state: `{"Stop": "my_fn", ...}` |
| DELETE | `/ctl/machine/{state}` | Remove state |
| POST | `/ctl/machine/reset` | Restore defaults, clear persisted layout |
| POST | `/ctl/machine/reload` | Hot-reload handlers.py |

### Handler source management

| Method | Path | Description |
|--------|------|-------------|
| POST | `/handlers` | Add handler: `{"source": "def my_fn(session, inp): ..."}` |
| GET | `/handlers` | List all handlers with source |
| DELETE | `/handlers/{name}` | Remove handler from source |

## Writing handlers

A handler is a function in `omnihook/handlers.py`:

```python
def my_handler(session: SessionState, inp: HookInput) -> tuple[str | None, dict]:
    # Return (next_state, response_dict)
    # next_state=None means stay in current state
    return None, {"systemMessage": "hello from omnihook"}
```

Or POST it at runtime:

```bash
curl -X POST http://127.0.0.1:9100/handlers \
  -H 'Content-Type: application/json' \
  -d '{"source": "def my_handler(session, inp):\n    return None, {\"systemMessage\": \"hello\"}"}'
```

Then wire it in:

```bash
curl -X PUT http://127.0.0.1:9100/ctl/machine/active/Stop \
  -H 'Content-Type: application/json' \
  -d '{"handler": "my_handler"}'
```

## Crash recovery

- Session state: `~/.claude/omnihook/sessions/{id}.json` — atomic write (tmp+rename) before every HTTP response
- Machine layout: `~/.claude/omnihook/machine.json` — persisted on every mutation
- Config: `~/.claude/omnihook/config.json` — enable/disable, rate limits
- PID: `~/.claude/omnihook/omnihook.pid` — launcher checks process liveness

If omnihook crashes: session state survives on disk. On restart, sessions resume from last persisted state. Handlers are idempotent — re-execution from a previous state produces the same result.

If Claude Code crashes: session state remains. New session gets a new ID; old sessions are cleaned up after 24h.

## License

MIT
