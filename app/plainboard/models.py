"""ORM models. All timestamps are naive UTC (serialized with a trailing Z by the schemas)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, Column, Computed, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Table, Text, UniqueConstraint, cast, false, true
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates
from sqlalchemy.types import UserDefinedType


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# --- enums (single source of truth; extend here) ------------------------------------------


class TaskStatus(StrEnum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunTrigger(StrEnum):
    CHAT = "chat"  # user message in an agent chat
    TICKET = "ticket"  # a ticket was handed to the manager
    ASSIGNMENT = "assignment"  # manager delegated a work order to an agent
    ASK = "ask"  # blocking question from another agent
    MESSAGE = "message"  # non-blocking message from another agent
    SCHEDULE = "schedule"
    WAKE = "wake"  # inbox updates (e.g. finished assignments) for the agent
    QUESTION = "question"  # a person's question (orchestration/questions.py): stored when it ends, for the statistics only
    IDEAS = "ideas"  # sorting ideas or planning tickets from them (orchestration/ideas.py): for the statistics only
    DOCS = "docs"  # translating doc pages (services/docs_translate.py): for the statistics only
    DOC_ASSISTANT = "doc_assistant"  # a visitor's question to a doc site's assistant (orchestration/docs_assistant.py): statistics only


class AssignmentStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DecisionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    WITHDRAWN = "withdrawn"


class LandingStatus(StrEnum):
    WAITING = "open"  # handed over: goes on the next train
    TRAIN = "train"  # on a train whose checks are running
    BACK = "back"  # it did not pass or conflicts: it is being repaired
    MERGED = "merged"  # on the base branch
    CLOSED = "closed"
    STUCK = "stuck"  # handed to the manager


class TrainStatus(StrEnum):
    CHECKING = "checking"
    BLOCKED = "blocked"  # checks passed, but it could not be merged: someone has to
    LANDED = "landed"
    FAILED = "failed"
    ABORTED = "aborted"  # given up without a result (e.g. the runner restarted): its branches go on the next one


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    LOST = "lost"  # the runner restarted while it was running


ACTIVE_RUN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)


# --- settings -------------------------------------------------------------------------------


class Account(Timestamps, Base):
    """A subscription or API key of a provider. Agents use the one with the most room left unless pinned."""

    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))
    label: Mapped[str] = mapped_column(String(100))
    key: Mapped[str] = mapped_column(String(40), unique=True)  # the account on the runner (e.g. its Codex home)
    auth_mode: Mapped[str] = mapped_column(String(20))
    secret_enc: Mapped[str | None] = mapped_column(Text)  # token or API key; None = the environment's
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # A provider that is an endpoint (a local model server, a model host): where it is, empty for the
    # provider's usual address, and the models this account offers (empty: the provider's own list).
    endpoint_url: Mapped[str] = mapped_column(String(500), default="", server_default="")
    models: Mapped[list[str]] = mapped_column(JSON, default=list)
    # How many tokens these models take at once (0: not said). The agent's harness keeps its context below it.
    context_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # How large the subscription's plan is, as a multiple of the provider's smallest plan, as the user
    # gave it. None: as the provider says (services/limits.py), else as large as the others.
    plan_size: Mapped[float | None] = mapped_column(Float)
    # A person's own subscription (None: the installation's, which an admin gives to people, users.account_ids),
    # and who else may use it: nobody ("private"), the people in ``shared_with`` ("people"), or everyone.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    share: Mapped[str] = mapped_column(String(20), default="private", server_default="private")
    shared_with: Mapped[list[int] | None] = mapped_column(JSON)
    # The installation's browser sign-in from before #84, still in the built-in runner's home only: the app
    # copies it over once (services/accounts.take_over), from then on it is kept here like any other.
    home_login: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # The projects its owner gave it to themselves, as a member of each (#93): their runs may use it, whatever
    # ``share`` says, while the owner works in the project and its owner has not switched it off there.
    project_ids: Mapped[list[int] | None] = mapped_column(JSON)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    secret: Mapped[bool] = mapped_column(Boolean, default=False)


class Runner(Timestamps, Base):
    """An external runner: a runner on another machine that connects out to PlainBoard with its token
    (orchestration/runners.py). The built-in runner has no row. Installation data, not a project's."""

    __tablename__ = "runners"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 of the token, which is shown once
    version: Mapped[str] = mapped_column(String(64), default="", server_default="")  # the commit it said it runs, last time
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    away_told_at: Mapped[datetime | None] = mapped_column(DateTime)  # its managers heard it is away (once per absence)
    update_told: Mapped[str] = mapped_column(String(80), default="", server_default="")  # its managers heard this about its self-update ("failed:<commit>", "manual:<commit>")


# --- projects & agents ----------------------------------------------------------------------


