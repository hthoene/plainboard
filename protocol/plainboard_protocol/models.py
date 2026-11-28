from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

# "full": all built-in coding tools (shell, edit, web ...) + PlainBoard tools.
# "board": PlainBoard tools + shell and file editing. Much smaller prompt -> fewer tokens.
# "read": reading only (files, search, the PlainBoard tools the app sends): no shell, no editing, no sub-agents.
# "none": only the PlainBoard tools the app sends (a doc site's assistant for visitors): no built-in tool at all.
Toolset = Literal["full", "board", "read", "none"]

# A provider account's key on the runner. The default account keeps the sign-in from before accounts existed.
DEFAULT_ACCOUNT = "default"
ACCOUNT_KEY_PATTERN = r"^[A-Za-z0-9_-]{1,40}$"
SCRATCH = ".scratch"  # an agent's folder for work data, in its workspace (made by the runner, counted to its project)


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

    # "own_login": a person's own sign-in (Claude's browser sign-in, Codex's ChatGPT sign-in), kept by the app:
    # ``login_files`` are written to a private folder only while runs of ``login_account`` go on (runner signins.py).
    # Never the runner's home, so a runner that does not know this mode refuses the run instead.
    claude_auth_mode: Literal["oauth_token", "api_key", "runner_login", "own_login"] = "oauth_token"
    claude_oauth_token: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    # "chatgpt": the ChatGPT subscription signed in on the runner; "api_key": openai_api_key.
    codex_auth_mode: Literal["chatgpt", "api_key", "own_login"] = "chatgpt"
    # Which of the runner's Codex sign-ins to use: every account has its own Codex home.
    codex_account: str = Field(default=DEFAULT_ACCOUNT, pattern=ACCOUNT_KEY_PATTERN)
    # Gemini CLI: "own_login" a Google sign-in kept by the app (login_files); "api_key": gemini_api_key.
    gemini_auth_mode: Literal["own_login", "api_key"] = "own_login"
    gemini_api_key: str | None = None
    login_account: str = Field(default=DEFAULT_ACCOUNT, pattern=ACCOUNT_KEY_PATTERN)
    login_files: dict[str, str] | None = None  # file name -> content; None: no own sign-in
    # A provider that is an endpoint (ProviderInfo.endpoint): the account's address, empty for the usual one, and its key.
    endpoint_url: str = ""
    endpoint_key: str | None = None
    endpoint_context: int = 0  # tokens its models take at once; 0: the runner's cautious guess
    github_token: str | None = None
    git_ssh_key: str | None = None  # private key for repositories with an SSH address and no key of their own
    git_author_name: str = "PlainBoard Agent"
    git_author_email: str = "agent@slop2gether.com"
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


class SkillSpec(BaseModel):
    """A Claude skill of the project for this agent. The runner fetches its files (a zip) from the app with the
    run's token (``GET /api/internal/runs/<run>/skills/<id>``) only when ``hash`` differs from what it laid down."""

    id: int
    name: str  # its folder: <workspace>/.claude/skills/<name>
    hash: str  # sha256 of its files


class SkillFetch(BaseModel):
    """Import a skill from git: the runner clones ``url`` at ``ref`` and answers with ``path`` in it as a zip."""

    url: str
    ref: str = ""
    path: str = ""
    credentials: Credentials


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
    skills: list[SkillSpec] = Field(default_factory=list)


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
    needs_code: bool = False  # the page shows a code after signing in, to be typed in here (Claude)


def files_digest(files: dict[str, str] | None) -> str:
    """What a set of sign-in files is, to tell versions apart; "" for none."""
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest() if files else ""


class SignInFiles(BaseModel):
    """An own sign-in the runner holds for the app: a finished sign-in (``base`` ""), or what a run, a limit
    check or a completion changed (a refreshed token), derived from the version with digest ``base``."""

    provider: str
    account: str
    files: dict[str, str]
    base: str = ""
    digest: str = ""


class SignInAck(BaseModel):
    account: str
    digest: str


class SignInRefresh(BaseModel):
    """A kept sign-in to refresh ahead of time where it is due soon (runner ``refresh_sign_in``)."""

    provider: str
    credentials: Credentials


