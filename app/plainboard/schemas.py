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
    # What the projects this person owns may use (services/permissions.py); null: no limit.
    max_projects: int | None = None
    account_ids: list[int] | None = None
    runner_ids: list[int] | None = None  # 0: the built-in runner
    max_parallel: int | None = None
    may_use_shared: bool = False
    enabled: bool
    avatar_version: int = 0  # 0: no picture; otherwise part of its address (/api/avatars/{id}?v=...)
    show_mentions: bool = True
    created_at: UTCDateTime


class PersonOut(ORM):
    """What everyone signed in may know about a user."""

    id: int
    username: str
    name: str
    avatar_version: int = 0


class MentionOut(ORM):
    """Someone named the user with @username (services/mentions.py)."""

    id: int
    project_id: int
    project_name: str = ""
    kind: str  # chat | comment | decision
    task_id: int | None = None
    task_number: int | None = None
    task_title: str = ""
    agent_id: int | None = None  # the chat it is in
    author: str = ""
    excerpt: str = ""
    read_at: UTCDateTime | None = None
    created_at: UTCDateTime


class MentionsOut(BaseModel):
    unread: int
    items: list[MentionOut]


class MentionsRead(BaseModel):
    ids: list[int] | None = Field(default=None, max_length=500)  # None: all


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=60)
    name: str = Field(default="", max_length=100)
    password: str = Field(min_length=8, max_length=200)
    is_admin: bool = False
    may_create_projects: bool = False


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    username: str | None = Field(default=None, max_length=100)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    is_admin: bool | None = None
    may_create_projects: bool | None = None
    may_use_shared: bool | None = None
    enabled: bool | None = None
    # Sent: set to this, null being no limit. Not sent: as it is.
    max_projects: int | None = Field(default=None, ge=0, le=1000)
    account_ids: list[int] | None = Field(default=None, max_length=200)
    runner_ids: list[int] | None = Field(default=None, max_length=200)
    max_parallel: int | None = Field(default=None, ge=0, le=100)


class MemberIn(BaseModel):
    role: ProjectRole


class MemberOut(BaseModel):
    user_id: int
    username: str
    name: str
    role: ProjectRole
    project_owner: bool = False  # the person whose limits hold for the project (Project.owner_id), not the role


class OwnerChangeOut(BaseModel):
    """What changes for a project if this person owned it (services/ownership.py)."""

    user_id: int
    name: str
    runner: str  # the project's runner; "": the built-in one
    runner_allowed: bool  # False: the change is refused until the project moves to another runner
    lost_accounts: list[str]  # subscriptions the project may use now and then no longer
    unpinned: list[str]  # "the project" and agents whose chosen subscription is then chosen automatically
    loses_shared: bool  # the installation's secrets, GitHub token and MCP servers
    max_parallel_before: int | None
    max_parallel_after: int | None


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
    tidy_branches: bool = True  # remove branches whose content is all on the base branch (orchestration/tidy.py)
    check_target_seconds: int = Field(default=0, ge=0, le=7 * 86400)  # how long a check run should take; 0: no target
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
    check_target_seconds: int | None = Field(default=None, ge=0, le=7 * 86400)  # 0: no target
    checks_baseline_seconds: int | None = Field(default=None, ge=0, le=7 * 86400)  # the verdict of a speed-up: what a check run takes now
    checks_verdict: str | None = Field(default=None, max_length=300)
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
    cpu: float = 0  # cores in use now
    cpu_avg: list[float] = Field(default_factory=list)  # cores used on average over the last 1, 6 and 24 hours
    cpu_history: list[float] = Field(default_factory=list)  # cores per slot, aligned with UsageOut.cpu_start


class ProjectUsageOut(BaseModel):
    id: int
    name: str
    agents: list[AgentUsageOut]
    uploads: int  # its attachments
    other_disk: int  # a folder all its agents share, folders of former agents, the clones its branches land in
    other_memory: int
    other_cpu: float = 0  # the same for what runs in no agent's folder of it: shared folder, former agents, landing checks
    other_cpu_avg: list[float] = Field(default_factory=list)
    other_cpu_history: list[float] = Field(default_factory=list)


class FolderOut(BaseModel):
    name: str
    bytes: int


class CleanedOut(BaseModel):
    path: str
    what: str  # build | cache | folder | logs | server
    bytes: int = 0


class CleanupOut(BaseModel):
    """A cleanup (services/cleanup.py): by the manager, in an emergency by itself, or servers stopped for memory."""

    at: UTCDateTime
    level: str  # manager | emergency | memory
    freed: int = 0
    free_before: int = 0
    total: int = 0
    items: list[CleanedOut] = Field(default_factory=list)  # the biggest
    kept: list[str] = Field(default_factory=list)  # folders left alone, with the reason
    told: bool = False  # the manager was told
    by: str = ""  # the member who deleted it
    project: str = ""