class Project(Timestamps, Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # What agents write to people in, e.g. "German". Empty: the language each person writes in.
    language: Mapped[str] = mapped_column(String(50), default="", server_default="")
    # Languages of the customer texts (codes), the first is the default (services/customer_texts.py).
    customer_languages: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["en"], server_default='["en"]')
    # Optional shared working directory (relative to the workspaces root, e.g. a bind-mounted
    # host folder). Unset = every agent gets its own directory/clone.
    workspace_path: Mapped[str | None] = mapped_column(String(500))
    # Maintained by the manager: installs what this project needs, so it survives container rebuilds.
    setup_script: Mapped[str] = mapped_column(Text, default="")
    github_token_enc: Mapped[str | None] = mapped_column(Text)
    auto_wake_manager: Mapped[bool] = mapped_column(Boolean, default=True)
    # When nothing in To do can start, the team goes on with the Backlog. Off: Backlog waits until the user moves it.
    continue_with_backlog: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # PlainBoard measures the check runs and has slow ones made faster (landing._checks_slow); off: none of it.
    fast_checks: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Its history is searchable for its agents (services/search): only with PostgreSQL and pgvector, and while the setting is on.
    semantic_search: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Members of this project working at the same time. Unset: the number from the settings (`max_parallel_runs`).
    parallel_agents: Mapped[int | None] = mapped_column(Integer)
    # The subscription this project's agents run on, unless an agent has its own. Unset: the one with the most room.
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    # The only accounts this project's agents may run on, set by an admin (empty: any). A project
    # shared with others does not draw on a subscription that is meant for something else.
    account_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    # Subscriptions members gave the project (``Account.project_ids``) that its owner switched off for it.
    blocked_account_ids: Mapped[list[int] | None] = mapped_column(JSON)
    # The runner its agents, jobs and trains run on. Unset: the built-in one.
    runner_id: Mapped[int | None] = mapped_column(ForeignKey("runners.id", ondelete="SET NULL"))
    # Whose project it is: the person who created it. What that person may use (``services/permissions.py``)
    # holds for it: subscriptions, runners, members at work. Unset, or an admin's: everything.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Paused by a person, for good: nothing of this project starts until a person resumes it
    # (RunManager.pause_project). Not ended by a restart, nor when "pause everything" ends.
    paused_at: Mapped[datetime | None] = mapped_column(DateTime)
    paused_by: Mapped[str] = mapped_column(String(100), default="", server_default="")
    task_counter: Mapped[int] = mapped_column(Integer, default=0)
    label_colors: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)  # label name -> color name
    auto_archive_days: Mapped[int] = mapped_column(Integer, default=14)  # done tickets leave the board after this; 0 = never
    review_days: Mapped[int] = mapped_column(Integer, default=3)  # tickets left in review go to the manager after this; 0 = never
    # Files of tickets done this long ago are deleted, their entries stay (services/retention.py); 0 = never.
    # Files of done tickets (services/retention.py); None: the PlainBoard default (settings), 0: never.
    attachment_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attachment_retention_min_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)  # only files from this size; 0: all; None with the days: the default
    # What answers people's questions (orchestration/questions.py). Empty: the default (questions.DEFAULT_*).
    ask_provider: Mapped[str] = mapped_column(String(50), default="", server_default="")
    ask_model: Mapped[str] = mapped_column(String(100), default="", server_default="")
    ask_effort: Mapped[str] = mapped_column(String(20), default="", server_default="")
    # What sorts its ideas into topics (orchestration/ideas.py). Empty: the same as its questions.
    idea_provider: Mapped[str] = mapped_column(String(50), default="", server_default="")
    idea_model: Mapped[str] = mapped_column(String(100), default="", server_default="")
    idea_effort: Mapped[str] = mapped_column(String(20), default="", server_default="")

    agents: Mapped[list[Agent]] = relationship(back_populates="project", cascade="all, delete-orphan", passive_deletes=True)


class PublicApiKey(Base):
    """A key for the read-only public API of a project (customer texts of its tickets). A project
    can have several, each with a label saying where it is used. Only the hash is stored."""

    __tablename__ = "public_api_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(200), default="")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    hint: Mapped[str] = mapped_column(String(8), default="")  # last characters, to recognise the key
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


class AccessKey(Base):
    """A key that may read this PlainBoard's API and change nothing (services/access_keys.py):
    for a tool or a person that looks at how the agents work. Only the hash is stored."""

    __tablename__ = "access_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(200), default="")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    hint: Mapped[str] = mapped_column(String(8), default="")  # last characters, to recognise the key
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


class User(Timestamps, Base):
    """A person who signs in. An admin runs the installation (settings, subscriptions, users) and
    sees every project; everyone else sees the projects they are a member of."""

    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)  # what they sign in with; lower case
    name: Mapped[str] = mapped_column(String(100))  # shown to people and agents
    password_hash: Mapped[str] = mapped_column(Text)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    may_create_projects: Mapped[bool] = mapped_column(Boolean, default=False)  # projects use the installation's subscriptions
    # What the projects this person owns (``Project.owner_id``) may use; an admin's: everything (services/permissions.py).
    max_projects: Mapped[int | None] = mapped_column(Integer)  # own projects at most; unset: any number
    account_ids: Mapped[list[int] | None] = mapped_column(JSON)  # the only subscriptions; unset: all of them
    runner_ids: Mapped[list[int] | None] = mapped_column(JSON)  # the only runners, 0 the built-in one; unset: all of them
    max_parallel: Mapped[int | None] = mapped_column(Integer)  # members at work across them at once; unset: no limit of its own
    may_use_shared: Mapped[bool] = mapped_column(Boolean, default=False)  # the global secrets, the installation's GitHub token and SSH key
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=0)  # raised to sign the user out everywhere
    # The profile picture: a small square the browser made. Read only where it is shown (deferred);
    # the version is part of its address, so a new one is seen at once and the old one may be cached for good.
    avatar: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    avatar_type: Mapped[str | None] = mapped_column(String(30))
    avatar_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 0: none
    show_mentions: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())  # the @ list and browser notifications; mentions are recorded either way


class PersonalToken(Base):
    """A person's own token for MCP clients and the API (services/personal_tokens.py): it acts as that
    person, with their roles, optionally reading only. Only the hash is stored. Not project data."""

    __tablename__ = "personal_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(200), default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    hint: Mapped[str] = mapped_column(String(8), default="")  # last characters, to recognise the token
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


class McpCall(Base):
    """One tool call through PlainBoard's MCP server (api/mcp_server.py), for the audit. Plain numbers,
    no foreign keys: the log outlives the token. Not project data."""

    __tablename__ = "mcp_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    token_id: Mapped[int] = mapped_column(Integer)
    tool: Mapped[str] = mapped_column(String(60))
    project_id: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(String(300), default="")


class UserSession(Base):
    """A signed-in browser. The login cookie names one; signing out deletes it, so a copied cookie is
    of no use afterwards. Not project data: moving a project takes none along."""

    __tablename__ = "user_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class ProjectRole(StrEnum):
    VIEWER = "viewer"  # reads everything of the project, changes nothing
    MEMBER = "member"  # works in it: tickets, chats with the agents, the team, notes
    OWNER = "owner"  # also its settings, repositories, secrets and members; may delete it