class ToolImage(BaseModel):
    """A picture in a tool's result (a screenshot): the model sees it."""

    media_type: str = "image/png"
    data: str  # base64


class ToolCallResult(BaseModel):
    text: str
    is_error: bool = False
    images: list[ToolImage] = Field(default_factory=list)
    # Not done yet: ask again with this id (POST /api/internal/runs/<id>/tools/pending/<call>). Only to a bridge that
    # sent TOOL_PENDING_HEADER: a long call (a quick check, a question) must not run into a proxy's timeout.
    pending: str = ""


TOOL_PENDING_HEADER = "x-plainboard-tool-pending"


class JobSpec(BaseModel):
    """A shell command that runs in the runner independently of any agent turn."""

    id: str = Field(pattern=r"^[0-9a-f]+$")
    command: str
    workspace: str
    credentials: Credentials
    env: dict[str, str] = Field(default_factory=dict)  # e.g. PLAINBOARD_AGENT: whose it is (LeftoverSpec)


class LeftoverOwner(BaseModel):
    agent: str  # as in PLAINBOARD_AGENT, which its turns and jobs carry in their environment
    workspace: str = ""  # its own folder (not one the team shares): what runs in it, or a container mounts from it, is its too


class LeftoverSpec(BaseModel):
    """What these agents left running outside their jobs: detached processes and Docker containers."""

    owners: list[LeftoverOwner]
    stop: bool = False
    before: float | None = None  # unix time: only what started before then


class Leftover(BaseModel):
    agent: str
    kind: Literal["process", "container"]
    id: str  # the pid, or the container's id
    label: str  # its command line, or the container's name and image


class LeftoverResult(BaseModel):
    found: list[Leftover] = Field(default_factory=list)
    docker: str = ""  # why containers were not looked at ("": they were, or there is no Docker here)


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


class CpuBucket(BaseModel):
    """CPU time used in one time slot of the history, by folder ("" for what works in no agent's folder)."""

    start: float  # unix time the slot begins
    span: float = 0  # seconds of the slot that were sampled
    seconds: dict[str, float] = Field(default_factory=dict)  # folder -> CPU seconds
    load: float = 0  # mean 1-minute load average of the host


class MachineUsage(BaseModel):
    """Disk and memory of the runner's machine. Folders are measured in the background, rarely."""

    volumes: list[VolumeUsage] = Field(default_factory=list)
    folders: list[FolderUsage] = Field(default_factory=list)
    measured_at: float | None = None  # unix time the folders were measured; None: not yet
    measuring: bool = False
    memory_total: int = 0
    memory_used: int = 0  # of the machine: total minus what is available
    memory: list[ProcessMemory] = Field(default_factory=list)
    cpu_cores: int = 0
    load_average: list[float] = Field(default_factory=list)  # 1, 5, 15 minutes
    cpu_now: dict[str, float] = Field(default_factory=dict)  # folder -> cores in use over the last sample ("" : in no folder)
    cpu_step: int = 300  # seconds per slot of the history
    cpu_history: list[CpuBucket] = Field(default_factory=list)  # oldest first, 24 hours


CleanupLevel = Literal["soft", "hard"]


class CleanupFolder(BaseModel):
    """An agent's folder the app found safe to clean: its agent runs no turn and has no open assignment."""

    path: str  # two levels below the workspaces root
    idle_since: float = 0  # unix time of its agent's last turn; oldest first
    orphan: bool = False  # no agent has it any more (deleted or renamed): may go entirely if nothing in it is unpushed
    busy: bool = False  # its agent works (turn, open assignment, running job): only listed, deleted only when asked to
    clones: list[str] | None = None  # names of the project's repositories (its clones there); other folders in it are extra work folders. None: none are offered


class CleanupSpec(BaseModel):
    """Free room on the workspaces' volume with what can be made again (see runner ``cleanup.py``)."""

    level: CleanupLevel  # soft: what costs nothing to rebuild; hard: also installed dependencies
    need: int  # bytes to free; cleaning stops once that much is gone
    folders: list[CleanupFolder] = Field(default_factory=list)
    tmp_days: float = 0  # > 0: also leftovers in the temp folder nothing changed in for that long (the regular cleanup)


