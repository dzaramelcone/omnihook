"""Finite state machine — the core of omnihook.

MACHINE is a dict[str, dict[str, Handler]]: outer key = state, inner key = event,
value = handler function. Declarative setup, imperative callbacks.

REGISTRY maps handler names → callables. All public functions in handlers.py are
auto-registered. The MACHINE references handlers by object identity at init, but
the /ctl/machine API rewires it by name from REGISTRY — no restart required.

Concurrency model: MACHINE, LIFECYCLE, and REGISTRY are replaced atomically
(single reference swap) on every mutation. The hot path (transition) snapshots
the reference at entry, so it always sees a consistent view — either all-old
or all-new, never partial.

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
import logging
from collections.abc import Callable

from . import handlers as h
from .models import HookInput, SessionState

log = logging.getLogger("omnihook")

Handler = Callable[[SessionState, HookInput], tuple[str | None, dict]]

# Names of handlers that only do a transition (re-dispatch target event after)
_TRANSITION_ONLY = frozenset({"activate", "passthrough"})


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
        "SessionStart": "activate",
        "PreToolUse": "activate",
        "PostToolUse": "activate",
        "Stop": "activate",
        "SessionEnd": "on_session_end",
    },
    "active": {
        "PreToolUse": "guard_secrets",
        "PostToolUse": "lint_python",
        "Stop": "passthrough",
        "SessionEnd": "on_session_end",
    },
}

_DEFAULT_LIFECYCLE: dict[str, dict[str, str]] = {
    "active": {"on_enter": "greet"},
}


def _safe_resolve(name: str) -> Handler:
    """Resolve a handler name, falling back to passthrough for unknown names."""
    return REGISTRY.get(name, h.passthrough)


def _build(layout: dict[str, dict[str, str]]) -> dict[str, dict[str, Handler]]:
    """Build a handler dict from a name-based layout. Unknown names → passthrough."""
    return {
        state: {key: _safe_resolve(name) for key, name in handlers.items()}
        for state, handlers in layout.items()
    }


MACHINE: dict[str, dict[str, Handler]] = _build(_DEFAULT_LAYOUT)
LIFECYCLE: dict[str, dict[str, Handler]] = _build(_DEFAULT_LIFECYCLE)


def _safe_call(
    handler: Handler, session: SessionState, inp: HookInput
) -> tuple[str | None, dict]:
    """Call a handler with failure containment. On error, return passthrough + error message."""
    try:
        return handler(session, inp)
    except Exception as e:
        fn_name = getattr(handler, "__name__", "?")
        log.error("handler %s raised: %s", fn_name, e)
        return None, {
            "systemMessage": (
                f"[omnihook] handler '{fn_name}' failed: {e}. "
                "Run `omnihook machine` to inspect, "
                "`omnihook disable` to pause hooks."
            ),
        }


def _apply_transition(
    session: SessionState,
    inp: HookInput,
    next_state: str,
    output: dict,
    lifecycle: dict[str, dict[str, Handler]],
) -> dict:
    """Apply a state transition: fire on_exit, update state, fire on_enter, merge outputs."""
    old_state = session.state
    if next_state == old_state:
        return output
    exit_handler = lifecycle.get(old_state, {}).get("on_exit")
    if exit_handler:
        _, exit_output = _safe_call(exit_handler, session, inp)
        output = {**output, **exit_output}
    session.state = next_state
    enter_handler = lifecycle.get(next_state, {}).get("on_enter")
    if enter_handler:
        _, enter_output = _safe_call(enter_handler, session, inp)
        output = {**output, **enter_output}
    return output


def transition(session: SessionState, inp: HookInput) -> tuple[SessionState, dict]:
    """Execute one state machine step. Returns (updated session, response dict).

    Snapshots MACHINE and LIFECYCLE at entry for a consistent view —
    concurrent mutations swap the module globals atomically (single reference),
    so in-flight transitions always see either all-old or all-new.
    """
    # Snapshot references — immune to concurrent swaps
    machine = MACHINE
    lifecycle = LIFECYCLE

    state_handlers = machine.get(session.state, {})
    handler = state_handlers.get(inp.hook_event_name)
    handler_name = getattr(handler, "__name__", None) if handler else None
    if handler is None:
        log.debug(
            "no handler for (%s, %s) — passthrough",
            session.state,
            inp.hook_event_name,
        )
        handler = h.passthrough
        handler_name = "passthrough"
    next_state, output = _safe_call(handler, session, inp)

    if next_state is not None:
        output = _apply_transition(session, inp, next_state, output, lifecycle)

        # Re-dispatch: if the handler only did a transition (e.g. activate),
        # run the real handler in the new state
        if handler_name in _TRANSITION_ONLY:
            new_handlers = machine.get(session.state, {})
            real_handler = new_handlers.get(inp.hook_event_name)
            if real_handler and real_handler is not handler:
                _, real_output = _safe_call(real_handler, session, inp)
                output = {**output, **real_output}

    return session, output


# --- Runtime mutation ---
# All mutations build new dicts and swap the module global in one shot.
# The hot path (transition) snapshots the reference at entry.


def _fn_to_name_map() -> dict[int, str]:
    return {id(fn): name for name, fn in REGISTRY.items()}


def _snapshot_of(mapping: dict[str, dict[str, Handler]]) -> dict[str, dict[str, str]]:
    """Convert a handler-function dict to a handler-name dict for serialization."""
    m = _fn_to_name_map()
    return {
        state: {key: m.get(id(fn), "?") for key, fn in handlers.items()}
        for state, handlers in mapping.items()
    }


def snapshot() -> dict[str, dict[str, str]]:
    return _snapshot_of(MACHINE)


def lifecycle_snapshot() -> dict[str, dict[str, str]]:
    return _snapshot_of(LIFECYCLE)


def _persist():
    """Persist current machine + lifecycle layout to disk (atomic write)."""
    from .store import save_machine_layout

    save_machine_layout(snapshot(), lifecycle_snapshot())


def _swap_machine(new: dict[str, dict[str, Handler]]):
    """Atomically swap MACHINE contents. Concurrent readers see old or new, never partial."""
    global MACHINE
    MACHINE = new


def _swap_lifecycle(new: dict[str, dict[str, Handler]]):
    global LIFECYCLE
    LIFECYCLE = new


def load_persisted():
    """Rebuild MACHINE + LIFECYCLE from disk if a saved layout exists.

    Unknown handler names fall back to passthrough (not crash).
    Corrupt machine.json is quarantined.
    """
    from .store import load_machine_layout

    try:
        machine_layout, lifecycle_layout = load_machine_layout()
    except Exception as e:
        log.error("corrupt machine.json, using defaults: %s", e)
        from .store import MACHINE_PATH, _quarantine

        if MACHINE_PATH.exists():
            _quarantine(MACHINE_PATH, str(e))
        return

    if machine_layout is not None:
        _swap_machine(_build(machine_layout))
    if lifecycle_layout:
        _swap_lifecycle(_build(lifecycle_layout))


def set_handler(state: str, event: str, handler_name: str):
    """Rewire a single (state, event) → handler by name. Creates the state if missing."""
    reload_handlers()
    fn = _resolve(handler_name)
    new = {s: dict(h) for s, h in MACHINE.items()}
    new.setdefault(state, {})[event] = fn
    _swap_machine(new)
    _persist()


def remove_handler(state: str, event: str):
    """Remove a handler. The event will fall through to passthrough."""
    new = {s: dict(h) for s, h in MACHINE.items()}
    if state in new:
        new[state].pop(event, None)
    _swap_machine(new)
    _persist()


def add_state(state: str, handlers: dict[str, str]):
    """Add an entire state with {event: handler_name} mapping."""
    reload_handlers()
    new = {s: dict(h) for s, h in MACHINE.items()}
    new[state] = {event: _resolve(name) for event, name in handlers.items()}
    _swap_machine(new)
    _persist()


def remove_state(state: str):
    """Remove a state entirely. Sessions in this state will fall through to passthrough."""
    new = {s: dict(h) for s, h in MACHINE.items() if s != state}
    _swap_machine(new)
    _persist()


def set_lifecycle(state: str, hook: str, handler_name: str):
    """Set a lifecycle hook: hook must be 'on_enter' or 'on_exit'."""
    reload_handlers()
    fn = _resolve(handler_name)
    new = {s: dict(h) for s, h in LIFECYCLE.items()}
    new.setdefault(state, {})[hook] = fn
    _swap_lifecycle(new)
    _persist()


def remove_lifecycle(state: str, hook: str | None = None):
    """Remove lifecycle hook(s) for a state. If hook is None, remove all."""
    new = {s: dict(h) for s, h in LIFECYCLE.items()}
    if state in new:
        if hook:
            new[state].pop(hook, None)
            if not new[state]:
                del new[state]
        else:
            del new[state]
    _swap_lifecycle(new)
    _persist()


def reset_machine():
    """Reset MACHINE + LIFECYCLE to hardcoded defaults."""
    from .store import clear_machine_layout

    reload_handlers()
    _swap_machine(_build(_DEFAULT_LAYOUT))
    _swap_lifecycle(_build(_DEFAULT_LIFECYCLE))
    clear_machine_layout()


def reload_handlers():
    """Hot-reload handlers.py module and re-scan REGISTRY.

    New/changed functions become available immediately. MACHINE and LIFECYCLE
    are rebuilt from their current name-based snapshots with fresh function objects.
    """
    global REGISTRY
    current_machine = snapshot()
    current_lifecycle = lifecycle_snapshot()
    importlib.reload(h)
    REGISTRY = _scan_handlers()
    _swap_machine(_build(current_machine))
    _swap_lifecycle(_build(current_lifecycle))