class ProjectMember(Base):
    """A user's part in a project. No row: the project does not exist for that user (admins aside)."""

    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10), default=ProjectRole.MEMBER)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Repository(Timestamps, Base):
    """A git repository belonging to a project; cloned into ``<workspace>/<name>``."""

    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))  # folder name inside the workspace
    url: Mapped[str] = mapped_column(String(500))
    default_branch: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")  # what it contains (helps routing work)
    # Decides whether finished branches may land: PlainBoard runs it on all of them merged together
    # and pushes to the base branch if it succeeds. Works with any git host. Empty: on GitHub, the
    # checks of a pull request for the merged branches decide.
    check_command: Mapped[str] = mapped_column(Text, default="", server_default="")
    # The fast part of the checks (format, lint, ...). PlainBoard runs it on a branch at the moment
    # its author hands it over, in the author's clone: what it catches never costs a check run.
    quick_check_command: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Land first, check after: finished branches are merged into the base branch at once (after the
    # quick check), and the checks run on the base branch afterwards; a red base is repaired by one
    # assignment. For a development branch. Off: nothing lands before its checks passed (trains).
    land_first: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Remove branches whose content is all on the base branch already, every hour (orchestration/tidy.py).
    tidy_branches: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    tidied_at: Mapped[datetime | None] = mapped_column(DateTime)
    tidy_listed: Mapped[list[str] | None] = mapped_column(JSON)  # old branches with work of their own the manager was told of
    # When the manager was last told that most of this repository's check runs fail (landing._keeps_failing).
    failing_told_at: Mapped[datetime | None] = mapped_column(DateTime)
    red_told_at: Mapped[datetime | None] = mapped_column(DateTime)  # the manager was last reminded that its base branch stays red
    # When the manager was last told to have this repository's slow check runs made faster, and their median then (landing._checks_slow).
    slow_told_at: Mapped[datetime | None] = mapped_column(DateTime)
    slow_told_seconds: Mapped[int | None] = mapped_column(Integer)
    # How long its check run should take, set by an owner (0: no target), and the verdict of the last
    # speed-up: the median it reached (sped up, or what it takes), when, and in words. Growth is measured against it.
    check_target_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    checks_baseline_seconds: Mapped[int | None] = mapped_column(Integer)
    checks_baseline_at: Mapped[datetime | None] = mapped_column(DateTime)
    checks_verdict: Mapped[str] = mapped_column(String(300), default="", server_default="")
    # How git reaches it: "default" (the general GitHub token, or none), "token" (its own HTTPS
    # access token, any host) or "ssh" (its own key pair, e.g. a deploy key).
    auth: Mapped[str] = mapped_column(String(20), default="default", server_default="default")
    token_user: Mapped[str] = mapped_column(String(200), default="", server_default="")
    token_enc: Mapped[str | None] = mapped_column(Text)
    ssh_key_enc: Mapped[str | None] = mapped_column(Text)
    ssh_public_key: Mapped[str] = mapped_column(Text, default="", server_default="")


agent_repositories = Table(
    "agent_repositories",
    Base.metadata,
    Column("agent_id", ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
    Column("repository_id", ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True),
)

agent_mcp_servers = Table(
    "agent_mcp_servers",
    Base.metadata,
    Column("agent_id", ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
    Column("mcp_server_id", ForeignKey("mcp_servers.id", ondelete="CASCADE"), primary_key=True),
)


class Agent(Timestamps, Base):
    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("project_id", "slug"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(200), default="")
    is_manager: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")  # area of responsibility
    instructions: Mapped[str] = mapped_column(Text, default="")  # extra system prompt
    provider: Mapped[str] = mapped_column(String(50), default="claude_code")
    model: Mapped[str] = mapped_column(String(100))
    effort: Mapped[str | None] = mapped_column(String(20))
    toolset: Mapped[str] = mapped_column(String(20), default="full")
    context_limit_tokens: Mapped[int] = mapped_column(Integer, default=350000)
    keep_recent_messages: Mapped[int] = mapped_column(Integer, default=6)
    summary_model: Mapped[str] = mapped_column(String(100), default="claude-haiku-5-5")
    summary_effort: Mapped[str | None] = mapped_column(String(20))  # None: the installation's (default_summary_effort)
    max_turns: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Paused together with the rest of the team (pause_team): resume_team enables exactly these again.
    team_pause: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    color: Mapped[str] = mapped_column(String(20), default="#6366f1")
    all_repos: Mapped[bool] = mapped_column(Boolean, default=True)  # False = only `repositories`
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))  # None = automatic
    # The doc agent of this doc site (#118): it alone writes the site's pages; hidden from the team and the board.
    doc_site_id: Mapped[int | None] = mapped_column(ForeignKey("doc_sites.id", ondelete="CASCADE"), index=True)

    project: Mapped[Project] = relationship(back_populates="agents")
    repositories: Mapped[list[Repository]] = relationship(secondary=agent_repositories, lazy="selectin")
    mcp_servers: Mapped[list[McpServer]] = relationship(secondary=agent_mcp_servers, lazy="selectin")


# --- board ----------------------------------------------------------------------------------


class Task(Timestamps, Base):
    """A ticket on the board. Tickets are never assigned to agents directly: the manager
    reads them and delegates clear work orders (assignments)."""

    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("project_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    # Public via the API. No title = hidden from customers.
    customer_title: Mapped[str] = mapped_column(String(150), default="")
    customer_description: Mapped[str] = mapped_column(Text, default="")
    # The other customer languages: code -> {"title", "description"}. The default language's are the two above.
    customer_texts: Mapped[dict[str, dict[str, str]]] = mapped_column(JSON, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), default=TaskStatus.BACKLOG)
    position: Mapped[float] = mapped_column(Float, default=0.0)
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    # What only the user can do for this ticket (e.g. test on a phone). Highlights it until done.
    user_action: Mapped[str] = mapped_column(String(500), default="")
    # Archived tickets are off the board but keep their history and links.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    # When it last went to the manager for lying in review unchanged (projects.review_days).
    review_handed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))


class IdeaStatus(StrEnum):
    OPEN = "open"
    TAKEN = "taken"  # became (part of) a ticket: task_id
    DISMISSED = "dismissed"  # left out on purpose (closed_reason), or its creation was undone
    MERGED = "merged"  # went into another idea (merged_into_id)


