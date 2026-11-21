"""ORM models. All timestamps are naive UTC (serialized with a trailing Z by the schemas)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Table, Text, UniqueConstraint, false, true
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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


# --- projects & agents ----------------------------------------------------------------------


class Project(Timestamps, Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # What agents write to people in, e.g. "German". Empty: the language each person writes in.
    language: Mapped[str] = mapped_column(String(50), default="", server_default="")
    # Optional shared working directory (relative to the workspaces root, e.g. a bind-mounted
    # host folder). Unset = every agent gets its own directory/clone.
    workspace_path: Mapped[str | None] = mapped_column(String(500))
    # Maintained by the manager: installs what this project needs, so it survives container rebuilds.
    setup_script: Mapped[str] = mapped_column(Text, default="")
    github_token_enc: Mapped[str | None] = mapped_column(Text)
    auto_wake_manager: Mapped[bool] = mapped_column(Boolean, default=True)
    # When nothing in To do can start, the team goes on with the Backlog. Off: Backlog waits until the user moves it.
    continue_with_backlog: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Members of this project working at the same time. Unset: the number from the settings (`max_parallel_runs`).
    parallel_agents: Mapped[int | None] = mapped_column(Integer)
    # The subscription this project's agents run on, unless an agent has its own. Unset: the one with the most room.
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    # The only accounts this project's agents may run on, set by an admin (empty: any). A project
    # shared with others does not draw on a subscription that is meant for something else.
    account_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    # The runner its agents, jobs and trains run on. Unset: the built-in one.
    runner_id: Mapped[int | None] = mapped_column(ForeignKey("runners.id", ondelete="SET NULL"))
    # Paused by a person, for good: nothing of this project starts until a person resumes it
    # (RunManager.pause_project). Not ended by a restart, nor when "pause everything" ends.
    paused_at: Mapped[datetime | None] = mapped_column(DateTime)
    paused_by: Mapped[str] = mapped_column(String(100), default="", server_default="")
    task_counter: Mapped[int] = mapped_column(Integer, default=0)
    label_colors: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)  # label name -> color name
    auto_archive_days: Mapped[int] = mapped_column(Integer, default=14)  # done tickets leave the board after this; 0 = never
    review_days: Mapped[int] = mapped_column(Integer, default=3)  # tickets left in review move to done after this; 0 = never

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
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=0)  # raised to sign the user out everywhere
    # The profile picture: a small square the browser made. Read only where it is shown (deferred);
    # the version is part of its address, so a new one is seen at once and the old one may be cached for good.
    avatar: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    avatar_type: Mapped[str | None] = mapped_column(String(30))
    avatar_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 0: none


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
    # Remove branches whose commits are all on the base branch already, once a day (orchestration/tidy.py).
    tidy_branches: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    tidied_at: Mapped[datetime | None] = mapped_column(DateTime)
    # When the manager was last told that most of this repository's check runs fail (landing._keeps_failing).
    failing_told_at: Mapped[datetime | None] = mapped_column(DateTime)
    red_told_at: Mapped[datetime | None] = mapped_column(DateTime)  # the manager was last reminded that its base branch stays red
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
    status: Mapped[str] = mapped_column(String(20), default=TaskStatus.BACKLOG)
    position: Mapped[float] = mapped_column(Float, default=0.0)
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    # What only the user can do for this ticket (e.g. test on a phone). Highlights it until done.
    user_action: Mapped[str] = mapped_column(String(500), default="")
    # Archived tickets are off the board but keep their history and links.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))


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
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(150))
    size: Mapped[int] = mapped_column(Integer)
    storage_name: Mapped[str] = mapped_column(String(100), unique=True)
    uploaded_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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
    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    session_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)  # provider's running total for the current session
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10))  # user | assistant | system
    kind: Mapped[str] = mapped_column(String(10), default="text")  # text | tool | summary | event | error
    content: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.QUEUED, index=True)
    input_text: Mapped[str] = mapped_column(Text, default="")
    effort: Mapped[str | None] = mapped_column(String(20))  # per-run override of the agent's effort
    attachment_ids: Mapped[list[int]] = mapped_column(JSON, default=list)  # files sent with a chat message
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    assignment_id: Mapped[int | None] = mapped_column(ForeignKey("assignments.id", ondelete="SET NULL"))
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("schedules.id", ondelete="SET NULL"))
    source_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    parent_run_id: Mapped[int | None] = mapped_column(Integer)
    author_name: Mapped[str | None] = mapped_column(String(100))  # a chat message: the person who wrote it
    result_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
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


class InboxItem(Base):
    """Updates for an agent, prepended to its next turn (cheaper than an extra run)."""

    __tablename__ = "inbox_items"
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
