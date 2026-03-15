"""Durable session store — Temporal-inspired write-ahead persistence.

Every state transition is persisted to disk BEFORE the HTTP response is sent.
On crash recovery, sessions resume from last persisted state. Atomic writes
via tmp+fsync+rename prevent partial state corruption. Per-session file locks
serialize concurrent requests.
"""

import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import GlobalConfig, RateLimit, SessionState

log = logging.getLogger("omnihook")

STORE_DIR = Path.home() / ".claude" / "omnihook"
SESSIONS_DIR = STORE_DIR / "sessions"
QUARANTINE_DIR = STORE_DIR / "quarantine"
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
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    _dirs_ready = True


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write(path: Path, content: str):
    """Write content to path atomically: write tmp → fsync → rename."""
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    os.write(fd, content.encode())
    os.fsync(fd)
    os.close(fd)
    tmp.rename(path)


def _quarantine(path: Path, reason: str):
    """Move a corrupt file to quarantine/ instead of crashing."""
    dest = QUARANTINE_DIR / f"{path.name}.{int(time.time())}"
    path.rename(dest)
    log.warning("quarantined %s → %s: %s", path.name, dest.name, reason)


def _safe_parse_session(path: Path) -> SessionState | None:
    """Parse a session file, quarantining it on failure."""
    try:
        return SessionState.model_validate_json(path.read_text())
    except Exception as e:
        _quarantine(path, str(e))
        return None


# --- Per-session file locking ---


@contextmanager
def session_lock(session_id: str):
    """Advisory file lock per session — serializes concurrent hook requests."""
    _ensure_dirs()
    lock_path = SESSIONS_DIR / f"{session_id}.lock"
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
    """Load session from disk. Caller must hold session_lock."""
    _ensure_dirs()
    path = SESSIONS_DIR / f"{session_id}.json"
    if path.exists():
        return _safe_parse_session(path) or _new_session(session_id)
    return _new_session(session_id)


def _new_session(session_id: str) -> SessionState:
    now = _now()
    return SessionState(session_id=session_id, created_at=now, updated_at=now)


def save_session(session: SessionState):
    """Persist session to disk. Caller must hold session_lock."""
    _ensure_dirs()
    session.updated_at = _now()
    path = SESSIONS_DIR / f"{session.session_id}.json"
    _atomic_write(path, session.model_dump_json(indent=2))


def delete_session(session_id: str):
    path = SESSIONS_DIR / f"{session_id}.json"
    path.unlink(missing_ok=True)
    lock_path = SESSIONS_DIR / f"{session_id}.lock"
    lock_path.unlink(missing_ok=True)


def list_sessions() -> list[SessionState]:
    _ensure_dirs()
    result = []
    for p in SESSIONS_DIR.glob("*.json"):
        s = _safe_parse_session(p)
        if s:
            result.append(s)
    return result


def cleanup_stale():
    _ensure_dirs()
    now = datetime.now(UTC)
    for p in SESSIONS_DIR.glob("*.json"):
        s = _safe_parse_session(p)
        if s is None:
            continue
        updated = datetime.fromisoformat(s.updated_at)
        if (now - updated).total_seconds() > _STALE_HOURS * 3600:
            p.unlink()
            lock_path = p.with_suffix(".lock")
            lock_path.unlink(missing_ok=True)


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