class StorageNoticeOut(BaseModel):
    """A notice to managers that the disk is (soon) above the target (services/storage.py)."""

    at: UTCDateTime
    reason: str  # over | soon | emergency
    percent: float
    rate: float | None = None  # bytes per hour
    hours_target: float | None = None
    hours_full: float | None = None
    projects: list[str] = Field(default_factory=list)  # whose managers were told, the cause first


class StorageOut(BaseModel):
    """The agents' volume against its target: trend and forecast, and the last notices."""

    notify_percent: float
    emergency_percent: float
    percent: float | None = None
    rate: float | None = None  # bytes per hour; None: not known yet
    hours_target: float | None = None  # until the target is crossed (0: it is); None: not growing
    hours_95: float | None = None
    hours_full: float | None = None
    notices: list[StorageNoticeOut] = Field(default_factory=list)


class MachineOut(BaseModel):
    """What one runner's machine uses: disk, memory and CPU, with the projects that run on it."""

    runner_ok: bool
    measured_at: UTCDateTime | None = None  # when the folders were last measured
    measuring: bool = False
    volumes: list[VolumeOut] = Field(default_factory=list)
    projects: list[ProjectUsageOut] = Field(default_factory=list)
    shared: list[FolderOut] = Field(default_factory=list)  # parts of the agents' home: caches, toolchains
    other_disk: int = 0  # folders in the workspaces of no project
    memory_total: int = 0
    memory_used: int = 0
    memory_other: int = 0  # processes of the runner that work in no agent's folder: the runner itself, browsers
    cpu_cores: int = 0
    cpu_step: int = 300  # seconds per slot of the histories
    cpu_start: UTCDateTime | None = None  # the first slot of the histories (24 hours, oldest first)
    cpu_load: list[float] = Field(default_factory=list)  # host load average over 1, 5, 15 minutes
    cpu_load_history: list[float] = Field(default_factory=list)  # mean 1-minute load per slot
    cpu_other: float = 0  # cores used by what works in no project's folder: the runner itself, browsers, the system
    cpu_other_avg: list[float] = Field(default_factory=list)
    cpu_other_history: list[float] = Field(default_factory=list)


class RunnerUsageOut(MachineOut):
    """An external runner: connected, its numbers are current; away, they are the last known ones (if any)."""

    id: int
    name: str
    connected: bool
    last_seen_at: UTCDateTime | None = None  # when it was last connected
    known_at: UTCDateTime | None = None  # when the numbers are from; None: nothing known


class SearchModelState(BaseModel):
    name: str
    label: str
    state: str  # absent | downloading | loading | ready | failed
    progress: float | None = None  # while downloading: the share done
    error: str = ""


class SearchModelOption(BaseModel):
    """One of the models the settings offer (services/search/embedding.MODELS)."""

    name: str
    label: str
    tier: str  # small (lean) | balanced | large (best quality)
    description: str
    dim: int
    max_tokens: int
    licence: str
    download_mb: int
    ram_mb: int
    mmteb: float | None = None
    mmteb_retrieval: float | None = None
    recommended: bool = False
    estimate_seconds: int | None = None  # a full index of what is indexed now, with it, on this server


class SearchKindOut(BaseModel):
    items: int = 0  # indexed
    total: int | None = None  # in the source (projects with search on); None: not known (commits)
    chunks: int = 0
    embedded: int = 0  # chunks with a vector of the current model


class SearchErrorOut(BaseModel):
    at: UTCDateTime
    message: str


class SearchStatusOut(BaseModel):
    available: bool  # PostgreSQL with pgvector
    enabled: bool  # the setting
    model: SearchModelState  # what search uses
    next: SearchModelState | None = None  # the model the index is being made anew for
    documents: int = 0  # chunks in the index
    embedded: int = 0  # of them with a vector of the current model; the rest waits (backfill, changes)
    next_embedded: int = 0  # of them with a vector of the next model
    by_kind: dict[str, SearchKindOut] = Field(default_factory=dict)
    rate: float | None = None  # chunks embedded per second here, lately
    eta_seconds: int | None = None  # until every chunk has its vector (of the next model, while switching)
    errors: list[SearchErrorOut] = Field(default_factory=list)  # the indexer's last failures
    models: list[SearchModelOption] = Field(default_factory=list)


class SearchHitOut(BaseModel):
    kind: str  # ticket, report, decision, comment, note, commit, idea
    ref: str
    title: str
    snippet: str  # the part of its text around the words searched for
    at: UTCDateTime
    changed_at: UTCDateTime
    ticket_number: int | None = None
    task_id: int | None = None  # the ticket it belongs to, to open it
    note_id: int | None = None
    idea_id: int | None = None
    doc_page_id: int | None = None  # a doc page (#118): open it in its site
    repo: str | None = None
    sha: str | None = None  # a commit's whole hash
    url: str | None = None  # a commit's page, where its repository has one


class ProjectSearchOut(BaseModel):
    # ok: by meaning and words; preparing: the model is not ready, by words only; off: the project's or the
    # installation's switch is off; unavailable: no PostgreSQL with pgvector here
    state: Literal["ok", "preparing", "off", "unavailable"]
    hits: list[SearchHitOut] = Field(default_factory=list)


