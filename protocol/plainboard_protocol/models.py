from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

# "full": all built-in coding tools (shell, edit, web ...) + PlainBoard tools.
# "board": PlainBoard tools + shell and file editing. Much smaller prompt -> fewer tokens.
Toolset = Literal["full", "board"]

# A provider account's key on the runner. The default account keeps the sign-in from before accounts existed.
DEFAULT_ACCOUNT = "default"
ACCOUNT_KEY_PATTERN = r"^[A-Za-z0-9_-]{1,40}$"


class ModelInfo(BaseModel):
    id: str
    label: str
    tier: Literal["small", "medium", "large", "xlarge"] = "medium"


class EndpointInfo(BaseModel):
    """A provider that is an address anyone can run or rent: a local model server, a model host, a gateway.
    Its accounts hold the address, the key and the models they offer."""

    url: str = ""  # the usual address; empty: the user names it
    key: bool = True  # whether it wants an API key
    hint: str = ""  # one line for the settings: what it is, where the key comes from


class ProviderInfo(BaseModel):
    name: str
    label: str
    models: list[ModelInfo]
    efforts: list[str] = Field(default_factory=list)
    supports_resume: bool = True
    endpoint: EndpointInfo | None = None


class Credentials(BaseModel):
    """Secrets forwarded per run. Never persisted by the runner."""

    claude_auth_mode: Literal["oauth_token", "api_key", "runner_login"] = "oauth_token"
    claude_oauth_token: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    # "chatgpt": the ChatGPT subscription signed in on the runner; "api_key": openai_api_key.
    codex_auth_mode: Literal["chatgpt", "api_key"] = "chatgpt"
    # Which of the runner's Codex sign-ins to use: every account has its own Codex home.
    codex_account: str = Field(default=DEFAULT_ACCOUNT, pattern=ACCOUNT_KEY_PATTERN)
    # A provider that is an endpoint (ProviderInfo.endpoint): the account's address, empty for the usual one, and its key.
    endpoint_url: str = ""
    endpoint_key: str | None = None
    endpoint_context: int = 0  # tokens its models take at once; 0: the runner's cautious guess
    github_token: str | None = None
    git_ssh_key: str | None = None  # private key for repositories with an SSH address and no key of their own
    git_author_name: str = "PlainBoard Agent"
    git_author_email: str = "agent@plainboard.local"
    secrets: dict[str, str] = Field(default_factory=dict)  # user-defined environment variables


class RepoSpec(BaseModel):
    name: str  # folder name inside the workspace
    url: str
    branch: str | None = None
    # The repository's own access, instead of the general GitHub token (at most one of them):
    token: str | None = None  # HTTPS access token of any git host
    token_user: str | None = None  # user name that goes with the token, if the host needs one
    ssh_key: str | None = None  # private key, e.g. of a deploy key


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


class ProviderAccount(BaseModel):
    """A subscription signed in on the runner, and a sign-in in progress, if any."""

    signed_in: bool = False
    detail: str = ""  # e.g. the account's email and plan
    login_url: str | None = None  # device sign-in: open this ...
    login_code: str | None = None  # ... and enter this code
    login_error: str | None = None


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


class VolumeUsage(BaseModel):
    """One file system: what the paths on it share."""

    paths: list[str]
    fsid: int = 0  # the same file system seen from two containers has the same id
    total: int
    used: int
    free: int  # what a process that is not root can still write


FolderKind = Literal["workspace", "shared", "train"]


class FolderUsage(BaseModel):
    """Disk use of one folder: an agent's workspace, a part of the shared home, a train's clone."""

    path: str
    kind: FolderKind
    bytes: int


class ProcessMemory(BaseModel):
    """Memory of the processes working in one folder (by their own working directory or a parent's)."""

    folder: str | None  # a workspace folder, at most two levels below the root, or a train's clone. None: the rest
    bytes: int
    processes: int


class MachineUsage(BaseModel):
    """Disk and memory of the runner's machine. Folders are measured in the background, rarely."""

    volumes: list[VolumeUsage] = Field(default_factory=list)
    folders: list[FolderUsage] = Field(default_factory=list)
    measured_at: float | None = None  # unix time the folders were measured; None: not yet
    measuring: bool = False
    memory_total: int = 0
    memory_used: int = 0  # of the machine: total minus what is available
    memory: list[ProcessMemory] = Field(default_factory=list)


CleanupLevel = Literal["soft", "hard"]