class CleanedItem(BaseModel):
    path: str
    what: Literal["build", "cache", "folder", "workdir", "logs", "home", "tmp"]
    bytes: int
    users: list[str] = Field(default_factory=list)  # trimmed shared cache: the agent folders whose clones use its tool


class CleanupResult(BaseModel):
    freed: int = 0
    items: list[CleanedItem] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)  # folders left alone, with the reason


CandidateSafety = Literal["safe", "busy", "unsure", "keep"]


class CleanupCandidate(BaseModel):
    """Something the manager may delete to free room (runner ``cleanup.candidates``)."""

    path: str
    what: Literal["build", "cache", "folder", "workdir", "home"]  # build output in an agent's folder, a cache, a removed agent's folder, an extra work folder in an agent's folder (a worktree, another clone), a folder in the shared home
    bytes: int = 0
    safety: CandidateSafety  # safe: can be made again; busy: its agent works now; unsure: may hold work; keep: holds unpushed work or tools
    why: str = ""
    remotes: list[str] = Field(default_factory=list)  # origin URLs of the clones in it: whose a home folder is
    mentioned: list[str] = Field(default_factory=list)  # home and cache: agent folders whose clones' files name its path
    tool: str = ""  # a shared tool cache (sccache, cargo, npm, pip, ...): nobody's, PlainBoard trims it itself
    modified: float = 0  # unix time of the newest change anywhere in it
    entries: dict[str, int] = Field(default_factory=dict)  # home and cache: its biggest top-level entries, bytes
    parent: str = ""  # home: the folder at the top of the home it is in (a folder of several agents' work); build: the home folder it is in
    agents: list[int] = Field(default_factory=list)  # home: the agents (PLAINBOARD_AGENT) whose processes work in it now


class CandidatesSpec(BaseModel):
    folders: list[CleanupFolder] = Field(default_factory=list)  # every agent folder of the projects without a folder of their own
    home: bool = True  # False: only what is in these folders, not the shared home


class UnpushedSpec(BaseModel):
    """Folders whose clones are looked at for work that is nowhere else (before a project moves to another runner)."""

    folders: list[str] = Field(default_factory=list)  # below the workspaces root


class UnpushedWork(BaseModel):
    """A clone with work that is on no remote (runner ``cleanup.unpushed_work``)."""

    folder: str  # as asked
    repo: str  # the clone, relative to the folder ("." : the folder itself)
    problems: list[str]


class TrimSpec(BaseModel):
    """Shrink the big shared tool caches, oldest files first (runner ``cleanup.trim``)."""

    need: int  # bytes to free at most
    large: int  # caches below this stay as they are
    keep: int  # no cache is trimmed below this, nor below half its size
    folders: list[CleanupFolder] = Field(default_factory=list)  # whose clones are looked at: who uses a cache


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


class RerunRef(BaseModel):
    """Workflow runs of Actions on GitHub whose failed jobs are run once more (``repo`` is owner/name)."""

    credentials: Credentials
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    run_ids: list[int] = Field(max_length=20)


class CommitRef(BaseModel):
    """A commit on GitHub whose checks are wanted (``repo`` is owner/name)."""

    credentials: Credentials
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")


class JobTiming(BaseModel):
    """How long one job of Actions took for a commit, with its slowest steps (name, seconds): where a check run's time goes."""

    name: str
    seconds: int
    steps: list[tuple[str, int]] = Field(default_factory=list)


TRAIN_KEY = r"^[\w.-]{1,80}$"


class TidySpec(BaseModel):
    """Remove the branches of a repository whose content is all on its base branch already."""

    credentials: Credentials
    key: str = Field(pattern=TRAIN_KEY)  # names the runner's clone for this repository
    remote: RepoSpec
    base: str | None = None  # None: the repository's default branch
    keep: list[str] = Field(default_factory=list)  # never these: in flight with PlainBoard, other base branches
    # owner/name on GitHub: a branch that an open pull request comes from or is based on stays, and so does a
    # protected one. If that cannot be read, nothing is removed.
    github: str | None = Field(default=None, pattern=r"^[\w.-]+/[\w.-]+$")
    min_age_hours: float = 24  # a branch whose last commit is younger stays
    stale_days: int = 14  # a branch with work of its own whose last commit is older is named in the result
    limit: int = 300