class SearchModelIn(BaseModel):
    model: str = Field(min_length=1, max_length=100)


class UsageOut(MachineOut):
    """The built-in runner's machine (the fields of ``MachineOut``) and the app's, and the external runners'."""

    data: int | None = None  # the database and the key, in the app's data folder
    runners: list[RunnerUsageOut] = Field(default_factory=list)  # external runners that projects use, by name
    storage: StorageOut | None = None
    cleanups: list[CleanupOut] = Field(default_factory=list)  # newest first
    servers_stopped: CleanupOut | None = None  # by the memory cleanup, the last time


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
    # Of a person's own subscription: who else may use it (services/permissions.may_use).
    share: Literal["private", "people", "everyone"] | None = None
    shared_with: list[int] | None = Field(default=None, max_length=1000)


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
    owner_id: int | None = None  # None: the installation's; else a person's own
    owner_name: str = ""
    share: str = "private"
    shared_with: list[int] | None = None  # to its managers only
    can_manage: bool = False  # the viewer may change, test, share and delete it
    home_login: bool = False  # the installation's sign-in is still in the built-in runner's home (accounts.take_over)
    project_ids: list[int] | None = None  # the projects its owner gave it to (#93); to its managers only


class ProjectAccountOut(BaseModel):
    """A person's own subscription in a project's list of what members gave it (#93). Never a secret."""

    id: int
    provider: str
    label: str
    enabled: bool
    owner_id: int | None
    owner_name: str = ""
    mine: bool = False  # the viewer's own: they give it or take it back
    given: bool = False  # its owner gave it to this project
    blocked: bool = False  # the project's owner switched it off for the project
    used: bool = False  # the project's runs may use it now (also one shared with its owner)


class ProjectAccountGiveIn(BaseModel):
    given: bool


class ProjectAccountBlockIn(BaseModel):
    blocked: bool


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
    tidy_branches: bool = True
    check_target_seconds: int = 0
    checks_baseline_seconds: int | None = None
    checks_verdict: str = ""
    agent_ids: list[int] = Field(default_factory=list)
    auth: RepoAuth = "default"
    token_user: str = ""
    has_token: bool = False
    ssh_public_key: str = ""  # to add at the git host, e.g. as a deploy key
    red_base: str | None = None  # the base branch, while its checks fail
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
    runner_id: int | None = None  # where it starts (None: the built-in runner); not sent: the owner's first allowed one
    repositories: list[RepositoryCreate] = Field(default_factory=list)
    first_steps: bool = False  # start with the two tickets that set a new project up (services/projects.FIRST_STEPS)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    language: str | None = Field(default=None, max_length=50)  # "" : the language each person writes in
    customer_languages: list[LanguageCode] | None = Field(default=None, min_length=1, max_length=30)  # the first is the default
    workspace_path: WorkspacePath = None
    setup_script: str | None = None
    github_token: str | None = None  # "" clears the override
    auto_wake_manager: bool | None = None
    continue_with_backlog: bool | None = None
    fast_checks: bool | None = None
    semantic_search: bool | None = None
    parallel_agents: int | None = Field(default=None, ge=0, le=20)  # null: the number from the settings again
    account_id: int | None = None  # null: the subscription with the most room again
    auto_archive_days: int | None = Field(default=None, ge=0, le=3650)
    review_days: int | None = Field(default=None, ge=0, le=365)
    attachment_retention_days: int | None = Field(default=None, ge=0, le=3650)  # files of done tickets; 0: never deleted; null: the PlainBoard default
    attachment_retention_min_mb: int | None = Field(default=None, ge=0, le=100000)  # only files from this size; 0: all; null: the default
    ask_provider: str | None = Field(default=None, max_length=50)  # questions: "" the default again
    ask_model: str | None = Field(default=None, max_length=100)
    ask_effort: str | None = Field(default=None, max_length=20)
    idea_provider: str | None = Field(default=None, max_length=50)  # sorting ideas: "" the same as questions again
    idea_model: str | None = Field(default=None, max_length=100)
    idea_effort: str | None = Field(default=None, max_length=20)


class ProjectAccountsIn(BaseModel):
    account_ids: list[int] = Field(max_length=200)  # the only accounts the project may use; empty: any


class ProjectOut(ORM):
    id: int
    name: str
    slug: str
    description: str
    language: str = ""
    customer_languages: list[str] = Field(default_factory=lambda: ["en"])  # the first is the default
    paused_at: UTCDateTime | None = None  # paused for good by a person, since then
    paused_by: str = ""
    workspace_path: str | None
    has_github_token: bool = False
    auto_wake_manager: bool
    continue_with_backlog: bool = True
    fast_checks: bool = True
    semantic_search: bool = True
    parallel_agents: int | None = None
    account_id: int | None = None
    account_ids: list[int] = []  # the only accounts it may use, set by an admin; empty: any
    runner_id: int | None = None  # its external runner; None: the built-in one
    role: ProjectRole | None = None  # the asking user's role; not part of events, which go to everyone
    auto_archive_days: int
    review_days: int = 3
    attachment_retention_days: int | None = None  # None: follows the PlainBoard default (settings)
    attachment_retention_min_mb: int | None = None
    ask_provider: str = ""
    ask_model: str = ""
    ask_effort: str = ""
    idea_provider: str = ""
    idea_model: str = ""
    idea_effort: str = ""
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
    doc_site_id: int | None = None  # the doc agent of this doc site: hidden from team and board, set up in the site's settings
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
LanguageCode = Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")]


