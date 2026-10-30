"""Pydantic models for the HTTP API, WebSocket events and service inputs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from plainboard_protocol import LimitWindow
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat() + "Z"


UTCDateTime = Annotated[datetime, PlainSerializer(_iso_utc, return_type=str, when_used="json")]
Priority = Literal["low", "medium", "high", "urgent"]
Status = Literal["backlog", "todo", "in_progress", "review", "done"]
ToolsetName = Literal["full", "board"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- repositories ---------------------------------------------------------------------------


class RepositoryCreate(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    name: str | None = Field(default=None, max_length=100, description="Folder name; default from URL")
    default_branch: str | None = None
    description: str = ""
    agent_ids: list[int] | None = None  # explicit access for agents restricted to selected repos


class RepositoryUpdate(BaseModel):
    url: str | None = Field(default=None, min_length=1, max_length=500)
    name: str | None = Field(default=None, max_length=100)
    default_branch: str | None = None
    description: str | None = None
    agent_ids: list[int] | None = None


class LimitsOut(BaseModel):
    available: bool = True  # False: this login has no plan limits at all
    windows: list[LimitWindow] = Field(default_factory=list)
    updated_at: int | None = None  # unix seconds, like the windows' resets_at


class RepositoryOut(ORM):
    id: int
    project_id: int
    name: str
    url: str
    default_branch: str | None
    description: str
    agent_ids: list[int] = Field(default_factory=list)
    created_at: UTCDateTime


# --- projects -------------------------------------------------------------------------------


WorkspacePath = Annotated[str | None, Field(max_length=500)]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    workspace_path: WorkspacePath = None
    github_token: str | None = None
    repositories: list[RepositoryCreate] = Field(default_factory=list)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    workspace_path: WorkspacePath = None
    setup_script: str | None = None
    github_token: str | None = None  # "" clears the override
    auto_wake_manager: bool | None = None


class ProjectOut(ORM):
    id: int
    name: str
    slug: str
    description: str
    workspace_path: str | None
    has_github_token: bool = False
    has_public_api_key: bool = False
    auto_wake_manager: bool
    created_at: UTCDateTime


# --- agents ---------------------------------------------------------------------------------


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: str = ""
    description: str = ""
    instructions: str = ""
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    toolset: ToolsetName = "full"
    context_limit_tokens: int | None = Field(default=None, ge=5000)
    keep_recent_messages: int | None = Field(default=None, ge=0, le=50)
    summary_model: str | None = None
    max_turns: int | None = Field(default=None, ge=1)
    color: str | None = None
    mcp_server_ids: list[int] | None = None
    all_repos: bool = True
    repository_ids: list[int] | None = None


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    role: str | None = None
    description: str | None = None
    instructions: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    toolset: ToolsetName | None = None
    context_limit_tokens: int | None = Field(default=None, ge=5000)
    keep_recent_messages: int | None = Field(default=None, ge=0, le=50)
    summary_model: str | None = None
    max_turns: int | None = Field(default=None, ge=1)
    enabled: bool | None = None
    color: str | None = None
    mcp_server_ids: list[int] | None = None
    all_repos: bool | None = None
    repository_ids: list[int] | None = None


class AgentOut(ORM):
    id: int
    project_id: int
    name: str
    slug: str
    role: str
    is_manager: bool
    description: str
    instructions: str
    provider: str
    model: str
    effort: str | None
    toolset: str
    context_limit_tokens: int
    keep_recent_messages: int
    summary_model: str
    max_turns: int | None
    enabled: bool
    color: str
    mcp_server_ids: list[int] = Field(default_factory=list)
    all_repos: bool = True
    repository_ids: list[int] = Field(default_factory=list)
    # live state
    status: Literal["idle", "queued", "running"] = "idle"
    running_jobs: int = 0
    context_tokens: int = 0
    base_context_tokens: int = 0  # fixed part of the context (system prompt, tools, memory)
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


# --- board ----------------------------------------------------------------------------------


CUSTOMER_TITLE_MAX = 150
CUSTOMER_DESCRIPTION_MAX = 400


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    customer_title: str = Field(default="", max_length=CUSTOMER_TITLE_MAX)
    customer_description: str = Field(default="", max_length=CUSTOMER_DESCRIPTION_MAX)
    status: Status = "backlog"
    priority: Priority = "medium"
    labels: list[str] = Field(default_factory=list)


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    customer_title: str | None = Field(default=None, max_length=CUSTOMER_TITLE_MAX)
    customer_description: str | None = Field(default=None, max_length=CUSTOMER_DESCRIPTION_MAX)
    status: Status | None = None
    priority: Priority | None = None
    labels: list[str] | None = None
    position: float | None = None


class TaskOut(ORM):
    id: int
    project_id: int
    number: int
    title: str
    description: str
    customer_title: str
    customer_description: str
    status: str
    position: float
    priority: str
    labels: list[str]
    created_by_agent_id: int | None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    comment_count: int = 0
    attachment_count: int = 0
    active_agent_ids: list[int] = Field(default_factory=list)  # agents with open assignments
    manager_active: bool = False
    open_decisions: int = 0


class PublicTask(BaseModel):
    number: int
    title: str
    description: str
    status: str
    updated_at: UTCDateTime


class PublicBoard(BaseModel):
    project: str
    tasks: list[PublicTask]


class PublicApiKey(BaseModel):
    key: str


class CommentCreate(BaseModel):
    body: str = Field(min_length=1)


class CommentOut(ORM):
    id: int
    task_id: int
    author_kind: str
    author_agent_id: int | None
    body: str
    created_at: UTCDateTime


class AttachmentOut(ORM):
    id: int
    task_id: int | None
    filename: str
    content_type: str
    size: int
    uploaded_by_agent_id: int | None
    created_at: UTCDateTime


class AssignmentOut(ORM):
    id: int
    project_id: int
    task_id: int | None
    agent_id: int
    created_by_agent_id: int | None
    title: str
    instructions: str
    effort: str | None = None
    status: str
    result: str | None
    run_id: int | None
    created_at: UTCDateTime
    finished_at: UTCDateTime | None


class DecisionOption(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    description: str = ""
    attachment_ids: list[int] = Field(default_factory=list, description="Files showing this option")


class DecisionAnswer(BaseModel):
    option_index: int | None = Field(default=None, ge=0)
    text: str = ""


class DecisionOut(ORM):
    id: int
    task_id: int
    requested_by_agent_id: int | None
    question: str
    details: str
    options: list[DecisionOption]
    attachment_ids: list[int]
    status: str
    answer_option: int | None
    answer_text: str
    answered_by: str | None
    answered_at: UTCDateTime | None
    created_at: UTCDateTime


class TaskDetail(TaskOut):
    comments: list[CommentOut] = Field(default_factory=list)
    attachments: list[AttachmentOut] = Field(default_factory=list)
    assignments: list[AssignmentOut] = Field(default_factory=list)
    decisions: list[DecisionOut] = Field(default_factory=list)


# --- chat & runs ----------------------------------------------------------------------------


class ChatMessageIn(BaseModel):
    text: str = ""
    attachment_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def not_empty(self) -> ChatMessageIn:
        if not self.text.strip() and not self.attachment_ids:
            raise ValueError("Message is empty")
        return self


class MessageOut(ORM):
    id: int
    conversation_id: int
    role: str
    kind: str
    content: str
    meta: dict[str, Any]
    run_id: int | None
    created_at: UTCDateTime


class ConversationOut(ORM):
    id: int
    agent_id: int
    provider_session_id: str | None
    context_tokens: int
    session_base_tokens: int = 0
    context_limit_tokens: int = 0
    compressions: int
    has_summary: bool = False
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    messages: list[MessageOut] = Field(default_factory=list)
    has_more: bool = False


class RunOut(ORM):
    id: int
    project_id: int
    agent_id: int
    trigger: str
    status: str
    input_text: str
    attachment_ids: list[int] = Field(default_factory=list)
    effort: str | None = None
    task_id: int | None
    assignment_id: int | None
    schedule_id: int | None
    source_agent_id: int | None
    parent_run_id: int | None
    result_text: str | None
    error: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    num_turns: int
    context_tokens: int
    created_at: UTCDateTime
    started_at: UTCDateTime | None
    finished_at: UTCDateTime | None
    stop_requested_at: UTCDateTime | None = None


# --- notes / schedules / mcp ----------------------------------------------------------------


class NoteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = ""
    pinned: bool = False


class NoteUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    content: str | None = None
    pinned: bool | None = None


class NoteOut(ORM):
    id: int
    project_id: int
    title: str
    content: str
    pinned: bool
    created_by_agent_id: int | None
    created_at: UTCDateTime
    updated_at: UTCDateTime


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    agent_id: int
    prompt: str = Field(min_length=1)
    kind: Literal["once", "cron"]
    run_at: datetime | None = None  # naive = in `timezone`
    cron: str | None = None
    timezone: str | None = None
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: str | None = None
    agent_id: int | None = None
    prompt: str | None = None
    kind: Literal["once", "cron"] | None = None
    run_at: datetime | None = None
    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None


class ScheduleOut(ORM):
    id: int
    project_id: int
    agent_id: int
    name: str
    prompt: str
    kind: str
    run_at: UTCDateTime | None
    cron: str | None
    timezone: str
    enabled: bool
    next_run_at: UTCDateTime | None
    last_run_at: UTCDateTime | None
    last_run_id: int | None
    created_at: UTCDateTime


class McpServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    transport: Literal["stdio", "http", "sse"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    all_agents: bool = True
    global_scope: bool = False  # available in every project


class McpServerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    transport: Literal["stdio", "http", "sse"] | None = None
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None  # values "***" keep the stored secret
    headers: dict[str, str] | None = None
    enabled: bool | None = None
    all_agents: bool | None = None


class McpServerOut(ORM):
    id: int
    project_id: int | None
    name: str
    transport: str
    command: str | None
    args: list[str]
    url: str | None
    env: dict[str, str] = Field(default_factory=dict)  # values masked
    headers: dict[str, str] = Field(default_factory=dict)  # values masked
    enabled: bool
    all_agents: bool
    agent_ids: list[int] = Field(default_factory=list)


# --- settings -------------------------------------------------------------------------------


class SettingsOut(BaseModel):
    values: dict[str, Any]  # secrets are returned as bool "is set"
    secrets: list[str]
    env_fallbacks: dict[str, bool]


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class CheckOut(BaseModel):
    ok: bool
    detail: str


# --- events ---------------------------------------------------------------------------------


class Event(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