class TidyBranch(BaseModel):
    name: str
    sha: str
    how: str = ""  # removed: why its content is on the base branch (ancestor, merged, landed at <commit>)
    days: int = 0  # unmerged: how old its last commit is


class TidyResult(BaseModel):
    deleted: list[str] = Field(default_factory=list)
    removed: list[TidyBranch] = Field(default_factory=list)  # the deleted ones, with the commit they pointed to
    unmerged: list[TidyBranch] = Field(default_factory=list)  # work of their own, older than stale_days: they stay


class GitLogSpec(BaseModel):
    """The commits of a repository's base branch, for the project's searchable history (app ``services/search``)."""

    credentials: Credentials
    key: str = Field(pattern=TRAIN_KEY)  # names the runner's clone for this repository
    remote: RepoSpec
    base: str | None = None  # None: the repository's default branch
    since: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")  # only commits after this one, when it is on the branch
    limit: int = Field(default=1000, ge=1, le=5000)  # the newest ones
    upto: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")  # the older history: read from this commit instead of the head, when it is on the branch
    skip: int = Field(default=0, ge=0)  # with upto: after its newest this many (the pages read already)
    diffs: int = Field(default=0, ge=0, le=500)  # the newest this many commits come with their changed lines (the docs watcher, #190)
    diff_chars: int = Field(default=8000, ge=0, le=200_000)  # of each such diff


class GitCommit(BaseModel):
    sha: str
    author: str = ""
    date: str = ""  # ISO 8601 with offset, the commit date
    message: str = ""
    files: list[str] = Field(default_factory=list)  # the first ones changed (a renamed file: its old and its new path)
    renames: dict[str, str] = Field(default_factory=dict)  # old path -> new path, as git found them (-M)
    file_count: int = 0
    insertions: int = 0
    deletions: int = 0
    diff: str = ""  # with GitLogSpec.diffs: its changed lines (git show -U0, lock files left out), cut at diff_chars


class GitLogResult(BaseModel):
    branch: str = ""
    head: str = ""  # the branch's commit now: where the next call starts (with upto: the commit read from)
    total: int | None = None  # the branch's commits without merges (None: a runner from before it)
    commits: list[GitCommit] = Field(default_factory=list)  # newest first


class GitTagsSpec(BaseModel):
    """A repository's tags (releases), newest first; for those not ``known`` yet also what changed since the tag
    before them (the docs watcher, #190). An empty ``known``: only the list."""

    credentials: Credentials
    key: str = Field(pattern=TRAIN_KEY)
    remote: RepoSpec
    limit: int = Field(default=100, ge=1, le=1000)
    known: list[Annotated[str, Field(max_length=250)]] = Field(default_factory=list, max_length=1000)
    diffs: int = Field(default=5, ge=0, le=50)  # new tags that come with the changes since their previous tag
    diff_chars: int = Field(default=20000, ge=0, le=200_000)


class GitTag(BaseModel):
    name: str
    sha: str = ""  # the commit it names
    date: str = ""  # ISO 8601: when it was made (an annotated tag) or its commit's date
    subject: str = ""  # its message's first line (annotated) or its commit's
    previous: str = ""  # the tag before it, with the changes below
    subjects: list[str] = Field(default_factory=list)  # of the commits since ``previous``
    diff: str = ""  # their changed lines since ``previous`` (-U0, lock files left out), cut at diff_chars


class GitTagsResult(BaseModel):
    tags: list[GitTag] = Field(default_factory=list)


class GitPresentSpec(BaseModel):
    """Whether commits are still on the base branch and paths still exist at its tip, in the runner's clone
    as last fetched (no network): what a search hit says about a commit now."""

    key: str = Field(pattern=TRAIN_KEY)
    base: str = Field(min_length=1, max_length=200)
    shas: list[Annotated[str, Field(pattern=r"^[0-9a-f]{7,64}$")]] = Field(default_factory=list, max_length=2000)  # also the index's sweep
    paths: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=300)
    patterns: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=2000)  # prefixes or globs (doc pages' code paths)


