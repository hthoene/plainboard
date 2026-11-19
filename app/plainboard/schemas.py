"""Pydantic models for the HTTP API, WebSocket events and service inputs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from plainboard_protocol import LimitWindow
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator

from .models import ProjectRole


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


# --- users ----------------------------------------------------------------------------------


class UserOut(ORM):
    id: int
    username: str
    name: str
    is_admin: bool
    may_create_projects: bool
    enabled: bool
    avatar_version: int = 0  # 0: no picture; otherwise part of its address (/api/avatars/{id}?v=...)
    created_at: UTCDateTime


class PersonOut(ORM):
    """What everyone signed in may know about a user."""

    id: int
    username: str
    name: str
    avatar_version: int = 0


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=60)
    name: str = Field(default="", max_length=100)
    password: str = Field(min_length=8, max_length=200)
    is_admin: bool = False
    may_create_projects: bool = False


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    is_admin: bool | None = None
    may_create_projects: bool | None = None
    enabled: bool | None = None


class MemberIn(BaseModel):
    role: ProjectRole


class MemberOut(BaseModel):
    user_id: int
    username: str
    name: str
    role: ProjectRole


# --- repositories ---------------------------------------------------------------------------


RepoAuth = Literal["default", "token", "ssh"]


class RepositoryCreate(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    name: str | None = Field(default=None, max_length=100, description="Folder name; default from URL")
    default_branch: str | None = None
    description: str = ""
    check_command: str = Field(default="", max_length=2000)  # decides whether finished branches may land
    quick_check_command: str = Field(default="", max_length=2000)  # run on a branch when it is handed over
    land_first: bool = False  # merge finished branches at once, check the base branch afterwards
    tidy_branches: bool = False  # remove branches that are fully merged, once a day
    agent_ids: list[int] | None = None  # explicit access for agents restricted to selected repos
    auth: RepoAuth = "default"
    token: str | None = None  # write-only
    token_user: str = Field(default="", max_length=200)
    ssh_private_key: str | None = None  # write-only; empty with auth "ssh": a key pair is created


class RepositoryUpdate(BaseModel):
    url: str | None = Field(default=None, min_length=1, max_length=500)
    name: str | None = Field(default=None, max_length=100)
    default_branch: str | None = None
    description: str | None = None
    check_command: str | None = Field(default=None, max_length=2000)
    quick_check_command: str | None = Field(default=None, max_length=2000)
    land_first: bool | None = None
    tidy_branches: bool | None = None
    agent_ids: list[int] | None = None
    auth: RepoAuth | None = None
    token: str | None = None  # write-only; empty keeps the stored one
    token_user: str | None = Field(default=None, max_length=200)
    ssh_private_key: str | None = None  # write-only; empty keeps the stored key
    new_ssh_key: bool = False  # replace the key pair with a new one


class LimitForecast(BaseModel):
    name: str  # the weekly window this is about
    status: Literal["ok", "tight", "over"]
    daily: float  # expected use per day, as a fraction of the limit
    budget: float  # use per day that would last until the reset
    projected: float  # expected utilization at the reset; can pass 1
    runs_out_at: int | None = None  # unix seconds
    basis_days: int = 0  # complete days behind `daily`; 0: the pace of this week so far


class AccountLimits(BaseModel):
    account_id: int
    provider: str
    label: str
    available: bool = True  # False: this login has no plan limits at all (an API key)
    windows: list[LimitWindow] = Field(default_factory=list)
    forecasts: list[LimitForecast] = Field(default_factory=list)
    updated_at: int | None = None  # unix seconds, like the windows' resets_at
    plan: str | None = None  # the plan as the provider names it, where it can tell
    plan_size: float | None = None  # its size as the provider says (Account.plan_size: as the user gave it)


class LimitsOut(BaseModel):
    accounts: list[AccountLimits] = Field(default_factory=list)  # the enabled accounts


# --- disk and memory ------------------------------------------------------------------------


class VolumeOut(BaseModel):
    kinds: list[str]  # what is on it: data, uploads, workspaces, home
    total: int
    used: int
    free: int
    low: bool  # little room left


class AgentUsageOut(BaseModel):
    id: int
    name: str
    disk: int | None = None  # its workspace; None: not measured yet, or a folder its project shares
    memory: int = 0  # of the processes working in its workspace: its turn, jobs, servers
    processes: int = 0


class ProjectUsageOut(BaseModel):
    id: int
    name: str
    agents: list[AgentUsageOut]
    uploads: int  # its attachments
    other_disk: int  # a folder all its agents share, folders of former agents, the clones its branches land in
    other_memory: int


class FolderOut(BaseModel):
    name: str
    bytes: int


class UsageOut(BaseModel):
    runner_ok: bool
    measured_at: UTCDateTime | None = None  # when the folders were last measured
    measuring: bool = False
    volumes: list[VolumeOut] = Field(default_factory=list)
    projects: list[ProjectUsageOut] = Field(default_factory=list)
    shared: list[FolderOut] = Field(default_factory=list)  # parts of the agents' home: caches, toolchains
    other_disk: int = 0  # folders in the workspaces of no project
    data: int | None = None  # the database and the key, in the app's data folder
    memory_total: int = 0
    memory_used: int = 0
    memory_other: int = 0  # processes of the runner that work in no agent's folder: the runner itself, browsers


# --- provider accounts ----------------------------------------------------------------------


class AccountCreate(BaseModel):
    provider: str
    label: str = Field(default="", max_length=100)
    auth_mode: str
    secret: str | None = None  # token or API key
    endpoint_url: str = Field(default="", max_length=500)
    models: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=200)
    context_tokens: int = Field(default=0, ge=0, le=10_000_000)
    plan_size: float | None = Field(default=None, gt=0, le=1000)


class AccountUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=100)
    auth_mode: str | None = None
    secret: str | None = None  # None = keep, "" = clear (then the environment's applies)
    enabled: bool | None = None
    endpoint_url: str | None = Field(default=None, max_length=500)
    models: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(default=None, max_length=200)
    context_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    plan_size: float | None = Field(default=None, ge=0, le=1000)  # 0 = as the provider says


class AccountOut(ORM):
    id: int
    provider: str
    label: str
    auth_mode: str
    enabled: bool
    secret_set: bool = False
    secret_from_env: bool = False  # no own secret, but the environment has one for this mode
    endpoint_url: str = ""
    models: list[str] = []
    context_tokens: int = 0
    plan_size: float | None = None


class RepositoryOut(ORM):
    id: int
    project_id: int
    name: str
    url: str
    default_branch: str | None
    description: str
    check_command: str = ""
    quick_check_command: str = ""
    land_first: bool = False
    tidy_branches: bool = False
    agent_ids: list[int] = Field(default_factory=list)
    auth: RepoAuth = "default"
    token_user: str = ""
    has_token: bool = False
    ssh_public_key: str = ""  # to add at the git host, e.g. as a deploy key
    created_at: UTCDateTime


# --- projects -------------------------------------------------------------------------------


WorkspacePath = Annotated[str | None, Field(max_length=500)]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    workspace_path: WorkspacePath = None
    github_token: str | None = None
    parallel_agents: int | None = Field(default=None, ge=0, le=20)
    account_id: int | None = None
    repositories: list[RepositoryCreate] = Field(default_factory=list)
    first_steps: bool = False  # start with the two tickets that set a new project up (services/projects.FIRST_STEPS)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    language: str | None = Field(default=None, max_length=50)  # "" : the language each person writes in
    workspace_path: WorkspacePath = None
    setup_script: str | None = None
    github_token: str | None = None  # "" clears the override
    auto_wake_manager: bool | None = None
    continue_with_backlog: bool | None = None
    parallel_agents: int | None = Field(default=None, ge=0, le=20)  # null: the number from the settings again
    account_id: int | None = None  # null: the subscription with the most room again
    auto_archive_days: int | None = Field(default=None, ge=0, le=3650)
    review_days: int | None = Field(default=None, ge=0, le=365)


class ProjectAccountsIn(BaseModel):
    account_ids: list[int] = Field(max_length=200)  # the only accounts the project may use; empty: any


class ProjectOut(ORM):
    id: int
    name: str
    slug: str
    description: str
    language: str = ""
    paused_at: UTCDateTime | None = None  # paused for good by a person, since then
    paused_by: str = ""
    workspace_path: str | None
    has_github_token: bool = False
    auto_wake_manager: bool
    continue_with_backlog: bool = True
    parallel_agents: int | None = None
    account_id: int | None = None
    account_ids: list[int] = []  # the only accounts it may use, set by an admin; empty: any
    role: ProjectRole | None = None  # the asking user's role; not part of events, which go to everyone
    auto_archive_days: int
    review_days: int = 3
    label_colors: dict[str, str] = Field(default_factory=dict)
    created_at: UTCDateTime


# --- secrets --------------------------------------------------------------------------------


class SecretCreate(BaseModel):
    name: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$", max_length=100, description="Environment variable name")
    value: str = Field(min_length=1)
    description: str = Field(default="", max_length=300)


class SecretUpdate(BaseModel):
    value: str | None = None  # empty = keep
    description: str | None = Field(default=None, max_length=300)


class SecretOut(ORM):
    id: int
    project_id: int | None
    name: str
    description: str
    updated_at: UTCDateTime


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
    summary_effort: str | None = None  # "" or None: the installation's
    max_turns: int | None = Field(default=None, ge=1)
    color: str | None = None
    mcp_server_ids: list[int] | None = None
    all_repos: bool = True
    repository_ids: list[int] | None = None
    account_id: int | None = None  # None = automatic


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
    summary_effort: str | None = None  # "" or None: the installation's
    max_turns: int | None = Field(default=None, ge=1)
    enabled: bool | None = None
    color: str | None = None
    mcp_server_ids: list[int] | None = None
    all_repos: bool | None = None
    repository_ids: list[int] | None = None
    account_id: int | None = None  # None = automatic


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
    summary_effort: str | None
    max_turns: int | None
    enabled: bool
    color: str
    mcp_server_ids: list[int] = Field(default_factory=list)
    all_repos: bool = True
    repository_ids: list[int] = Field(default_factory=list)
    account_id: int | None = None
    # live state
    status: Literal["idle", "queued", "running", "background"] = "idle"
    running_jobs: int = 0
    job_commands: list[str] = Field(default_factory=list)  # what the running jobs are (shortened)
    open_todos: int = 0
    own_notes: int = 0
    context_tokens: int = 0
    base_context_tokens: int = 0  # fixed part of the context (system prompt, tools, memory)
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


# --- board ----------------------------------------------------------------------------------


CUSTOMER_TITLE_MAX = 150
CUSTOMER_DESCRIPTION_MAX = 400
USER_ACTION_MAX = 500
LabelColor = Literal["gray", "red", "orange", "amber", "green", "teal", "blue", "indigo", "purple", "pink"]


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
    user_action: str | None = Field(default=None, max_length=USER_ACTION_MAX)
    archived: bool | None = None


class UserActionDone(BaseModel):
    note: str = ""


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
    user_action: str
    archived_at: UTCDateTime | None = None
    created_by_agent_id: int | None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    comment_count: int = 0
    attachment_count: int = 0
    active_agent_ids: list[int] = Field(default_factory=list)  # agents with open assignments
    manager_active: bool = False
    open_decisions: int = 0


class StatsDay(BaseModel):
    date: str  # UTC
    tickets_created: int = 0
    tickets_done: int = 0
    runs: int = 0
    failed_runs: int = 0
    tokens: int = 0  # input + output
    cost_usd: float = 0.0
    # How fast work reaches the base branch (see StatsPeriod).
    branches_landed: int = 0
    landing_seconds: int | None = None  # median from handing a branch over to it being on the base branch
    check_runs: int = 0
    check_runs_failed: int = 0
    assignments_done: int = 0
    repairs: int = 0  # of those: repairs for landing (a failed check run, a conflict)
    updates: int = 0  # times PlainBoard itself started with another version


class StatsPeriod(BaseModel):
    """What a period brought and what it cost: the same numbers for the period shown and the one
    before it, so that a change (a new version of PlainBoard, another way of working) can be seen."""

    tickets_done: int = 0
    branches_landed: int = 0
    landing_seconds: int | None = None
    check_runs: int = 0
    check_runs_failed: int = 0
    assignments_done: int = 0
    repairs: int = 0
    cost_usd: float = 0.0


class FlowFinding(BaseModel):
    """Something PlainBoard's own checks found that slows the team down (services/flow_check.py)."""

    key: str
    title: str
    detail: str
    action: str


