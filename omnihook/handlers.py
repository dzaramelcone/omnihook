"""Hook handlers — pure functions: (SessionState, HookInput) -> (next_state | None, response_dict).

Handlers are idempotent. If omnihook crashes mid-transition and the handler
re-executes on recovery, the outcome is identical (Temporal activity semantics).

Add your own handlers here. Every public function is auto-registered by name
and can be wired into any (state, event) slot via the /ctl/machine API or
POST /handlers.
"""

import re
import subprocess

from .models import HookInput, SessionState

# --- Guard patterns ---

_SECRETS = re.compile(r"(\.env|\.pem|\.key|credentials|secrets)")


# --- Shared / stateless handlers ---


def passthrough(session: SessionState, inp: HookInput) -> tuple[str | None, dict]:
    return None, {}


def guard_secrets(session: SessionState, inp: HookInput) -> tuple[str | None, dict]:
    """PreToolUse: deny reads/writes to secret files (.env, .pem, .key, etc.)."""
    tool_input = inp.tool_input or {}
    tool_name = inp.tool_name or ""
    path = tool_input.get("file_path", "")

    if tool_name == "Grep":
        path = tool_input.get("path", "")

    if _SECRETS.search(path):
        return None, {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"refusing to access {path} — secrets must not be exposed to the model. "
                    "To adjust guards: `omnihook machine` to see handlers, "
                    "`omnihook disable` to pause all hooks."
                ),
            }
        }
    return None, {}


def lint_python(session: SessionState, inp: HookInput) -> tuple[str | None, dict]:
    """PostToolUse: run ruff lint + format on Python files after edit."""
    tool_input = inp.tool_input or {}
    file_path = tool_input.get("file_path", "")
    if not file_path.endswith(".py"):
        return None, {}

    cwd = inp.cwd or None
    subprocess.run(["ruff", "check", "--fix", file_path], cwd=cwd, capture_output=True)
    subprocess.run(["ruff", "format", file_path], cwd=cwd, capture_output=True)
    return None, {}


# --- Lifecycle handlers ---


def activate(session: SessionState, inp: HookInput) -> tuple[str | None, dict]:
    """Transition idle → active. Used as the default handler for all events in idle state."""
    return "active", {}


def on_session_end(session: SessionState, inp: HookInput) -> tuple[str | None, dict]:
    return "ended", {}


def greet(session: SessionState, inp: HookInput) -> tuple[str | None, dict]:
    """on_enter for active: greet once per session."""
    if session.data.get("greeted"):
        return None, {}
    session.data["greeted"] = True
    return None, {
        "systemMessage": (
            "[omnihook] hooks active — `omnihook status` | `omnihook disable` | `omnihook machine`"
        ),
    }