class CustomerTextIn(BaseModel):
    title: str | None = Field(default=None, max_length=CUSTOMER_TITLE_MAX)
    description: str | None = Field(default=None, max_length=CUSTOMER_DESCRIPTION_MAX)


class CustomerText(BaseModel):
    title: str = ""
    description: str = ""


LabelColor = Literal["gray", "red", "orange", "amber", "green", "teal", "blue", "indigo", "purple", "pink"]


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    customer_title: str = Field(default="", max_length=CUSTOMER_TITLE_MAX)
    customer_description: str = Field(default="", max_length=CUSTOMER_DESCRIPTION_MAX)
    customer_texts: dict[LanguageCode, CustomerTextIn] = Field(default_factory=dict)  # per customer language; the default's = the two above
    status: Status = "backlog"
    priority: Priority = "medium"
    labels: list[str] = Field(default_factory=list)


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    customer_title: str | None = Field(default=None, max_length=CUSTOMER_TITLE_MAX)
    customer_description: str | None = Field(default=None, max_length=CUSTOMER_DESCRIPTION_MAX)
    customer_texts: dict[LanguageCode, CustomerTextIn] | None = None  # merged per language and field
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
    customer_texts: dict[str, CustomerText] = Field(default_factory=dict)  # the other customer languages
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
    # Work is finished and only waits to land (no open assignment): its branch waits for a train, is checked, or is repaired.
    landing: Literal["waiting", "checking", "repairing", "held"] | None = None


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
    lang: str  # the language of title and description: the one asked for, else the default
    title: str
    description: str
    status: str
    updated_at: UTCDateTime


class PublicBoard(BaseModel):
    project: str
    languages: list[str]  # the customer languages, the default first
    default_language: str
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


class PersonalTokenIn(BaseModel):
    label: str = Field(default="", max_length=200)
    read_only: bool = False
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)  # None: until revoked


class PersonalTokenOut(ORM):
    id: int
    label: str
    hint: str  # last characters of the token
    read_only: bool
    expires_at: UTCDateTime | None
    created_at: UTCDateTime
    last_used_at: UTCDateTime | None
    token: str | None = None  # only in the answer that creates the token


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
    idea_id: int | None = None
    doc_site_id: int | None = None
    filename: str
    content_type: str
    size: int
    uploaded_by_agent_id: int | None
    created_at: UTCDateTime
    purged_at: UTCDateTime | None = None  # its file was deleted by the project's retention; the entry stays
    purged_after_days: int | None = None


class RetentionCount(BaseModel):
    count: int
    bytes: int


class RetentionPreviewOut(BaseModel):
    """What a retention setting would delete: at once, and within the next ``soon_days``."""

    now: RetentionCount
    soon: RetentionCount
    soon_days: int
    projects: int | None = None  # the PlainBoard default: how many projects follow it


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
    scratchpad: str | None = None


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


class ChatPartOut(BaseModel):
    """One message of a chat run: several wait for the same turn when people write before it starts."""

    id: int
    author: str | None = None
    text: str = ""
    attachment_ids: list[int] = Field(default_factory=list)
    at: UTCDateTime


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
    parts: list[ChatPartOut] | None = None  # a chat run: its messages, each shown on its own
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


class SkillOut(ORM):
    id: int
    name: str
    description: str
    enabled: bool
    all_agents: bool
    agent_ids: list[int] = Field(default_factory=list)
    source: str
    source_url: str
    source_ref: str
    source_path: str
    content_hash: str
    size: int
    file_count: int
    updated_at: UTCDateTime


class SkillImport(BaseModel):
    url: str = Field(max_length=500)
    ref: str = Field(default="", max_length=200)
    path: str = Field(default="", max_length=300)
    all_agents: bool = True
    agent_ids: list[int] = Field(default_factory=list)


class SkillUpdate(BaseModel):
    enabled: bool | None = None
    all_agents: bool | None = None
    agent_ids: list[int] | None = None


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


# --- runners --------------------------------------------------------------------------------


class RunnerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class RunnerOut(BaseModel):
    """An external runner, as Settings > Runners lists it. Disk and memory only while it is connected."""

    id: int
    name: str
    connected: bool
    version: str = ""  # the commit it runs, short; empty: not known
    version_ok: bool = True  # the same as the app's (or one of them is not known)
    updating: bool = False  # it runs idle to update itself to the app's version, or restarts for it
    restart_by: UTCDateTime | None = None  # while updating: it restarts at the latest then, whatever still runs
    last_seen_at: UTCDateTime | None = None
    created_at: UTCDateTime
    projects: list[str] = []
    disk_total: int = 0
    disk_used: int = 0
    memory_total: int = 0
    memory_used: int = 0


