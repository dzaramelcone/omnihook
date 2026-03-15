"""Omnihook — standalone FastAPI server for all Claude Code hooks.

Single /hook endpoint receives every hook event. The state machine routes
events to handlers based on (current_state, event_name). Session state is
durably persisted before each response (crash-proof).
"""

import ast
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .machine import (
    REGISTRY,
    add_state,
    lifecycle_snapshot,
    load_persisted,
    reload_handlers,
    remove_handler,
    remove_lifecycle,
    remove_state,
    reset_machine,
    set_handler,
    set_lifecycle,
    snapshot,
    transition,
)
from .models import HookInput, RateLimit
from .store import (
    check_rate_limit,
    cleanup_stale,
    delete_session,
    list_sessions,
    load_config,
    load_session,
    save_config,
    save_session,
    session_lock,
)

log = logging.getLogger("omnihook")

app = FastAPI(title="omnihook")


@app.on_event("startup")
def startup():
    cleanup_stale()
    load_persisted()
    log.info("omnihook started, stale sessions cleaned, machine layout loaded")


@app.post("/hook")
def handle_hook(inp: HookInput) -> dict:
    config = load_config()
    if not config.enabled:
        return {}

    with session_lock(inp.session_id):
        session = load_session(inp.session_id)
        if not session.enabled:
            return {}

        if not check_rate_limit(session, config.rate_limit):
            save_session(session)
            return {
                "systemMessage": (
                    "[omnihook] rate limited — hooks paused until window resets. "
                    "Run `omnihook disable` to turn off hooks, "
                    "or `omnihook rate-limit 120 10` to raise the limit."
                ),
            }

        session, output = transition(session, inp)

        # Persist BEFORE responding (write-ahead)
        if session.state == "ended":
            delete_session(session.session_id)
        else:
            save_session(session)

    return output


# --- Control plane ---


@app.post("/ctl/enable")
def enable_global():
    config = load_config()
    config.enabled = True
    save_config(config)
    return {"enabled": True}


@app.post("/ctl/disable")
def disable_global():
    config = load_config()
    config.enabled = False
    save_config(config)
    return {"enabled": False}


@app.post("/ctl/enable/{session_id}")
def enable_session(session_id: str):
    with session_lock(session_id):
        session = load_session(session_id)
        session.enabled = True
        save_session(session)
    return {"session_id": session_id, "enabled": True}


@app.post("/ctl/disable/{session_id}")
def disable_session(session_id: str):
    with session_lock(session_id):
        session = load_session(session_id)
        session.enabled = False
        save_session(session)
    return {"session_id": session_id, "enabled": False}


@app.post("/ctl/rate-limit")
def set_rate_limit(limit: RateLimit):
    config = load_config()
    config.rate_limit = limit
    save_config(config)
    return {"rate_limit": limit.model_dump()}


# --- Machine mutation (live, no restart) ---


@app.get("/ctl/machine")
def get_machine():
    return {
        "machine": snapshot(),
        "lifecycle": lifecycle_snapshot(),
        "registry": sorted(REGISTRY),
    }


@app.put("/ctl/machine/{state}/{event}")
def put_handler(state: str, event: str, body: dict):
    """Set a handler: PUT /ctl/machine/active/Stop {"handler": "on_stop_active"}"""
    handler_name = body.get("handler", "")
    if not handler_name:
        return JSONResponse({"error": "missing 'handler' key"}, 400)
    set_handler(state, event, handler_name)
    return {"state": state, "event": event, "handler": handler_name}


@app.delete("/ctl/machine/{state}/{event}")
def delete_handler(state: str, event: str):
    remove_handler(state, event)
    return {"removed": f"{state}/{event}"}


@app.put("/ctl/machine/{state}")
def put_state(state: str, body: dict[str, str]):
    """Add/replace a state: PUT /ctl/machine/my_state {"Stop": "passthrough", ...}"""
    add_state(state, body)
    return {"state": state, "handlers": body}


@app.delete("/ctl/machine/{state}")
def delete_state(state: str):
    remove_state(state)
    return {"removed": state}


@app.post("/ctl/machine/reset")
def reset():
    reset_machine()
    return {"reset": True, "machine": snapshot()}


@app.post("/ctl/machine/reload")
def reload():
    """Hot-reload handlers.py — new/changed functions become available immediately."""
    reload_handlers()
    return {"registry": sorted(REGISTRY), "machine": snapshot()}


# --- Lifecycle hooks (on_enter / on_exit) ---