class CleanupFolder(BaseModel):
    """An agent's folder the app found safe to clean: its agent runs no turn and has no open assignment."""

    path: str  # two levels below the workspaces root
    idle_since: float = 0  # unix time of its agent's last turn; oldest first
    orphan: bool = False  # no agent has it any more (deleted or renamed): may go entirely if nothing in it is unpushed
    busy: bool = False  # its agent works (turn, open assignment, running job): only listed, deleted only when asked to


class CleanupSpec(BaseModel):
    """Free room on the workspaces' volume with what can be made again (see runner ``cleanup.py``)."""

    level: CleanupLevel  # soft: what costs nothing to rebuild; hard: also installed dependencies
    need: int  # bytes to free; cleaning stops once that much is gone
    folders: list[CleanupFolder] = Field(default_factory=list)


class CleanedItem(BaseModel):
    path: str
    what: Literal["build", "cache", "folder", "logs", "home"]
    bytes: int


class CleanupResult(BaseModel):
    freed: int = 0
    items: list[CleanedItem] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)  # folders left alone, with the reason


CandidateSafety = Literal["safe", "busy", "unsure", "keep"]


class CleanupCandidate(BaseModel):
    """Something the manager may delete to free room (runner ``cleanup.candidates``)."""

    path: str
    what: Literal["build", "cache", "folder", "home"]  # build output in an agent's folder, a cache, a removed agent's folder, a folder in the shared home
    bytes: int = 0
    safety: CandidateSafety  # safe: can be made again; busy: its agent works now; unsure: may hold work; keep: holds unpushed work or tools
    why: str = ""
    remotes: list[str] = Field(default_factory=list)  # origin URLs of the clones in it: whose a home folder is


class CandidatesSpec(BaseModel):
    folders: list[CleanupFolder] = Field(default_factory=list)  # every agent folder of the projects without a folder of their own


class DeleteSpec(BaseModel):
    paths: list[str]
    folders: list[CleanupFolder] = Field(default_factory=list)
    force: bool = False  # also busy and unsure candidates; never what holds unpushed work


class JobPort(BaseModel):
    """A port on which a running job answers as a web server."""

    port: int
    local: bool = False  # listens on 127.0.0.1 only: reachable from inside the runner, not through the app
    title: str = ""  # of the page it starts with
    base: bool = False  # it has /preview-base/<port>/ as its base path and answers only there


class PullRequestRef(BaseModel):
    """A pull request on GitHub, with the access to look at it."""

    credentials: Credentials
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")  # owner/name
    number: int = Field(ge=1)


class PullRequestCheck(BaseModel):
    name: str
    # cancelled: ended without a result (e.g. a newer push replaced the run); only for a commit's checks.
    state: Literal["pass", "fail", "pending", "cancelled"]
    link: str = ""


class PullRequestState(BaseModel):
    state: Literal["open", "merged", "closed"]
    mergeable: Literal["yes", "conflict", "unknown"] = "unknown"
    head_sha: str = ""
    branch: str = ""  # the branch the pull request comes from
    url: str = ""
    title: str = ""
    checks: list[PullRequestCheck] = Field(default_factory=list)


class CommitRef(BaseModel):
    """A commit on GitHub whose checks are wanted (``repo`` is owner/name)."""

    credentials: Credentials
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")


TRAIN_KEY = r"^[\w.-]{1,80}$"


class TidySpec(BaseModel):
    """Remove the branches of a repository whose commits are all on its base branch already."""

    credentials: Credentials
    key: str = Field(pattern=TRAIN_KEY)  # names the runner's clone for this repository
    remote: RepoSpec
    base: str | None = None  # None: the repository's default branch
    keep: list[str] = Field(default_factory=list)  # never these: in flight with PlainBoard, other base branches
    # owner/name on GitHub: a branch that open pull requests are based on stays (deleting it would close them).
    github: str | None = Field(default=None, pattern=r"^[\w.-]+/[\w.-]+$")
    min_age_hours: int = 24  # a branch whose last commit is younger stays
    limit: int = 300


class TidyResult(BaseModel):
    deleted: list[str] = Field(default_factory=list)


class ProjectFiles(BaseModel):
    """What a deleted project leaves on the runner."""

    workspace: str | None = None  # the folder its agents worked in. None: it stays (e.g. the user's own folder)
    trains: list[Annotated[str, Field(pattern=TRAIN_KEY)]] = Field(default_factory=list)  # the runner's own clones