class RunnerCreated(BaseModel):
    """Shown once: the token is stored only as a hash."""

    runner: RunnerOut
    token: str
    command: str  # docker run ...
    compose: str  # the same as a docker-compose.yml


class RunnerChoice(BaseModel):
    id: int | None  # None: the built-in runner
    name: str
    connected: bool


class ProjectRunnerOut(BaseModel):
    runner_id: int | None
    runners: list[RunnerChoice]
    busy: str = ""  # why it can't be switched right now; empty: it can


class UnpushedItem(BaseModel):
    agents: list[str]  # whose workspace it is (several: a project folder they share)
    repo: str  # the clone, relative to the workspace
    problems: list[str]


class UnpushedOut(BaseModel):
    """Work in the project's workspaces on its current runner that is on no remote: it stays behind on a move."""

    runner: str  # the current runner's name
    checked: bool  # False: the runner could not be asked (``detail`` says why)
    detail: str = ""
    items: list[UnpushedItem] = []


class ProjectRunnerIn(BaseModel):
    runner_id: int | None


# --- events ---------------------------------------------------------------------------------


class Event(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


# --- flow: what works where, is checked and lands (services/flow.py) ----------------------------


class FlowRepo(BaseModel):
    id: int
    name: str
    base: str  # the base branch
    land_first: bool  # branches land before their checks (a development branch)
    typical_check_seconds: int | None  # the median of the last check runs that landed; None until there is one
    check_target_seconds: int
    red: bool  # the base branch fails its checks


class FlowJob(BaseModel):
    id: int
    command: str
    started_at: UTCDateTime


class FlowWork(BaseModel):
    """What an agent is busy with right now: a turn (or one waiting to start) and the jobs it left running."""

    agent_id: int
    status: Literal["running", "queued", "background"]
    run_id: int | None = None
    trigger: str | None = None
    since: UTCDateTime | None = None
    task_id: int | None = None
    task_number: int | None = None
    title: str = ""  # the work order, if the turn is one
    repair: bool = False  # PlainBoard ordered it: a branch or train that failed its checks
    branch: str | None = None  # the branch it repairs, if one
    jobs: list[FlowJob] = Field(default_factory=list)


class FlowBranch(BaseModel):
    id: int
    repository_id: int
    branch: str
    title: str
    agent_id: int | None
    task_id: int | None
    task_number: int | None
    status: Literal["waiting", "checking", "repairing", "merged", "stuck", "closed"]
    train_id: int | None
    failures: int  # times it went back to its author
    since: UTCDateTime  # when it entered this state
    merged_at: UTCDateTime | None
    conflict_files: list[str] = Field(default_factory=list)
    held: Literal["message", "manager"] | None = None  # it does not land until its author hands it over again / the manager releases it
    held_reason: str = ""


class FlowTrain(BaseModel):
    id: int
    repository_id: int
    status: Literal["checking", "blocked", "landed", "failed", "aborted"]
    direct: bool  # its branches landed first, the checks look at the base branch afterwards
    size: int  # branches on it; 0: a look at the base branch
    started_at: UTCDateTime
    finished_at: UTCDateTime | None
    check_seconds: int
    failed_checks: list[str] = Field(default_factory=list)
    repaired_group: bool = False  # branches that failed together, checked again as a group
    escalated: bool = False
    branches: list[str] = Field(default_factory=list)


class FlowLive(BaseModel):
    now: UTCDateTime
    repositories: list[FlowRepo]
    work: list[FlowWork]
    branches: list[FlowBranch]  # on their way (waiting, checking, repairing, stuck) and the last ones that landed
    trains: list[FlowTrain]  # being checked now (or blocked)


class FlowRepair(BaseModel):
    agent_id: int | None
    assignment_id: int | None
    at: UTCDateTime


class FlowHistoryItem(BaseModel):
    id: str
    kind: Literal["train", "sweep", "stuck"]
    at: UTCDateTime
    repository_id: int | None
    train: FlowTrain | None = None
    repairs: list[FlowRepair] = Field(default_factory=list)  # who was ordered to repair what failed
    detail: str = ""  # a sweep: the branches removed; stuck: why


class FlowHistory(BaseModel):
    items: list[FlowHistoryItem]
    next_before: UTCDateTime | None  # ask for the next page with this; None: that was all


class FlowStep(BaseModel):
    key: Literal["handed", "failed", "repair", "checking", "landed", "stuck"]
    state: Literal["done", "now", "todo"]
    at: UTCDateTime | None = None
    detail: str = ""


class FlowTicketBranch(BaseModel):
    branch: FlowBranch
    repository: str
    train: FlowTrain | None
    steps: list[FlowStep]


# --- ideas (services/ideas.py, orchestration/ideas.py) -----------------------------------------

IDEA_TEXT_MAX = 10000


class IdeaCreate(BaseModel):
    text: str = Field(min_length=1, max_length=IDEA_TEXT_MAX)
    title: str = Field(default="", max_length=200)
    topic_id: int | None = None  # placed by a person: the model leaves its topic alone
    x: float | None = None
    y: float | None = None


class IdeaInput(BaseModel):
    """What a person typed into the field of the board of ideas: the model reads what they mean."""

    text: str = Field(min_length=1, max_length=IDEA_TEXT_MAX)


class IdeaUpdate(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=IDEA_TEXT_MAX)
    title: str | None = Field(default=None, max_length=200)
    topic_id: int | None = None  # sent as null: out of its topic (unsorted)
    x: float | None = None
    y: float | None = None
    votes: int | None = Field(default=None, ge=0, le=1000000)


class IdeaOption(BaseModel):
    label: str
    description: str = ""
    resolve: str = ""  # an option of a conflict question, done without the model: keep:<kept>:<left out> | both:<a>:<b>


class IdeaOut(ORM):
    id: int
    project_id: int
    title: str
    text: str
    author: str
    status: str  # open | taken | dismissed | merged
    topic_id: int | None
    topic_manual: bool
    task_id: int | None
    task_number: int | None = None
    merged_into_id: int | None
    hint: str
    sort_state: str  # pending | sorted | failed
    x: float | None
    y: float | None
    votes: int
    similar: list[int] = Field(default_factory=list)  # open ideas kept apart from it on purpose (not merged although close)
    duplicate_of: int | None = None  # to be merged into this one
    conflicts: list[int] = Field(default_factory=list)  # open ideas it contradicts, until the person decides
    question: str  # the model's question to the person; "" none
    question_options: list[IdeaOption]  # the first is recommended; a free answer is possible too
    question_status: str  # "" | open | answered | withdrawn
    question_answer: str
    question_at: UTCDateTime | None
    closed_at: UTCDateTime | None
    closed_reason: str
    attachments: list[AttachmentOut] = Field(default_factory=list)
    created_at: UTCDateTime
    updated_at: UTCDateTime


class IdeaVersionOut(ORM):
    id: int
    idea_id: int
    title: str
    text: str
    source: str  # who replaced it: user | ai | undo
    created_at: UTCDateTime


class IdeaAnswer(BaseModel):
    option_index: int | None = Field(default=None, ge=0)
    text: str = Field(default="", max_length=IDEA_TEXT_MAX)


class IdeaTopicIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    color: int | None = Field(default=None, ge=0, le=9)
    x: float | None = None
    y: float | None = None


class IdeaTopicOut(ORM):
    id: int
    project_id: int
    name: str
    color: int
    hint: str
    manual: bool
    x: float | None
    y: float | None


class IdeaChangeOut(ORM):
    """One step on the board of ideas, to show ("added to 'Booking'") and undo (POST .../changes/{id}/undo)."""

    id: int
    project_id: int
    kind: str  # new | several | amend | merge | split | resolve | question | edit | reorder
    source: str  # input | answer | user | auto | button
    author: str
    summary: dict[str, Any]
    created_at: UTCDateTime
    undone_at: UTCDateTime | None
    ideas: list[IdeaOut] = Field(default_factory=list)  # the ideas it touched, as they are now (with an answer only)


class IdeasOut(BaseModel):
    topics: list[IdeaTopicOut]
    ideas: list[IdeaOut]
    sorting: bool = False  # the model is sorting ideas right now


class IdeaTicketIn(BaseModel):
    """One idea straight into a ticket; what is not sent comes from the idea."""

    status: Literal["backlog", "todo"] = "backlog"
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    priority: Priority = "medium"
    labels: list[str] = Field(default_factory=list)


class IdeaTicketOut(BaseModel):
    ticket: TaskOut
    idea: IdeaOut


# --- doc sites (#118, services/docs.py) -----------------------------------------------------------------

DocSlug = Annotated[str, Field(max_length=80, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]


Color = Annotated[str, Field(max_length=9, pattern=r"^(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8})?$")]


class DocThemeColors(BaseModel):
    """One mode (light or dark) of a site's theme; "" keeps the site's own colour."""

    accent: Color = ""  # links, the marked page, the logo mark
    page: Color = ""  # the background
    surface: Color = ""  # cards, code, menus
    text: Color = ""  # (lighter shades for secondary text and lines follow from it)


class DocThemeLink(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=1, max_length=500)  # http(s), mailto: or an address on this host (/...)


class DocTheme(BaseModel):
    """A doc site's own look (#121, services/docs_theme.py). Every field at its default: the site as every site looks."""

    logo_light: int | None = None  # a file of the site (an image)
    logo_dark: int | None = None  # in dark mode; none: logo_light
    favicon: int | None = None
    show_name: bool = True  # the site's name beside the logo
    light: DocThemeColors = Field(default_factory=DocThemeColors)
    dark: DocThemeColors = Field(default_factory=DocThemeColors)
    font_family: str = Field(default="", max_length=200)  # a CSS font-family list; "": the site's own (Inter)
    font_url: str = Field(default="", max_length=500)  # https: a web font's stylesheet (e.g. Google Fonts)
    radius: int | None = Field(default=None, ge=0, le=28)  # px of a card's corner, the rest in proportion; None: 14
    links: list[DocThemeLink] = Field(default_factory=list, max_length=6)  # in the header (back to the website)
    css: str = Field(default="", max_length=20000)  # applied on the public site only, after everything else


class DocThemePreviewOut(BaseModel):
    css: str


DocPageSlug = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]


class DocCustomText(BaseModel):
    title: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=200000)


class DocCustomPageCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    slug: DocPageSlug | None = None  # None: from the title
    body: str = Field(default="", max_length=200000)
    texts: dict[str, DocCustomText] = Field(default_factory=dict)  # the site's further languages


class DocCustomPageUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    slug: DocPageSlug | None = None
    body: str | None = Field(default=None, max_length=200000)
    texts: dict[str, DocCustomText] | None = None  # replaces all of them
    position: int | None = Field(default=None, ge=0)


class DocCustomPageOut(BaseModel):
    id: int
    site_id: int
    position: int
    title: str
    slug: str
    body: str
    texts: dict[str, DocCustomText]
    path: str  # under the site: p/<slug>
    edited_by: str
    updated_at: UTCDateTime


class DocSiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: DocSlug | None = None  # None: from the name
    description: str = Field(default="", max_length=300)
    spec: str = Field(default="", max_length=20000)
    published: bool = False
    public: bool = False  # published and public: everyone; published and private: people with a role in the project
    language: str = Field(default="en", max_length=20)
    languages: list[str] = Field(default_factory=list, max_length=30)
    agent_provider: str | None = Field(default=None, max_length=50)  # its doc agent; None: the installation's defaults
    agent_model: str | None = Field(default=None, max_length=100)
    agent_effort: str | None = Field(default=None, max_length=20)
    versioned: bool = False  # versions (#126); off: always the current state


