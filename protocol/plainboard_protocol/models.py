from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

# "full": all built-in coding tools (shell, edit, web ...) + PlainBoard tools.
# "board": PlainBoard tools + shell and file editing. Much smaller prompt -> fewer tokens.
Toolset = Literal["full", "board"]


class ModelInfo(BaseModel):
    id: str
    label: str
    tier: Literal["small", "medium", "large", "xlarge"] = "medium"


class ProviderInfo(BaseModel):
    name: str
    label: str
    models: list[ModelInfo]
    efforts: list[str] = Field(default_factory=list)
    supports_resume: bool = True


class Credentials(BaseModel):
    """Secrets forwarded per run. Never persisted by the runner."""

    claude_auth_mode: Literal["oauth_token", "api_key", "runner_login"] = "oauth_token"
    claude_oauth_token: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    github_token: str | None = None
    git_author_name: str = "PlainBoard Agent"
    git_author_email: str = "agent@plainboard.local"


class RepoSpec(BaseModel):
    name: str  # folder name inside the workspace
    url: str
    branch: str | None = None


class WorkspaceSpec(BaseModel):
    path: str
    repos: list[RepoSpec] = Field(default_factory=list)
    shared: bool = False  # a folder also used by others (e.g. the user): checkouts are left alone
    setup_script: str = ""  # runs before the run if the runner has not run this exact script yet


class ToolDef(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    # Local tools are executed inside the runner (they need the agent's filesystem).
    local: bool = False


class ToolBridgeSpec(BaseModel):
    base_url: str
    run_id: str
    run_token: str
    tools: list[ToolDef] = Field(default_factory=list)


class McpServerSpec(BaseModel):
    """External MCP server made available to an agent (stdio runs inside the runner)."""

    name: str
    transport: Literal["stdio", "http", "sse"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class RunSpec(BaseModel):
    run_id: str
    provider: str
    model: str
    effort: str | None = None
    toolset: Toolset = "full"
    system_prompt: str
    prompt: str
    resume_session_id: str | None = None
    max_turns: int | None = None
    workspace: WorkspaceSpec
    credentials: Credentials
    bridge: ToolBridgeSpec
    mcp_servers: list[McpServerSpec] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class CompletionSpec(BaseModel):
    """Single tool-less completion, e.g. for context summaries."""

    provider: str
    model: str
    system_prompt: str
    prompt: str
    credentials: Credentials
    effort: str | None = None


class CompletionResult(BaseModel):
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class CheckResult(BaseModel):
    ok: bool
    detail: str


class ToolCallResult(BaseModel):
    text: str
    is_error: bool = False


class JobSpec(BaseModel):
    """A shell command that runs in the runner independently of any agent turn."""

    id: str = Field(pattern=r"^[0-9a-f]+$")
    command: str
    workspace: str
    credentials: Credentials


class JobState(BaseModel):
    running: bool
    exit_code: int | None = None
    log_path: str = ""
    tail: str = ""  # end of the output, once the job has finished


# ---------------------------------------------------------------------------
# Run events (runner -> app), streamed as NDJSON
# ---------------------------------------------------------------------------


class SessionEvent(BaseModel):
    type: Literal["session"] = "session"
    session_id: str


class TextDeltaEvent(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class AssistantTextEvent(BaseModel):
    type: Literal["assistant_text"] = "assistant_text"
    text: str


class ToolUseEvent(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultEvent(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str = ""
    is_error: bool = False


class UsageEvent(BaseModel):
    """Usage of one model call. ``context_tokens`` approximates the current context size."""

    type: Literal["usage"] = "usage"
    context_tokens: int


class CompactionEvent(BaseModel):
    """The provider compacted its session on its own; the session itself stays valid."""

    type: Literal["compaction"] = "compaction"
    tokens_before: int = 0


class ResultEvent(BaseModel):
    type: Literal["result"] = "result"
    session_id: str | None = None
    is_error: bool = False
    error: str | None = None
    text: str | None = None
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    context_tokens: int = 0


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    message: str
    cancelled: bool = False


RunEvent = Annotated[
    Union[
        SessionEvent,
        TextDeltaEvent,
        AssistantTextEvent,
        ToolUseEvent,
        ToolResultEvent,
        UsageEvent,
        CompactionEvent,
        ResultEvent,
        ErrorEvent,
    ],
    Field(discriminator="type"),
]

_event_adapter: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)


def parse_event(raw: str | bytes) -> RunEvent:
    return _event_adapter.validate_json(raw)