class IdeaTopic(Timestamps, Base):
    """A topic of a project's ideas: an island on the board of ideas, made by the model that sorts them
    (services/ideas.py) or by a person."""

    __tablename__ = "idea_topics"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    color: Mapped[int] = mapped_column(Integer, default=0)  # index into the design's --chart-1..10 (0-9)
    hint: Mapped[str] = mapped_column(String(300), default="")  # the model's short note, e.g. "3 ideas about payment"
    x: Mapped[float | None] = mapped_column(Float)  # where the island lies; None: the UI places it
    y: Mapped[float | None] = mapped_column(Float)
    manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # made or renamed by a person: the model keeps its name


class Idea(Timestamps, Base):
    """An idea of a project. Collecting and sorting ideas starts nothing: only a person turns them into tickets."""

    __tablename__ = "ideas"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(20), default=IdeaStatus.OPEN, index=True)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("idea_topics.id", ondelete="SET NULL"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))  # the ticket it went into
    hint: Mapped[str] = mapped_column(String(300), default="")  # the model's short note, e.g. "similar to ..."
    # Sorting by the model: pending (waiting for it), sorted, failed (left unsorted; "sort again" retries).
    sort_state: Mapped[str] = mapped_column(String(10), default="pending")
    x: Mapped[float | None] = mapped_column(Float)  # where the card lies; None: the UI places it
    y: Mapped[float | None] = mapped_column(Float)
    votes: Mapped[int] = mapped_column(Integer, default=0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)  # taken, dismissed or merged
    closed_reason: Mapped[str] = mapped_column(String(300), default="")
    # Placed by a person (moved to a topic or on the board): the model leaves its topic alone.
    topic_manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("ideas.id", ondelete="SET NULL"))
    # The model's question about it, as a decision for the person: options [{label, description}], the first recommended.
    question: Mapped[str] = mapped_column(Text, default="", server_default="")
    question_options: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, server_default="[]")
    question_status: Mapped[str] = mapped_column(String(10), default="", server_default="")  # "" | open | answered | withdrawn
    question_answer: Mapped[str] = mapped_column(Text, default="", server_default="")
    question_at: Mapped[datetime | None] = mapped_column(DateTime)


class IdeaLink(Base):
    """The model's finding that an idea is similar to, or a duplicate of, another one."""

    __tablename__ = "idea_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    idea_id: Mapped[int] = mapped_column(ForeignKey("ideas.id", ondelete="CASCADE"), index=True)
    other_id: Mapped[int] = mapped_column(ForeignKey("ideas.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="similar")  # services/ideas.py: duplicate | kept | split | conflict | resolved (similar: no longer made)


class IdeaVersion(Base):
    """An earlier title and text of an idea, kept whenever they change: by a person, the model, or an undo."""

    __tablename__ = "idea_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    idea_id: Mapped[int] = mapped_column(ForeignKey("ideas.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(10), default="user")  # who replaced it: user | ai | undo
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IdeaChange(Base):
    """What happened to the ideas in one step (an input read by the model, an edit, a rearrangement of the
    topics), for the UI to show and to undo. ``undo`` holds the state before it (services/ideas.undo)."""

    __tablename__ = "idea_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # new | several | amend | merge | split | resolve | question | edit | reorder
    source: Mapped[str] = mapped_column(String(10), default="input")  # input | answer | user | auto | button
    author: Mapped[str] = mapped_column(String(100), default="")
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    undo: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime)