Translator = Literal["", "llm", "deepl", "google"]


class DocSiteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    slug: DocSlug | None = None
    description: str | None = Field(default=None, max_length=300)
    spec: str | None = Field(default=None, max_length=20000)
    versioned: bool | None = None  # versions (#126): off again, they stay but are not served
    published: bool | None = None
    public: bool | None = None
    languages: list[str] | None = Field(default=None, max_length=30)  # further languages, translated
    translator: Translator | None = None
    translate_provider: str | None = Field(default=None, max_length=50)  # llm: "" = the project's default provider
    translate_model: str | None = Field(default=None, max_length=100)  # llm: "" = the provider's small model
    agent_provider: str | None = Field(default=None, max_length=50)  # its doc agent
    agent_model: str | None = Field(default=None, max_length=100)
    agent_effort: str | None = Field(default=None, max_length=20)  # "": the installation's default
    # Its assistant for visitors (#119): provider "" = Claude (the only one so far), model "" = its small one.
    assistant_enabled: bool | None = None
    assistant_provider: str | None = Field(default=None, max_length=50)
    assistant_model: str | None = Field(default=None, max_length=100)
    assistant_effort: str | None = Field(default=None, max_length=20)
    assistant_code: bool | None = None  # may read the code and the board's public texts: see assistant_warning
    assistant_daily_messages: int | None = Field(default=None, ge=1, le=100000)
    assistant_daily_cost: float | None = Field(default=None, ge=0, le=100000)  # USD per day; 0: no cap
    assistant_max_tokens: int | None = Field(default=None, ge=128, le=8192)
    theme: DocTheme | None = None  # its look (#121); DocTheme() as it is: none


class DocKeysIn(BaseModel):
    """Write-only; "" removes a key, None keeps it."""

    deepl: str | None = Field(default=None, max_length=200)
    google: str | None = Field(default=None, max_length=200)


class DocTranslationItem(BaseModel):
    lang: str
    title: str
    state: Literal["current", "outdated", "missing", "failed"]
    translated_by: str = ""
    translated_at: UTCDateTime | None = None
    error: str = ""


class DocTranslationOut(DocTranslationItem):
    body: str


class DocTranslationIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=200000)


class DocDomainIn(BaseModel):
    domain: str = Field(max_length=253)  # "" removes it


class DocDomainOut(BaseModel):
    """What the user sets up for the site's own domain (the UI words it): a CNAME, or a public hostname of a
    Cloudflare tunnel, from ``domain`` to ``target``; PlainBoard then reads ``verify_url`` and expects ``token``."""

    domain: str
    target: str  # the host this PlainBoard is reached at (where the request came to)
    verify_url: str
    token: str
    verified_at: UTCDateTime | None = None
    cloudflare_token_set: bool = False


class DocAgentOut(BaseModel):
    """The hidden agent of a doc site (#118): configured in the site's settings."""

    id: int
    slug: str
    name: str
    provider: str
    model: str
    effort: str | None = None
    enabled: bool = True