class TrainSpec(BaseModel):
    """Several finished branches to be checked together: merged, in order, onto the base branch in
    the runner's own clone of the repository, so that one check run covers all of them. Plain git:
    works with any host."""

    credentials: Credentials
    key: str = Field(pattern=TRAIN_KEY)  # names the runner's clone for this repository
    remote: RepoSpec  # where to fetch from and push to, with the repository's own access if it has one
    base: str | None = None  # None: the repository's default branch
    branch: str  # the train's own branch
    members: list[str]  # branches, in the order they are merged
    # Leave a branch out while its head is still this commit: it failed like this before.
    skip_unchanged: dict[str, str] = Field(default_factory=dict)
    # Files in which an author resolves a conflict right now. A member that changes one of them is
    # left out: landing it would undo that work before it is back.
    busy_files: list[str] = Field(default_factory=list)
    # owner/name on GitHub: push the train's branch and open a pull request, whose checks check the
    # train. None: the train is checked by a command in the clone and never pushed as a branch.
    github: str | None = Field(default=None, pattern=r"^[\w.-]+/[\w.-]+$")
    title: str = ""
    body: str = ""


class TrainResult(BaseModel):
    base: str = ""
    head_sha: str = ""
    number: int | None = None  # the train's pull request, if it has one
    url: str = ""
    merged: dict[str, str] = Field(default_factory=dict)  # branch -> its head commit, now part of the train
    conflicts: list[str] = Field(default_factory=list)  # cannot be merged onto the base branch: its author resolves that
    # Merges onto the base branch, but not with a branch before it on this train. Nothing its author
    # could resolve yet: what it conflicts with is not on the base branch. It goes with the next train.
    waits: list[str] = Field(default_factory=list)
    held: list[str] = Field(default_factory=list)  # left out: see TrainSpec.busy_files
    conflict_files: dict[str, list[str]] = Field(default_factory=dict)  # branch -> the files it conflicts in
    missing: list[str] = Field(default_factory=list)  # not on the remote
    unchanged: list[str] = Field(default_factory=list)  # left out: see TrainSpec.skip_unchanged
    landed: list[str] = Field(default_factory=list)  # already contained in the base branch


class TrainCheckSpec(BaseModel):
    """Run the repository's check command in the clone that holds the assembled train."""

    credentials: Credentials
    key: str = Field(pattern=TRAIN_KEY)
    id: str = Field(pattern=r"^[0-9a-f]+$")  # the job this becomes
    command: str


class TrainLandSpec(BaseModel):
    """Push a checked train to the base branch (it only moves forward) and remove the landed branches."""

    credentials: Credentials
    key: str = Field(pattern=TRAIN_KEY)
    remote: RepoSpec
    base: str
    head_sha: str
    delete_branches: dict[str, str] = Field(default_factory=dict)  # branch -> the commit that landed


class BranchRef(BaseModel):
    credentials: Credentials
    remote: RepoSpec
    branch: str


class UpstreamRef(BaseModel):
    """Where newer versions of PlainBoard itself come from, and the commit that is running."""

    credentials: Credentials
    repository: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")  # owner/name on GitHub
    branch: str = Field(pattern=r"^[\w./-]+$")
    commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")


class UpstreamState(BaseModel):
    behind: int  # commits the branch has that the running commit has not
    url: str = ""  # where to read them


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


class LimitWindow(BaseModel):
    name: str  # e.g. five_hour, seven_day
    utilization: float  # fraction used; can pass 1
    resets_at: int | None = None  # unix seconds
    status: str | None = None


class LimitsEvent(BaseModel):
    """Current state of the account's plan limits, for providers that have them. Also the answer
    to a read of the limits."""

    type: Literal["limits"] = "limits"
    windows: list[LimitWindow] = Field(default_factory=list)
    # These are all the windows the account has: one that is missing no longer exists (a plan that
    # dropped a limit). False: only the windows that changed, the others stay as they were.
    complete: bool = False
    # The account's plan where the provider can tell (only in the answer to a read of the limits): its
    # name, and its size as a multiple of the provider's smallest plan, for comparing what is left in
    # absolute terms. None: not known.
    plan: str | None = None
    plan_size: float | None = None


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
    cost_usd: float = 0.0  # cost of this run
    session_cost_usd: float | None = None  # instead of cost_usd: provider reports the session's total so far
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
        LimitsEvent,
        CompactionEvent,
        ResultEvent,
        ErrorEvent,
    ],
    Field(discriminator="type"),
]

_event_adapter: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)


def parse_event(raw: str | bytes) -> RunEvent:
    return _event_adapter.validate_json(raw)
