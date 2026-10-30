"""ORM models. All timestamps are naive UTC (serialized with a trailing Z by the schemas)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Table, Text, UniqueConstraint
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


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    LOST = "lost"  # the runner restarted while it was running


ACTIVE_RUN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)


# --- settings -------------------------------------------------------------------------------


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    secret: Mapped[bool] = mapped_column(Boolean, default=False)


# --- projects & agents ----------------------------------------------------------------------


class Project(Timestamps, Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # Optional shared working directory (relative to the workspaces root, e.g. a bind-mounted
    # host folder). Unset = every agent gets its own directory/clone.
    workspace_path: Mapped[str | None] = mapped_column(String(500))
    # Maintained by the manager: installs what this project needs, so it survives container rebuilds.
    setup_script: Mapped[str] = mapped_column(Text, default="")
    github_token_enc: Mapped[str | None] = mapped_column(Text)
    public_api_key_hash: Mapped[str | None] = mapped_column(String(64), unique=True)  # read-only access to customer notes
    auto_wake_manager: Mapped[bool] = mapped_column(Boolean, default=True)
    task_counter: Mapped[int] = mapped_column(Integer, default=0)

    agents: Mapped[list[Agent]] = relationship(back_populates="project", cascade="all, delete-orphan", passive_deletes=True)


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
    context_limit_tokens: Mapped[int] = mapped_column(Integer, default=400000)
    keep_recent_messages: Mapped[int] = mapped_column(Integer, default=6)
    summary_model: Mapped[str] = mapped_column(String(100), default="claude-haiku-5-5")
    max_turns: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    color: Mapped[str] = mapped_column(String(20), default="#6366f1")
    all_repos: Mapped[bool] = mapped_column(Boolean, default=True)  # False = only `repositories`

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
    created_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    author_kind: Mapped[str] = mapped_column(String(10), default="user")  # user | agent | system
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
    result_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    num_turns: Mapped[int] = mapped_column(Integer, default=0)
    context_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    stop_requested_at: Mapped[datetime | None] = mapped_column(DateTime)  # told to wrap up


class Job(Base):
    """A shell command running in the runner independently of the turn that started it."""

    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    runner_id: Mapped[str] = mapped_column(String(32), unique=True)
    command: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.RUNNING, index=True)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    log_path: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


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
    created_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))


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
