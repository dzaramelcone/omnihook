"""Durable session store — Temporal-inspired write-ahead persistence.

Every state transition is persisted to disk BEFORE the HTTP response is sent.
On crash recovery, sessions resume from last persisted state. Atomic writes
via tmp+rename prevent partial state corruption.
"""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from .models import GlobalConfig, RateLimit, SessionState

STORE_DIR = Path.home() / ".claude" / "omnihook"
SESSIONS_DIR = STORE_DIR / "sessions"
CONFIG_PATH = STORE_DIR / "config.json"
MACHINE_PATH = STORE_DIR / "machine.json"
PID_PATH = STORE_DIR / "omnihook.pid"

_STALE_HOURS = 24


_dirs_ready = False


def _ensure_dirs():
    global _dirs_ready
    if _dirs_ready:
        return
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _dirs_ready = True


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write(path: Path, content: str):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.rename(path)


# --- Global config (cached in memory, invalidated on save) ---

_config_cache: GlobalConfig | None = None


def load_config() -> GlobalConfig:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if CONFIG_PATH.exists():
        _config_cache = GlobalConfig.model_validate_json(CONFIG_PATH.read_text())
    else:
        _config_cache = GlobalConfig()
    return _config_cache


def save_config(config: GlobalConfig):
    global _config_cache
    _ensure_dirs()
    _atomic_write(CONFIG_PATH, config.model_dump_json(indent=2))
    _config_cache = config


# --- Machine layout ---


def load_machine_layout() -> tuple[dict | None, dict | None]:
    """Load persisted machine + lifecycle layout. Returns (machine, lifecycle)."""
    if not MACHINE_PATH.exists():
        return None, None
    data = json.loads(MACHINE_PATH.read_text())
    # Support both old format (flat dict) and new format ({"machine": ..., "lifecycle": ...})
    if "machine" in data:
        return data["machine"], data.get("lifecycle", {})
    return data, {}


def save_machine_layout(
    layout: dict[str, dict[str, str]],
    lifecycle: dict[str, dict[str, str]] | None = None,
):
    _ensure_dirs()
    data = {"machine": layout, "lifecycle": lifecycle or {}}
    _atomic_write(MACHINE_PATH, json.dumps(data, indent=2))


def clear_machine_layout():
    MACHINE_PATH.unlink(missing_ok=True)


# --- Session state ---


def load_session(session_id: str) -> SessionState:
    _ensure_dirs()
    path = SESSIONS_DIR / f"{session_id}.json"
    if path.exists():
        return SessionState.model_validate_json(path.read_text())
    now = _now()
    return SessionState(session_id=session_id, created_at=now, updated_at=now)


def save_session(session: SessionState):
    _ensure_dirs()
    session.updated_at = _now()
    path = SESSIONS_DIR / f"{session.session_id}.json"
    _atomic_write(path, session.model_dump_json(indent=2))


def delete_session(session_id: str):
    path = SESSIONS_DIR / f"{session_id}.json"
    path.unlink(missing_ok=True)


def list_sessions() -> list[SessionState]:
    _ensure_dirs()
    return [
        SessionState.model_validate_json(p.read_text())
        for p in SESSIONS_DIR.glob("*.json")
    ]


def cleanup_stale():
    _ensure_dirs()
    now = datetime.now(UTC)
    for p in SESSIONS_DIR.glob("*.json"):
        session = SessionState.model_validate_json(p.read_text())
        updated = datetime.fromisoformat(session.updated_at)
        if (now - updated).total_seconds() > _STALE_HOURS * 3600:
            p.unlink()


# --- Rate limiting ---


def check_rate_limit(session: SessionState, limit: RateLimit) -> bool:
    """Fixed-window rate limiter. Returns True if the call is allowed.

    State stored in session.data["_rate"] = {"window_start": float, "count": int}.
    Uses wall-clock time (not monotonic) since state is persisted to disk.
    """
    now = time.time()
    rate = session.data.get("_rate", {"window_start": now, "count": 0})

    if now - rate["window_start"] >= limit.window_sec:
        session.data["_rate"] = {"window_start": now, "count": 1}
        return True

    if rate["count"] < limit.max_calls:
        rate["count"] += 1
        session.data["_rate"] = rate
        return True

    return False


# --- PID management ---


def write_pid():
    _ensure_dirs()
    PID_PATH.write_text(str(os.getpid()))


def clear_pid():
    PID_PATH.unlink(missing_ok=True)


def read_pid() -> int | None:
    if not PID_PATH.exists():
        return None
    text = PID_PATH.read_text().strip()
    return int(text) if text.isdigit() else None
