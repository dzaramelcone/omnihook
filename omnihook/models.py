from pydantic import BaseModel, Field


class HookInput(BaseModel):
    """Incoming hook event from Claude Code (HTTP POST body)."""

    session_id: str
    transcript_path: str = ""
    cwd: str = ""
    permission_mode: str = "default"
    hook_event_name: str

    # Tool events
    tool_name: str | None = None
    tool_use_id: str | None = None
    tool_input: dict | None = None
    tool_response: str | None = None
    error: str | None = None
    is_interrupt: bool | None = None

    # Session events
    source: str | None = None
    model_name: str | None = Field(None, alias="model")
    reason: str | None = None

    # Stop / Subagent events
    last_assistant_message: str | None = None
    stop_hook_active: bool | None = None
    agent_id: str | None = None
    agent_type: str | None = None

    # User prompt
    prompt: str | None = None

    model_config = {"extra": "allow"}


class SessionState(BaseModel):
    """Durable per-session state, persisted to disk after every transition."""

    session_id: str
    state: str = "idle"
    data: dict = Field(default_factory=dict)
    enabled: bool = True
    created_at: str
    updated_at: str


class RateLimit(BaseModel):
    max_calls: int = 60
    window_sec: float = 10.0


class GlobalConfig(BaseModel):
    enabled: bool = True
    rate_limit: RateLimit = Field(default_factory=RateLimit)