class StatsVersion(BaseModel):
    at: UTCDateTime
    commit: str


class StatsAgent(BaseModel):
    agent_id: int
    runs: int = 0
    failed_runs: int = 0
    assignments_done: int = 0
    busy_seconds: int = 0
    tokens: int = 0
    cost_usd: float = 0.0


class StatsOut(BaseModel):
    days: list[StatsDay]
    agents: list[StatsAgent]
    open_tickets: int
    median_cycle_seconds: int | None = None  # from starting work on a ticket to done
    current: StatsPeriod = Field(default_factory=StatsPeriod)
    previous: StatsPeriod = Field(default_factory=StatsPeriod)  # the same number of days right before
    versions: list[StatsVersion] = Field(default_factory=list)  # PlainBoard's own updates in the period


class PublicTask(BaseModel):
    number: int
    title: str
    description: str
    status: str
    updated_at: UTCDateTime


class PublicBoard(BaseModel):
    project: str
    tasks: list[PublicTask]


class PublicApiKeyIn(BaseModel):
    label: str = Field(default="", max_length=200)


class PublicApiKeyOut(ORM):
    id: int
    project_id: int
    label: str
    hint: str  # last characters of the key
    created_at: UTCDateTime
    last_used_at: UTCDateTime | None
    key: str | None = None  # only in the answer that creates the key


