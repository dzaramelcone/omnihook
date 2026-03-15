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


# Single source of truth — MACHINE is derived from this on startup and reset
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

MACHINE: dict[str, dict[str, Handler]] = {
    state: {event: _resolve(name) for event, name in handlers.items()}
    for state, handlers in _DEFAULT_LAYOUT.items()
}

# Lifecycle hooks: {state: {"on_enter": handler, "on_exit": handler}}
# Fired on state transitions — on_exit(old_state) then on_enter(new_state).
# Same handler signature as event handlers.
LIFECYCLE: dict[str, dict[str, Handler]] = {}

_DEFAULT_LIFECYCLE: dict[str, dict[str, str]] = {}


def transition(session: SessionState, inp: HookInput) -> tuple[SessionState, dict]:
    """Execute one state machine step. Returns (updated session, response dict).

    On state change: fires on_exit for the old state, then on_enter for the new.
    Lifecycle handlers can modify session.data but cannot override the transition
    or the response — the event handler's output is what gets returned.
    """
    old_state = session.state
    state_handlers = MACHINE.get(session.state, {})
    handler = state_handlers.get(inp.hook_event_name, h.passthrough)
    next_state, output = handler(session, inp)

    if next_state is not None and next_state != old_state:
        # Fire on_exit for old state
        exit_handler = LIFECYCLE.get(old_state, {}).get("on_exit")
        if exit_handler:
            exit_handler(session, inp)
        session.state = next_state
        # Fire on_enter for new state
        enter_handler = LIFECYCLE.get(next_state, {}).get("on_enter")
        if enter_handler:
            enter_handler(session, inp)
    elif next_state is not None:
        session.state = next_state

    return session, output


# --- Runtime mutation ---


def _fn_to_name_map() -> dict[int, str]:
    return {id(fn): name for name, fn in REGISTRY.items()}


def snapshot() -> dict[str, dict[str, str]]:
    """Return MACHINE as {state: {event: handler_name}} for introspection."""
    m = _fn_to_name_map()
    return {
        state: {event: m.get(id(fn), "?") for event, fn in handlers.items()}
        for state, handlers in MACHINE.items()
    }


def lifecycle_snapshot() -> dict[str, dict[str, str]]:
    """Return LIFECYCLE as {state: {"on_enter": name, "on_exit": name}}."""
    m = _fn_to_name_map()
    return {
        state: {hook: m.get(id(fn), "?") for hook, fn in hooks.items()}
        for state, hooks in LIFECYCLE.items()
    }


def _persist():
    """Persist current machine + lifecycle layout to disk (atomic write)."""
    from .store import save_machine_layout

    save_machine_layout(snapshot(), lifecycle_snapshot())


def load_persisted():
    """Rebuild MACHINE + LIFECYCLE from disk if a saved layout exists."""
    from .store import load_machine_layout

    machine_layout, lifecycle_layout = load_machine_layout()
    if machine_layout is not None:
        MACHINE.clear()
        for state, handlers in machine_layout.items():
            MACHINE[state] = {event: _resolve(name) for event, name in handlers.items()}
    if lifecycle_layout:
        LIFECYCLE.clear()
        for state, hooks in lifecycle_layout.items():
            LIFECYCLE[state] = {hook: _resolve(name) for hook, name in hooks.items()}


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


def set_lifecycle(state: str, hook: str, handler_name: str):
    """Set a lifecycle hook: hook must be 'on_enter' or 'on_exit'."""
    reload_handlers()
    fn = _resolve(handler_name)
    LIFECYCLE.setdefault(state, {})[hook] = fn
    _persist()


def remove_lifecycle(state: str, hook: str | None = None):
    """Remove lifecycle hook(s) for a state. If hook is None, remove all."""
    if state in LIFECYCLE:
        if hook:
            LIFECYCLE[state].pop(hook, None)
            if not LIFECYCLE[state]:
                del LIFECYCLE[state]
        else:
            del LIFECYCLE[state]
    _persist()


def reset_machine():
    """Reset MACHINE + LIFECYCLE to hardcoded defaults."""
    from .store import clear_machine_layout

    reload_handlers()
    MACHINE.clear()
    for state, handlers in _DEFAULT_LAYOUT.items():
        MACHINE[state] = {event: _resolve(name) for event, name in handlers.items()}
    LIFECYCLE.clear()
    for state, hooks in _DEFAULT_LIFECYCLE.items():
        LIFECYCLE[state] = {hook: _resolve(name) for hook, name in hooks.items()}
    clear_machine_layout()


def reload_handlers():
    """Hot-reload handlers.py module and re-scan REGISTRY.

    New/changed functions become available immediately. Existing MACHINE
    and LIFECYCLE entries are re-linked by name to fresh function objects.
    """
    current_machine = snapshot()
    current_lifecycle = lifecycle_snapshot()
    importlib.reload(h)
    REGISTRY.clear()
    REGISTRY.update(_scan_handlers())
    MACHINE.clear()
    for state, handlers in current_machine.items():
        MACHINE[state] = {}
        for event, name in handlers.items():
            MACHINE[state][event] = REGISTRY.get(name, h.passthrough)
    LIFECYCLE.clear()
    for state, hooks in current_lifecycle.items():
        LIFECYCLE[state] = {}
        for hook, name in hooks.items():
            LIFECYCLE[state][hook] = REGISTRY.get(name, h.passthrough)