class TicketState(Base):
    """Where a ticket stands, written by PlainBoard from the reports of its assignments
    (services/ticket_state.py): one text that is replaced, not a list that grows."""

    __tablename__ = "ticket_states"
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    until_assignment_id: Mapped[int] = mapped_column(Integer, default=0)  # the last report it includes
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TaskTransition(Base):
    """A ticket entering a status, for the statistics. Keeps the number, so it outlives the ticket."""

    __tablename__ = "task_transitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    task_number: Mapped[int] = mapped_column(Integer)
    from_status: Mapped[str | None] = mapped_column(String(20))  # None: the ticket was created
    status: Mapped[str] = mapped_column(String(20))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    author_kind: Mapped[str] = mapped_column(String(10), default="user")  # user | agent | system
    author_name: Mapped[str | None] = mapped_column(String(100))  # which person, where a project has several
    author_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    # A file of an idea (#100): never deleted by the retention, kept when the idea is closed.
    idea_id: Mapped[int | None] = mapped_column(ForeignKey("ideas.id", ondelete="CASCADE"), index=True)
    # A file of a doc site (#118): never deleted by the retention or the cleanup, only with its site or by hand.
    doc_site_id: Mapped[int | None] = mapped_column(ForeignKey("doc_sites.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(150))
    size: Mapped[int] = mapped_column(Integer)
    storage_name: Mapped[str] = mapped_column(String(100), unique=True)
    uploaded_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Its file was deleted by the project's retention (services/retention.py); the entry stays with name and size.
    purged_at: Mapped[datetime | None] = mapped_column(DateTime)
    purged_after_days: Mapped[int | None] = mapped_column(Integer)


class DocSite(Timestamps, Base):
    """A documentation site of a project (#118, services/docs.py): pages in a tree, written by people and agents,
    served without login at /docs/<slug> once published, and at its own domain once that is verified."""

    __tablename__ = "doc_sites"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)  # /docs/<slug>: unique in the installation
    description: Mapped[str] = mapped_column(String(300), default="")  # for search engines and link previews
    # The doc spec: what the user wants in it (content, structure, tone, audience), written by the manager with
    # the user. Every agent that writes the site reads it with the site and follows it.
    spec: Mapped[str] = mapped_column(Text, default="")
    published: Mapped[bool] = mapped_column(Boolean, default=False)  # served at all (else a draft, in the app only)
    public: Mapped[bool] = mapped_column(Boolean, default=False)  # served to everyone; else only to people with a role in the project
    language: Mapped[str] = mapped_column(String(20), default="en")  # what its pages are written in
    languages: Mapped[list[str]] = mapped_column(JSON, default=list)  # further languages, translated (services/docs_translate.py)
    translator: Mapped[str] = mapped_column(String(10), default="")  # llm | deepl | google | "" (none: translations by hand)
    translate_provider: Mapped[str] = mapped_column(String(50), default="")  # llm: the provider and model ("": the provider's small one)
    translate_model: Mapped[str] = mapped_column(String(100), default="")
    deepl_key_enc: Mapped[str | None] = mapped_column(Text)  # write-only
    google_key_enc: Mapped[str | None] = mapped_column(Text)  # write-only
    domain: Mapped[str | None] = mapped_column(String(253), unique=True)  # its own host name, served by Host header
    domain_token: Mapped[str] = mapped_column(String(64), default="")  # answered at /.well-known/plainboard-docs on that host
    domain_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    cloudflare_token_enc: Mapped[str | None] = mapped_column(Text)  # write-only: creates the DNS record
    # Its assistant for visitors (#119, orchestration/docs_assistant.py): off by default; its own model ("": the
    # provider's small one); ``assistant_code`` lets it read the code and the board's public texts (a risk: see there).
    assistant_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    assistant_provider: Mapped[str] = mapped_column(String(50), default="", server_default="")
    assistant_model: Mapped[str] = mapped_column(String(100), default="", server_default="")
    assistant_effort: Mapped[str] = mapped_column(String(20), default="", server_default="")
    assistant_code: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    assistant_daily_messages: Mapped[int] = mapped_column(Integer, default=300, server_default="300")  # of all visitors
    assistant_daily_cost: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")  # USD; 0: no cap
    assistant_max_tokens: Mapped[int] = mapped_column(Integer, default=1024, server_default="1024")  # per answer
    # Its look (#121, services/docs_theme.py): logos, colours, font, corners, header links, custom CSS. Empty: the
    # look every site has.
    theme: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")


class DocPage(Timestamps, Base):
    """A page of a doc site. Every saved state is a ``DocPageVersion``."""

    __tablename__ = "doc_pages"
    __table_args__ = (Index("ix_doc_pages_tree", "site_id", "parent_id", "position"), Index("ix_doc_pages_path", "site_id", "path"))
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("doc_sites.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("doc_pages.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    # Versions of the site (#126, services/docs_releases.py): a row holds the page in the versions ``since_seq`` up to
    # ``until_seq`` (None: up to the working version); ``page_key`` is the page across versions (the id of its row
    # with the highest range), what ``parent_id``, links (doc-page:<key>) and every API name it by.
    page_key: Mapped[int | None] = mapped_column(ForeignKey("doc_pages.id", ondelete="CASCADE"), index=True)
    since_seq: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    until_seq: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120))
    path: Mapped[str] = mapped_column(String(2000), default="")  # its address under the site: the slugs from the top (kept on every move)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    body: Mapped[str] = mapped_column(Text, default="")
    source_hash: Mapped[str] = mapped_column(String(16), default="")  # of title and text: a translation of another one is outdated
    # Code paths it describes (prefixes or globs, e.g. app/plainboard/services/docs.py, runner/*): a change there
    # after a landing marks it for an update (orchestration/docs_watcher.py).
    paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    edited_by: Mapped[str] = mapped_column(String(100), default="")
    edited_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # its text last changed (a move is no edit)
    drift: Mapped[int] = mapped_column(Integer, default=0)  # commits that changed its paths since that
    flagged_at: Mapped[datetime | None] = mapped_column(DateTime)  # since when it may be outdated (an edit clears it)
    flag_reason: Mapped[str] = mapped_column(Text, default="")
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime)  # the manager last heard it is stale


class DocRelease(Timestamps, Base):
    """A version of a doc site (#126, services/docs_releases.py): the working one (``working``, one per site, what is
    written now) or one cut from it (``seq``: the order of cutting). The working one's ``seq`` is the one the next cut
    gets. ``hidden``: not served (for the working one: the switch whether it is public at all)."""

    __tablename__ = "doc_releases"
    __table_args__ = (UniqueConstraint("site_id", "slug"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("doc_sites.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=1)
    working: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(80))
    slug: Mapped[str] = mapped_column(String(40))  # its address segment: /docs/<site>/<slug>/
    notes: Mapped[str] = mapped_column(Text, default="")
    released_on: Mapped[datetime | None] = mapped_column(DateTime)
    deprecated: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)  # what visitors get at /docs/<site>/ (none: the latest released)


class DocPageVersion(Base):
    """A saved state of a page: who (a person or an agent), when, why. The newest is the page as it is."""

    __tablename__ = "doc_page_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("doc_pages.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(100), default="")
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    reason: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DocThemeVersion(Base):
    """A saved state of a site's theme (#128, services/docs_theme.py): who, when, why. The newest is the theme as it is."""

    __tablename__ = "doc_theme_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("doc_sites.id", ondelete="CASCADE"), index=True)
    theme: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    author: Mapped[str] = mapped_column(String(100), default="")
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    reason: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DocTranslation(Base):
    """A page in another language of its site (services/docs_translate.py). ``source_hash``: of the page it was made from."""

    __tablename__ = "doc_translations"
    __table_args__ = (UniqueConstraint("page_id", "lang"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("doc_sites.id", ondelete="CASCADE"), index=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("doc_pages.id", ondelete="CASCADE"), index=True)
    lang: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    source_hash: Mapped[str] = mapped_column(String(16), default="")
    translated_by: Mapped[str] = mapped_column(String(100), default="")  # deepl | google | the model | a person's name
    translated_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str] = mapped_column(Text, default="")  # the last attempt failed: tried again after a while
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime)