class AccessKeyOut(ORM):
    id: int
    label: str
    hint: str  # last characters of the key
    created_at: UTCDateTime
    last_used_at: UTCDateTime | None
    key: str | None = None  # only in the answer that creates the key


class CommentCreate(BaseModel):
    body: str = Field(min_length=1)


class CommentOut(ORM):
    id: int
    task_id: int
    author_kind: str
    author_agent_id: int | None
    author_name: str | None = None
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
    # Where the ticket stands, kept by PlainBoard from the reports ("" while it has only a few).
    state: str = ""
    state_updated_at: UTCDateTime | None = None
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
    author_name: str | None = None  # a chat message: the person who wrote it
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
    read_at: UTCDateTime | None = None  # a waiting chat message: when its busy agent read it


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


class AgentNoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class AgentNoteOut(ORM):
    id: int
    project_id: int
    agent_id: int
    text: str
    created_by_agent_id: int | None
    created_at: UTCDateTime
    updated_at: UTCDateTime


TodoState = Literal["open", "done", "archived"]


class TodoCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", description="Markdown, as detailed as needed")


class TodoUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    done: bool | None = None
    agent_id: int | None = None


class TodoOut(ORM):
    id: int
    project_id: int
    agent_id: int
    title: str
    description: str
    done_at: UTCDateTime | None
    created_by_agent_id: int | None
    created_at: UTCDateTime
    updated_at: UTCDateTime


