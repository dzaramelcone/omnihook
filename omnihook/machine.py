"""Finite state machine — the core of omnihook.

MACHINE is a dict[str, dict[str, Handler]]: outer key = state, inner key = event,
value = handler function. Declarative setup, imperative callbacks.

REGISTRY maps handler names → callables. All public functions in handlers.py are
auto-registered. The MACHINE references handlers by object identity at init, but
the /ctl/machine API rewires it by name from REGISTRY — no restart required.

Transition semantics (Temporal-inspired):
  1. Load session state from durable store
  2. Look up handler for (current_state, event)
  3. Execute handler → (next_state | None, response)
  4. If next_state: update session.state
  5. Persist session to disk (write-ahead, atomic rename)
  6. Return HTTP response

If omnihook crashes between 3–5, the old state persists and the handler
re-executes on the next hook call. Handlers MUST be idempotent.
"""

import importlib
import inspect
from collections.abc import Callable

from . import handlers as h
from .models import HookInput, SessionState

Handler = Callable[[SessionState, HookInput], tuple[str | None, dict]]


def _scan_handlers() -> dict[str, Handler]:
    return {
        name: fn
        for name, fn in inspect.getmembers(h, inspect.isfunction)
        if not name.startswith("_")
    }


# Auto-register all public handler functions by name
REGISTRY: dict[str, Handler] = _scan_handlers()


def _resolve(name: str) -> Handler:
    if name not in REGISTRY:
        raise KeyError(f"unknown handler {name!r}, available: {sorted(REGISTRY)}")
    return REGISTRY[name]


MACHINE: dict[str, dict[str, Handler]] = {
    "idle": {
        "SessionStart": h.on_session_start,
        "PreToolUse": h.guard_secrets,
        "PostToolUse": h.lint_python,
        "Stop": h.passthrough,
        "SessionEnd": h.on_session_end,
    },
    "active": {
        "PreToolUse": h.guard_secrets,
        "PostToolUse": h.lint_python,
        "Stop": h.passthrough,
        "SessionEnd": h.on_session_end,
    },
}

# Defaults stored as names (not function refs) so reset works after hot-reload
_DEFAULT_LAYOUT: dict[str, dict[str, str]] = {
    "idle": {
        "SessionStart": "on_session_start",
        "PreToolUse": "guard_secrets",
        "PostToolUse": "lint_python",
        "Stop": "passthrough",
        "SessionEnd": "on_session_end",
    },
    "active": {
        "PreToolUse": "guard_secrets",
        "PostToolUse": "lint_python",
        "Stop": "passthrough",
        "SessionEnd": "on_session_end",
    },
}


def transition(session: SessionState, inp: HookInput) -> tuple[SessionState, dict]:
    """Execute one state machine step. Returns (updated session, response dict)."""
    state_handlers = MACHINE.get(session.state, {})
    handler = state_handlers.get(inp.hook_event_name, h.passthrough)
    next_state, output = handler(session, inp)
    if next_state is not None:
        session.state = next_state
    return session, output


# --- Runtime mutation ---


def snapshot() -> dict[str, dict[str, str]]:
    """Return MACHINE as {state: {event: handler_name}} for introspection."""
    fn_to_name: dict[int, str] = {id(fn): name for name, fn in REGISTRY.items()}
    return {
        state: {event: fn_to_name.get(id(fn), "?") for event, fn in handlers.items()}
        for state, handlers in MACHINE.items()
    }


def _persist():
    """Persist current machine layout to disk (atomic write)."""
    from .store import save_machine_layout

    save_machine_layout(snapshot())


def load_persisted():
    """Rebuild MACHINE from disk if a saved layout exists. Called on startup."""
    from .store import load_machine_layout

    layout = load_machine_layout()
    if layout is None:
        return
    MACHINE.clear()
    for state, handlers in layout.items():
        MACHINE[state] = {event: _resolve(name) for event, name in handlers.items()}


def set_handler(state: str, event: str, handler_name: str):
    """Rewire a single (state, event) → handler by name. Creates the state if missing."""
    reload_handlers()
    fn = _resolve(handler_name)
    MACHINE.setdefault(state, {})[event] = fn
    _persist()


def remove_handler(state: str, event: str):
    """Remove a handler. The event will fall through to passthrough."""
    if state in MACHINE:
        MACHINE[state].pop(event, None)
    _persist()


def add_state(state: str, handlers: dict[str, str]):
    """Add an entire state with {event: handler_name} mapping."""
    reload_handlers()
    MACHINE[state] = {event: _resolve(name) for event, name in handlers.items()}
    _persist()


def remove_state(state: str):
    """Remove a state entirely. Sessions in this state will fall through to passthrough."""
    MACHINE.pop(state, None)
    _persist()


def reset_machine():
    """Reset MACHINE to hardcoded defaults, clearing any persisted overrides."""
    from .store import clear_machine_layout

    reload_handlers()
    MACHINE.clear()
    for state, handlers in _DEFAULT_LAYOUT.items():
        MACHINE[state] = {event: _resolve(name) for event, name in handlers.items()}
    clear_machine_layout()


def reload_handlers():
    """Hot-reload handlers.py module and re-scan REGISTRY.

    New/changed functions become available immediately. Existing MACHINE
    entries that reference old function objects are re-linked by name.
    """
    # Snapshot BEFORE reload — old fn objects still match old REGISTRY ids
    current = snapshot()
    importlib.reload(h)
    REGISTRY.clear()
    REGISTRY.update(_scan_handlers())
    # Re-link MACHINE entries to fresh function objects by name
    MACHINE.clear()
    for state, handlers in current.items():
        MACHINE[state] = {}
        for event, name in handlers.items():
            MACHINE[state][event] = REGISTRY.get(name, h.passthrough)