class DocCustomPage(Timestamps, Base):
    """A page of a site outside its docs tree (#121, services/docs_custom.py): an imprint, a privacy policy, linked
    in the site's footer at /p/<slug>. Written by people only (legal texts): no agent tool reaches it, the search
    and the assistant leave it out. ``texts``: its title and text in the site's further languages, by hand."""

    __tablename__ = "doc_custom_pages"
    __table_args__ = (UniqueConstraint("site_id", "slug"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("doc_sites.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text, default="")
    texts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")  # lang -> {title, body}
    edited_by: Mapped[str] = mapped_column(String(100), default="")


class DocWatch(Base):
    """How far the docs watcher read a repository of a project: its last commit, and the last landing it took in."""

    __tablename__ = "doc_watch"
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True)
    head: Mapped[str] = mapped_column(String(64), default="")
    landed_at: Mapped[datetime | None] = mapped_column(DateTime)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Decision(Timestamps, Base):
    """A question from the manager to the user on a ticket. Attachments referenced here are meant
    for the user's review, not as input for other agents."""

    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    requested_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    question: Mapped[str] = mapped_column(Text)
    details: Mapped[str] = mapped_column(Text, default="")
    options: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # label, description, attachment_ids
    attachment_ids: Mapped[list[int]] = mapped_column(JSON, default=list)  # not tied to a single option
    status: Mapped[str] = mapped_column(String(20), default=DecisionStatus.OPEN)
    answer_option: Mapped[int | None] = mapped_column(Integer)  # index into options
    answer_text: Mapped[str] = mapped_column(Text, default="")
    answered_by: Mapped[str | None] = mapped_column(String(10))  # user | agent
    answered_at: Mapped[datetime | None] = mapped_column(DateTime)


class Assignment(Timestamps, Base):
    """A clear work order from the manager (or another agent) to one agent, optionally for a ticket."""

    __tablename__ = "assignments"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    created_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(300))
    instructions: Mapped[str] = mapped_column(Text)
    effort: Mapped[str | None] = mapped_column(String(20))  # overrides the agent's effort for this work order
    status: Mapped[str] = mapped_column(String(20), default=AssignmentStatus.QUEUED)
    result: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[int | None] = mapped_column(Integer)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Set when PlainBoard itself ordered this work: repairing that branch, which broke a train or conflicts.
    landing_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # Or repairing the branches of that train, which did not pass together.
    train_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # The agent's scratchpad for this piece of work (services/scratchpad.py): it survives context
    # compression, stays archived for a while after the assignment ended, then goes.
    scratchpad: Mapped[str | None] = mapped_column(Text)


class Landing(Timestamps, Base):
    """A finished branch an agent handed to PlainBoard (merge_when_green). PlainBoard brings it to
    the base branch without an agent turn, together with the other finished branches: one check
    run for all of them (a train)."""

    __tablename__ = "landings"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    branch: Mapped[str] = mapped_column(String(250), default="")
    number: Mapped[int | None] = mapped_column(Integer)  # its own pull request, if the agent opened one
    url: Mapped[str] = mapped_column(String(500), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))  # the author
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    assignment_id: Mapped[int | None] = mapped_column(Integer)  # the assignment it was last handed over in (a repair: this is its fix)
    status: Mapped[str] = mapped_column(String(20), default=LandingStatus.WAITING, index=True)
    head_sha: Mapped[str] = mapped_column(String(64), default="")
    failed_sha: Mapped[str] = mapped_column("handled_sha", String(64), default="")  # the head it last failed with
    failures: Mapped[int] = mapped_column(Integer, default=0)  # times it went back to its author
    train_id: Mapped[int | None] = mapped_column(Integer, index=True)  # the train it is on, or landed with
    # Branches that failed together and are repaired together share a number: they are checked
    # again as that group, before anything new joins them.
    group_no: Mapped[int | None] = mapped_column(Integer)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime)
    # It could not be merged: since when, and in which files while its author resolves that. Until it
    # is back, branches that change these files wait (landings.busy); once it is, it goes first.
    conflict_at: Mapped[datetime | None] = mapped_column(DateTime)
    conflict_files: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Held (#96): it does not land, but stays on the list. "message": its author got a message after handing
    # it over and has to hand it over again (a new commit, or the same one to confirm it). "manager": held by
    # hand until release_branch. "next": handed over again while on a train: not with that train, with the next.
    hold: Mapped[str] = mapped_column(String(20), default="", server_default="")
    held_at: Mapped[datetime | None] = mapped_column(DateTime)
    hold_reason: Mapped[str] = mapped_column(String(300), default="", server_default="")
    hold_told_at: Mapped[datetime | None] = mapped_column(DateTime)  # the manager heard it has been held for long
    # Closed because a repair branch carries it (landings.carry): that landing's id. Its ticket shows that one's way.
    carried_by: Mapped[int | None] = mapped_column(Integer, index=True)


class Train(Timestamps, Base):
    """Finished branches of one repository that are checked together. One train at a time per
    repository: however fast the team works, check runs follow the clock, not the number of changes."""

    __tablename__ = "trains"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(20))  # command: the repository's check command | github: a pull request's checks
    status: Mapped[str] = mapped_column(String(20), default=TrainStatus.CHECKING, index=True)
    branch: Mapped[str] = mapped_column(String(250), default="")
    base: Mapped[str] = mapped_column(String(250), default="")
    head_sha: Mapped[str] = mapped_column(String(64), default="")
    number: Mapped[int | None] = mapped_column(Integer)  # github: its pull request
    url: Mapped[str] = mapped_column(String(500), default="")
    job_id: Mapped[str | None] = mapped_column(String(32))  # command: the runner job that runs the check
    group_no: Mapped[int | None] = mapped_column(Integer)  # the repaired branches of a failed train (see Landing.group_no)
    size: Mapped[int] = mapped_column(Integer, default=0)  # branches on it
    check_seconds: Mapped[int] = mapped_column(Integer, default=0)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Its branches were pushed to the base branch before the checks (Repository.land_first): the
    # checks are those of the base branch at head_sha, and their result changes nothing about what landed.
    direct: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    failed_checks: Mapped[str] = mapped_column(Text, default="", server_default="")  # names, one per line
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # the manager was told the base stays red
    rerun_at: Mapped[datetime | None] = mapped_column(DateTime)  # a look at the base branch (size 0): its failed jobs were run once more