class DocReleaseOut(BaseModel):
    """A version of a doc site (#126, services/docs_releases.py)."""

    id: int
    site_id: int
    name: str  # e.g. 1.0; the working one "next" until renamed
    slug: str  # its address: /docs/<site>/<slug>/
    working: bool  # the version written now (one per site)
    seq: int  # the order of cutting (the working one: the next cut's)
    notes: str = ""
    released_on: UTCDateTime | None = None
    deprecated: bool = False
    hidden: bool = False  # not served (the working one: not public)
    is_default: bool = False  # chosen as the visitors' default
    default: bool = False  # what visitors get at /docs/<site>/ now (chosen, else the latest released one)
    url: str
    created_at: UTCDateTime
    updated_at: UTCDateTime


class DocReleaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slug: str | None = Field(default=None, max_length=40, pattern=r"^[a-z0-9][a-z0-9._-]*$")  # default: from the name
    notes: str = Field(default="", max_length=20000)
    released_on: datetime | None = None  # default: now


class DocReleaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    slug: str | None = Field(default=None, max_length=40, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    notes: str | None = Field(default=None, max_length=20000)
    released_on: datetime | None = None
    deprecated: bool | None = None
    hidden: bool | None = None
    default: bool | None = None  # true: the visitors' default; false: back to the latest released one


class DocSiteOut(ORM):
    id: int
    project_id: int
    name: str
    slug: str
    description: str
    spec: str
    published: bool
    public: bool = False
    language: str = "en"
    languages: list[str] = []
    translator: str = ""
    translate_provider: str = ""
    translate_model: str = ""
    deepl_key_set: bool = False
    google_key_set: bool = False
    domain: str | None = None
    domain_verified_at: UTCDateTime | None = None
    cloudflare_token_set: bool = False
    assistant_enabled: bool = False
    assistant_provider: str = ""
    assistant_model: str = ""
    assistant_effort: str = ""
    assistant_code: bool = False
    assistant_daily_messages: int = 300
    assistant_daily_cost: float = 0.0
    assistant_max_tokens: int = 1024
    assistant_warning: str = ""  # what reading the code and the board risks: shown beside assistant_code
    theme: DocTheme = Field(default_factory=DocTheme)
    agent: DocAgentOut | None = None  # its doc agent, the only agent that writes it
    versioned: bool = False  # uses versions (#126): off, it always shows the current state and visitors see no versions
    releases: list[DocReleaseOut] = []  # its versions (#126): the working one first, then the released ones, newest first
    url: str  # its address under this PlainBoard: /docs/<slug>/
    pages: int = 0
    stale_pages: int = 0  # flagged as possibly outdated
    created_at: UTCDateTime
    updated_at: UTCDateTime


class DocPageItem(ORM):
    """A page in the site's tree (without its text)."""

    id: int  # the page in every version of its site (#126)
    site_id: int
    release_id: int | None = None  # the version it was read or changed in; None: the working version
    parent_id: int | None
    position: int
    title: str
    slug: str
    path: str  # under the site: parent-slug/slug (the home page is the first page at the top, also at "")
    depth: int = 0
    children: int = 0  # pages directly below it
    paths: list[str]
    edited_by: str
    edited_at: UTCDateTime
    drift: int
    flagged_at: UTCDateTime | None = None
    flag_reason: str = ""
    updated_at: UTCDateTime


class DocCrumb(BaseModel):
    id: int
    title: str
    path: str


class DocPageOut(DocPageItem):
    body: str
    versions: int = 0
    ancestors: list[DocCrumb] = []  # from the top down: what to open in the tree
    translations: list[DocTranslationItem] = []


class DocPagesOut(BaseModel):
    items: list[DocPageItem]
    total: int  # children of that parent
    offset: int


class DocPageCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=200000)
    parent_id: int | None = None
    position: int | None = Field(default=None, ge=0)  # None: last
    slug: DocSlug | None = None
    paths: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=50)
    reason: str = Field(default="", max_length=300)


class DocPageUpdate(BaseModel):
    """Only what is given changes. ``parent_id`` with ``move``: another place in the tree (None: the top)."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=200000)
    move: bool = False
    parent_id: int | None = None
    position: int | None = Field(default=None, ge=0)
    slug: DocSlug | None = None
    paths: list[Annotated[str, Field(min_length=1, max_length=300)]] | None = Field(default=None, max_length=50)
    reason: str = Field(default="", max_length=300)


class DocVersionOut(ORM):
    id: int
    page_id: int
    title: str
    body: str | None = None  # in the list: None
    author: str
    agent_id: int | None
    reason: str
    created_at: UTCDateTime


class DocCloudflareIn(BaseModel):
    token: str | None = Field(default=None, max_length=200)  # stored encrypted for this site; None: the one stored
    target: str | None = Field(default=None, max_length=253)  # None: the host this request came to
    proxied: bool = True


class DocDnsOut(BaseModel):
    zone: str
    record: str
    target: str
    created: bool  # False: an existing record was changed


class DocVerifyOut(BaseModel):
    verified: bool
    detail: str = ""
    verified_at: UTCDateTime | None = None