class TodoPage(BaseModel):
    items: list[TodoOut]
    total: int


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    agent_id: int
    prompt: str = Field(min_length=1)
    kind: Literal["once", "cron"]
    run_at: datetime | None = None  # naive = in `timezone`
    cron: str | None = None
    timezone: str | None = None
    enabled: bool = True
    skip_quiet: bool = False  # leave out a run when nothing happened in the project since the last one


class ScheduleUpdate(BaseModel):
    name: str | None = None
    agent_id: int | None = None
    prompt: str | None = None
    kind: Literal["once", "cron"] | None = None
    run_at: datetime | None = None
    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    skip_quiet: bool | None = None


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
    skip_quiet: bool = False
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


class ExportIn(BaseModel):
    passphrase: str = ""  # seals the credentials into the file; without one they stay behind
    chats: bool = True


class ImportOut(BaseModel):
    project: ProjectOut
    counts: dict[str, int]  # what came along, by table
    missing: list[str]  # credentials that did not, to enter again


class PauseMoving(BaseModel):
    """Something that still moves while everything is being paused."""

    what: Literal["turn", "job", "check"]  # an agent's turn, a job an assignment waits for, a check of finished branches
    project: str
    agent: str = ""
    detail: str = ""


class PauseOut(BaseModel):
    paused: bool
    by: str = ""
    since: UTCDateTime | None = None
    standing: bool = False  # paused, and nothing moves any more: PlainBoard can be restarted
    moving: list[PauseMoving] = Field(default_factory=list)
    servers: int = 0  # servers (previews) agents left running: they end if the runner restarts


class PreviewOut(BaseModel):
    """A web server an agent has started and left running, as the UI lists it."""

    port: int
    title: str  # of the page it starts with; may be empty
    local: bool  # listens on 127.0.0.1 only: it opens in the browser window, not at /preview/<port>/
    base: bool = False  # it has /preview-base/<port>/ as its base path: that is where it opens
    job_id: int
    agent_id: int
    command: str
    started_at: UTCDateTime
    access: Literal["private", "password", "public"]  # who opens it without the login (share_preview)
    shared_until: UTCDateTime | None = None


# --- events ---------------------------------------------------------------------------------


class Event(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