class FlowEvent(Base):
    """Something PlainBoard did by itself, as it happened (services/flow_log.py). The tables above
    keep the latest state; this keeps the course, for the work log. Nothing reads it at run time."""

    __tablename__ = "flow_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(40))  # e.g. train.failed, landing.back, wait.ended
    # What it is about. Plain numbers, no foreign keys: the log outlives what it describes.
    agent_id: Mapped[int | None] = mapped_column(Integer)
    run_id: Mapped[int | None] = mapped_column(Integer)
    assignment_id: Mapped[int | None] = mapped_column(Integer)
    task_id: Mapped[int | None] = mapped_column(Integer)
    landing_id: Mapped[int | None] = mapped_column(Integer)
    train_id: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --- conversations & runs -------------------------------------------------------------------


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), unique=True)
    provider_session_id: Mapped[str | None] = mapped_column(String(200))
    context_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Context size at the first model call of the current session (system prompt + tools + memory):
    # compression cannot go below this, so it is only triggered by growth beyond it.
    session_base_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # System prompt frozen at session start (keeps the prompt cache valid) and the latest
    # briefing sections the agent has seen (changes are sent as an update in the next turn).
    # session_sections are the sections of the frozen prompt: if the provider compacts its session,
    # earlier updates may be gone, so the agent's knowledge falls back to them.
    system_prompt: Mapped[str | None] = mapped_column(Text)
    prompt_sections: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    session_sections: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # The task list as last sent in this session: it is only sent again when it changed.
    todos_shown: Mapped[str | None] = mapped_column(Text)
    account_key: Mapped[str | None] = mapped_column(String(40))  # the provider account of the last run
    # What the current session is about (e.g. "task:12"). A member's assignment for something else
    # starts a new session: context is per piece of work, not per agent.
    session_topic: Mapped[str | None] = mapped_column(String(40))
    summary: Mapped[str | None] = mapped_column(Text)
    summary_until_message_id: Mapped[int] = mapped_column(Integer, default=0)
    compressions: Mapped[int] = mapped_column(Integer, default=0)
    total_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    session_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)  # provider's running total for the current session
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10))  # user | assistant | system
    kind: Mapped[str] = mapped_column(String(10), default="text")  # text | tool | summary | event | error
    content: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @validates("content")
    def _no_nul(self, _key: str, value: str) -> str:
        return value.replace("\x00", "") if value and "\x00" in value else value  # PostgreSQL text can't hold NUL (#120); meta: db._json


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_agent_id_status", "agent_id", "status"),
        Index("ix_runs_agent_id_finished_at", "agent_id", "finished_at"),
        Index("ix_runs_project_id_created_at", "project_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.QUEUED, index=True)
    input_text: Mapped[str] = mapped_column(Text, default="")
    effort: Mapped[str | None] = mapped_column(String(20))  # per-run override of the agent's effort
    attachment_ids: Mapped[list[int]] = mapped_column(JSON, default=list)  # files sent with a chat message
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True)
    assignment_id: Mapped[int | None] = mapped_column(ForeignKey("assignments.id", ondelete="SET NULL"), index=True)
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("schedules.id", ondelete="SET NULL"))
    source_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    parent_run_id: Mapped[int | None] = mapped_column(Integer)
    author_name: Mapped[str | None] = mapped_column(String(100))  # a chat message: the person who wrote it
    # A chat run: the messages waiting for it, each with its writer, text, files and time
    # ({id, author, text, named, attachment_ids, at}). None: an older run, one message.
    parts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    result_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    num_turns: Mapped[int] = mapped_column(Integer, default=0)
    context_tokens: Mapped[int] = mapped_column(Integer, default=0)
    job_wait_seconds: Mapped[int] = mapped_column(Integer, default=0)  # part of the turn spent waiting for jobs
    account_key: Mapped[str | None] = mapped_column(String(40))  # the provider account it ran on (the prompt cache is per account)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    stop_requested_at: Mapped[datetime | None] = mapped_column(DateTime)  # told to wrap up
    # A chat message that waits as this turn: when its busy agent read it, if it did before the turn
    # started. The chat keeps it at the bottom until then and shows it at that time afterwards.
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


class Job(Base):
    """A shell command running in the runner independently of the turn that started it."""

    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    # The assignment that waits for this job's result and stays open until it ends. None: a server
    # or a job started outside an assignment.
    assignment_id: Mapped[int | None] = mapped_column(ForeignKey("assignments.id", ondelete="SET NULL"), index=True)
    runner_id: Mapped[str] = mapped_column(String(32), unique=True)
    command: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.RUNNING, index=True)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    log_path: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class PreviewShare(Base):
    """Who may open /preview/<port>/ without the PlainBoard login, until it expires. No row: nobody."""

    __tablename__ = "preview_shares"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    port: Mapped[int] = mapped_column(Integer, unique=True)  # ports are shared by all projects
    access: Mapped[str] = mapped_column(String(20))  # public | password
    password_hash: Mapped[str | None] = mapped_column(String(200))
    created_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Mention(Base):
    """A person named with @username in a chat message, comment or decision of a project they have
    access to (services/mentions.py). Theirs alone: unread until they open it."""

    __tablename__ = "mentions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # chat | comment | decision
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))  # the chat it is in
    author: Mapped[str] = mapped_column(String(100), default="")
    excerpt: Mapped[str] = mapped_column(Text, default="")
    read_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class InboxItem(Base):
    """Updates for an agent, prepended to its next turn (cheaper than an extra run)."""

    __tablename__ = "inbox_items"
    __table_args__ = (Index("ix_inbox_items_agent_id_consumed_at", "agent_id", "consumed_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- knowledge, automation, integrations ----------------------------------------------------


class Note(Timestamps, Base):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text, default="")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)  # pinned notes are in every agent's prompt
    created_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))


class AgentNote(Timestamps, Base):
    """A team member's own note: always in that member's prompt, so it survives context resets
    (services/own_notes.py keeps all notes of a member within a small budget)."""

    __tablename__ = "agent_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))  # None = the user
    review_asked_at: Mapped[datetime | None] = mapped_column(DateTime)  # last time its member was asked to check it


class Todo(Timestamps, Base):
    """An item on a team member's task list. Done items move to the archive and are later deleted
    (services/todos.py), so agents only ever see the open ones."""

    __tablename__ = "todos"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    done_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))  # None = the user