@app.put("/ctl/lifecycle/{state}/{hook}")
def put_lifecycle(state: str, hook: str, body: dict):
    """Set a lifecycle hook: PUT /ctl/lifecycle/active/on_enter {"handler": "my_fn"}"""
    if hook not in ("on_enter", "on_exit"):
        return JSONResponse({"error": "hook must be 'on_enter' or 'on_exit'"}, 400)
    handler_name = body.get("handler", "")
    if not handler_name:
        return JSONResponse({"error": "missing 'handler' key"}, 400)
    set_lifecycle(state, hook, handler_name)
    return {"state": state, "hook": hook, "handler": handler_name}


@app.delete("/ctl/lifecycle/{state}/{hook}")
def delete_lifecycle_hook(state: str, hook: str):
    remove_lifecycle(state, hook)
    return {"removed": f"{state}/{hook}"}


@app.delete("/ctl/lifecycle/{state}")
def delete_lifecycle_state(state: str):
    remove_lifecycle(state)
    return {"removed": state}


@app.get("/ctl/lifecycle")
def get_lifecycle():
    return {"lifecycle": lifecycle_snapshot()}


# --- Handler source management ---

_HANDLERS_PATH = Path(__file__).parent / "handlers.py"


def _validate_handler_source(source: str) -> tuple[str | None, str]:
    """Validate source is a single function def with the right signature.

    Returns (error, name). On success error is None and name is the function name.
    """
    source = source.strip()
    try:
        parsed = ast.parse(source)
    except SyntaxError as e:
        return f"syntax error: {e}", ""
    defs = [n for n in parsed.body if isinstance(n, ast.FunctionDef)]
    if len(defs) != 1:
        return f"expected exactly 1 function def, got {len(defs)}", ""
    fn = defs[0]
    if fn.name.startswith("_"):
        return f"handler name {fn.name!r} must not start with _", ""
    params = [a.arg for a in fn.args.args]
    if len(params) < 2:
        return f"handler must accept (session, inp), got {params}", ""
    return None, fn.name


@app.post("/handlers")
def post_handler(body: dict):
    """POST a handler function. Body: {"source": "def my_fn(session, inp): ..."}

    Validates the source, appends it to handlers.py, reloads the module.
    Rolls back on reload failure.
    """
    source = body.get("source", "")
    if not source:
        return JSONResponse({"error": "missing 'source' key"}, 400)

    err, name = _validate_handler_source(source)
    if err:
        return JSONResponse({"error": err}, 422)

    # Backup before modifying
    backup = _HANDLERS_PATH.read_text()
    with _HANDLERS_PATH.open("a") as f:
        f.write("\n\n" + source.strip() + "\n")

    err = _safe_reload()
    if err:
        # Rollback
        _HANDLERS_PATH.write_text(backup)
        _safe_reload()
        return JSONResponse({"error": f"reload failed, rolled back: {err}"}, 422)

    return {"added": name, "registry": sorted(REGISTRY)}


@app.delete("/handlers/{name}")
def delete_handler_source(name: str):
    """Remove a handler function from handlers.py by name."""
    if name.startswith("_"):
        return JSONResponse({"error": "cannot remove private functions"}, 400)

    source = _HANDLERS_PATH.read_text()
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)

    target = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            target = node
            break

    if target is None:
        return JSONResponse({"error": f"handler {name!r} not found"}, 404)

    start = target.lineno - 1
    end = len(lines)
    for node in tree.body:
        if node.lineno > target.end_lineno:
            end = node.lineno - 1
            while end > start and lines[end - 1].strip() == "":
                end -= 1
            break

    backup = source
    kept = lines[:start] + lines[end:]
    _HANDLERS_PATH.write_text("".join(kept))

    err = _safe_reload()
    if err:
        _HANDLERS_PATH.write_text(backup)
        _safe_reload()
        return JSONResponse({"error": f"reload failed, rolled back: {err}"}, 422)

    return {"removed": name, "registry": sorted(REGISTRY)}


def _safe_reload() -> str | None:
    """Reload handlers, returning error string on failure or None on success."""
    try:
        reload_handlers()
        return None
    except Exception as e:
        return str(e)


@app.get("/handlers")
def list_handler_source():
    """List all handler functions with their source."""
    source = _HANDLERS_PATH.read_text()
    tree = ast.parse(source)
    lines = source.splitlines()
    result = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            fn_lines = lines[node.lineno - 1 : node.end_lineno]
            result[node.name] = "\n".join(fn_lines)
    return {"handlers": result}


@app.get("/ctl/status")
def status():
    config = load_config()
    sessions = list_sessions()
    return {
        "enabled": config.enabled,
        "rate_limit": config.rate_limit.model_dump(),
        "sessions": [s.model_dump() for s in sessions],
    }


@app.get("/health")
def health():
    return {"status": "ok"}