class GitPresentResult(BaseModel):
    on_base: list[str] = Field(default_factory=list)  # of the shas asked
    missing: list[str] = Field(default_factory=list)  # of the paths asked
    unmatched: list[str] | None = None  # of the patterns asked: those that match no file at the tip (None: a runner from before it)


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


class PromoteSpec(BaseModel):
    """Promotion (#129): what goes from the base branch (e.g. dev) to the release branch (e.g. main). In a
    clone of its own, so trains of the base branch go on meanwhile. ``checkout``: the commit is checked out
    there for the check command (the promotion), else only read (a preview)."""

    credentials: Credentials
    key: str = Field(pattern=TRAIN_KEY)
    remote: RepoSpec
    base: str | None = Field(default=None, max_length=200)  # None: the repository's default branch
    release: str = Field(min_length=1, max_length=200)
    sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")  # None: the head of the base branch now
    checkout: bool = False
    limit: int = Field(default=200, ge=1, le=2000)


class PromotePlan(BaseModel):
    base: str
    release: str
    sha: str  # the fixed commit of the base branch that is promoted
    release_sha: str | None = None  # the release branch now; None: it does not exist yet
    fast_forward: bool = True  # the release branch is an ancestor of sha
    total: int = 0  # commits on sha that the release branch lacks (merges included)
    commits: list[GitCommit] = Field(default_factory=list)  # newest first, without merges, at most ``limit``
    first_parent: list[str] = Field(default_factory=list)  # the base branch's own commits in the range (train heads), newest first


class PromotePushSpec(BaseModel):
    """Bring a checked commit of the base branch to the release branch: a fast-forward push, else a merge
    commit. A push the host refuses (a protected branch) becomes a pull request when ``github`` names the
    repository (owner/name)."""

    credentials: Credentials
    key: str = Field(pattern=TRAIN_KEY)
    remote: RepoSpec
    base: str = Field(min_length=1, max_length=200)
    release: str = Field(min_length=1, max_length=200)
    sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    tag: str = Field(default="", max_length=100)
    github: str | None = None
    title: str = Field(default="", max_length=300)


class PromoteResult(BaseModel):
    ok: bool
    how: str = ""  # fast-forward | merge | pull-request | "" (failed)
    sha: str = ""  # what the release branch points to now (a pull request: the commit it proposes)
    number: int | None = None  # pull-request: its number
    url: str = ""
    detail: str = ""


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
    # The running release ("X.Y.Z"); "" for a build of a commit (:edge, a local build): then it is compared with the branch.
    version: str = Field(default="", pattern=r"^(\d+\.\d+\.\d+)?$")


class UpstreamState(BaseModel):
    behind: int  # commits the branch has that the running commit has not; for a release: newer releases
    url: str = ""  # where to read them
    latest: str = ""  # for a release: the newest one ("X.Y.Z"), if it is newer


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
    # The model (family) the window limits, as the provider names it (e.g. "Fable"); None: all of the plan's usage.
    model: str | None = None


class LimitsEvent(BaseModel):
    """Current state of the account's plan limits, for providers that have them. Also the answer
    to a read of the limits."""

    type: Literal["limits"] = "limits"
    windows: list[LimitWindow] = Field(default_factory=list)
    # These are all the windows the account has: one that is missing no longer exists (a plan that
    # dropped a limit). False: only the windows that changed, the others stay as they were.
    complete: bool = False
    # The windows this report speaks for in full: one of these it leaves out no longer exists, the
    # others stay as they were (a provider that reports some windows only with requests touching them).
    covers: list[str] = Field(default_factory=list)
    # The account's plan where the provider can tell (only in the answer to a read of the limits): its
    # name, and its size as a multiple of the provider's smallest plan, for comparing what is left in
    # absolute terms. None: not known.
    plan: str | None = None
    plan_size: float | None = None
    # The models this account may use, where the provider lists them per account (only in the answer to a
    # read of the limits). None: not known.
    models: list[ModelInfo] | None = None


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