class Schedule(Timestamps, Base):
    __tablename__ = "schedules"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(10))  # once | cron
    run_at: Mapped[datetime | None] = mapped_column(DateTime)  # UTC, for "once"
    cron: Mapped[str | None] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_run_id: Mapped[int | None] = mapped_column(Integer)
    # A run is left out when nothing happened in the project since the last one (schedules.quiet):
    # a status report that would say "nothing new" costs a turn for nothing.
    skip_quiet: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))


class Secret(Timestamps, Base):
    """An environment variable for agents (e.g. a CLI token). Encrypted; None project = all projects."""

    __tablename__ = "secrets"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    value_enc: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(String(300), default="")


class Skill(Timestamps, Base):
    """A Claude skill of a project (services/skills.py): its files as a zip, laid into the agents' workspaces."""

    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(1024), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    all_agents: Mapped[bool] = mapped_column(Boolean, default=True)
    agent_ids: Mapped[list[int]] = mapped_column(JSON, default=list)  # without all_agents: these agents only
    source: Mapped[str] = mapped_column(String(10), default="upload")  # upload | git
    source_url: Mapped[str] = mapped_column(String(500), default="")
    source_ref: Mapped[str] = mapped_column(String(200), default="")
    source_path: Mapped[str] = mapped_column(String(300), default="")
    archive: Mapped[str] = mapped_column(Text, default="")  # base64 of the zip of its files
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)  # bytes of its files, unpacked
    file_count: Mapped[int] = mapped_column(Integer, default=0)


class McpServer(Timestamps, Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)  # None = global
    name: Mapped[str] = mapped_column(String(100))
    transport: Mapped[str] = mapped_column(String(10))  # stdio | http | sse
    command: Mapped[str | None] = mapped_column(String(500))
    args: Mapped[list[str]] = mapped_column(JSON, default=list)
    url: Mapped[str | None] = mapped_column(String(500))
    env_enc: Mapped[str | None] = mapped_column(Text)  # encrypted JSON object
    headers_enc: Mapped[str | None] = mapped_column(Text)  # encrypted JSON object
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    all_agents: Mapped[bool] = mapped_column(Boolean, default=True)


# --- semantic search (services/search): PostgreSQL with pgvector only ----------------------------


class Vector(UserDefinedType):
    """pgvector's ``vector``, without a fixed dimension: each row names the model that made it."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "vector"

    def bind_processor(self, dialect: Any):
        return lambda value: None if value is None else "[" + ",".join(f"{float(x):.6g}" for x in value) + "]"

    def bind_expression(self, bindvalue: Any):
        return cast(bindvalue, self)

    def result_processor(self, dialect: Any, coltype: Any):
        return lambda value: None if value is None else [float(x) for x in value.strip("[]").split(",") if x]


# Tables that need the extension: never made by ``create_all`` with the others, only by ``search.schema.ensure``.
NEEDS_VECTOR = {"info": {"needs": "vector"}}


class SearchDocument(Base):
    """One chunk of something in a project's history, prepared for retrieval (services/search/documents.py):
    a header that makes it self-contained, the cleaned text and its hash (unchanged text is never embedded
    again); its vectors are in ``SearchVector``. Derived data: made again from the sources at any time."""

    __tablename__ = "search_documents"
    __table_args__ = (
        UniqueConstraint("project_id", "kind", "source_id", "chunk"),
        Index("ix_search_documents_scope", "project_id", "kind", "at"),
        Index("ix_search_documents_tsv", "tsv", postgresql_using="gin"),
        NEEDS_VECTOR,
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))  # an indexer's kind: ticket, report, decision, comment, note, commit
    source_id: Mapped[str] = mapped_column(String(100))  # the item within its kind (a row id, repository:sha)
    chunk: Mapped[int] = mapped_column(Integer, default=0)
    ref: Mapped[str] = mapped_column(String(120))  # how an agent names it: #64, assignment #5043, 1a2b3c4d5e
    title: Mapped[str] = mapped_column(String(300), default="")
    header: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime)  # when the item was made (a ticket, a commit)
    changed_at: Mapped[datetime] = mapped_column(DateTime)  # when it last changed at its source
    ticket_number: Mapped[int | None] = mapped_column(Integer)
    repo: Mapped[str | None] = mapped_column(String(100))
    paths: Mapped[list[str] | None] = mapped_column(JSON)  # a commit's first changed files: whether they still exist is said with a hit
    content_hash: Mapped[str] = mapped_column(String(64))
    # Where it comes from: deleting the item, or what holds it (its ticket, the agent of a report, the
    # project, a repository), deletes its chunks and their vectors with it, in the same transaction.
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    assignment_id: Mapped[int | None] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), index=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id", ondelete="CASCADE"), index=True)
    comment_id: Mapped[int | None] = mapped_column(ForeignKey("comments.id", ondelete="CASCADE"), index=True)
    note_id: Mapped[int | None] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[int | None] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    # A doc page: its foreign key is added by search.schema.ensure once doc_pages exists (migration 0073 makes this
    # table from today's models, before 0088 made doc_pages).
    doc_page_id: Mapped[int | None] = mapped_column(Integer, index=True)
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed("to_tsvector('simple', header || ' ' || body)", persisted=True), deferred=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SearchVector(Base):
    """A chunk's vector by one model. Two models side by side while the index is made anew for another one:
    search keeps using the current model's until the new one has every chunk, then switches at once."""

    __tablename__ = "search_vectors"
    __table_args__ = (Index("ix_search_vectors_scope", "project_id", "model"), NEEDS_VECTOR)
    document_id: Mapped[int] = mapped_column(ForeignKey("search_documents.id", ondelete="CASCADE"), primary_key=True)
    model: Mapped[str] = mapped_column(String(100), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    embedding: Mapped[list[float]] = mapped_column(Vector())


class SearchCursor(Base):
    """How far an indexer has read a project's source: the last item's stamp and id, or a repository's last commit."""

    __tablename__ = "search_cursors"
    __table_args__ = (NEEDS_VECTOR,)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    source: Mapped[str] = mapped_column(String(60), primary_key=True)  # an indexer's kind, commit:<repository id>, sweep
    value: Mapped[str] = mapped_column(String(200), default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
