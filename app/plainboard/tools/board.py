"""Ticket tools. Status changes are reserved for the manager (it owns ticket flow)."""

from __future__ import annotations

from sqlalchemy import func, select
from pydantic import BaseModel, Field

from ..models import Assignment, Task
from ..schemas import CUSTOMER_DESCRIPTION_MAX, CUSTOMER_TITLE_MAX, DecisionOption, Priority, Status, TaskCreate, TaskUpdate
from ..services import board as board_service
from ..services import decisions as decision_service
from ..services.util import truncate
from .registry import ALL, MANAGER, ToolContext, local_tool, tool


class ListTasksArgs(BaseModel):
    status: Status | None = None
    query: str | None = Field(default=None, description="Text search in title/description")
    limit: int = Field(default=50, ge=1, le=200)


class TicketRef(BaseModel):
    number: int = Field(description="Ticket number (#)")


class CreateTaskArgs(BaseModel):
    title: str
    description: str = ""
    customer_title: str = Field(default="", max_length=CUSTOMER_TITLE_MAX, description="Public, for end users: says clearly what this is, simple words; empty = hidden")
    customer_description: str = Field(default="", max_length=CUSTOMER_DESCRIPTION_MAX, description="Public, for end users: 1-2 short sentences on what they get")
    status: Status = "backlog"
    priority: Priority = "medium"
    labels: list[str] = Field(default_factory=list)


class UpdateTaskArgs(BaseModel):
    number: int
    title: str | None = None
    description: str | None = None
    customer_title: str | None = Field(default=None, max_length=CUSTOMER_TITLE_MAX, description="Public, for end users: says clearly what this is, simple words; empty = hidden")
    customer_description: str | None = Field(default=None, max_length=CUSTOMER_DESCRIPTION_MAX, description="Public, for end users: 1-2 short sentences on what they get")
    status: Status | None = None
    priority: Priority | None = None
    labels: list[str] | None = None


class CommentArgs(BaseModel):
    number: int
    body: str = Field(description="Markdown")


class AttachFileArgs(BaseModel):
    task_number: int
    path: str = Field(description="File path, absolute or relative to your workspace")


class RequestDecisionArgs(BaseModel):
    task_number: int
    question: str = Field(description="One short question in plain words, no jargon")
    details: str = Field(default="", description="Markdown: background the user needs")
    options: list[DecisionOption] = Field(default_factory=list, max_length=8, description="Choices to click; empty = free answer only")
    attachment_ids: list[int] = Field(default_factory=list, description="Files for the question as a whole")


class ResolveDecisionArgs(BaseModel):
    decision_id: int
    option: int | None = Field(default=None, ge=1, description="Number of the chosen option")
    note: str = Field(default="", description="The user's answer in their words")
    withdraw: bool = Field(default=False, description="The question is no longer needed")


def ticket_line(task: Task) -> str:
    labels = f" {{{', '.join(task.labels)}}}" if task.labels else ""
    return f"#{task.number} [{task.status}] ({task.priority}) {task.title}{labels}"


@tool("list_tasks", "List tickets on the board (compact). Filter by status or text.", ListTasksArgs)
async def list_tasks(ctx: ToolContext, args: ListTasksArgs) -> str:
    tasks = await board_service.list_tasks(ctx.session, ctx.project.id, status=args.status, query=args.query, limit=args.limit)
    return "\n".join(ticket_line(t) for t in tasks) or "No tickets."


@tool("get_board_summary", "Ticket counts per status plus all open tickets.")
async def get_board_summary(ctx: ToolContext, _args) -> str:
    rows = (await ctx.session.execute(select(Task.status, func.count()).where(Task.project_id == ctx.project.id).group_by(Task.status))).all()
    counts = ", ".join(f"{status}: {n}" for status, n in rows) or "empty board"
    open_tasks = [t for t in await board_service.list_tasks(ctx.session, ctx.project.id) if t.status != "done"][:40]
    return f"Counts: {counts}\n" + "\n".join(ticket_line(t) for t in open_tasks)


@tool("get_task", "Full ticket: description, attachments (paths), recent comments, assignments.", TicketRef)
async def get_task(ctx: ToolContext, args: TicketRef) -> str:
    task = await board_service.get_task_by_number(ctx.session, ctx.project.id, args.number)
    text = await board_service.ticket_text(ctx.session, task, comment_limit=10)
    assignments = (await ctx.session.scalars(select(Assignment).where(Assignment.task_id == task.id).order_by(Assignment.id))).all()
    if assignments:
        text += "\n\nAssignments:\n" + "\n".join(f"- #{a.id} [{a.status}] agent {a.agent_id}: {a.title}" + (f" → {truncate(a.result, 200)}" if a.result else "") for a in assignments)
    return text


@tool("create_task", "Create a ticket (e.g. a follow-up you discovered). Lands in backlog unless status given.", CreateTaskArgs)
async def create_task(ctx: ToolContext, args: CreateTaskArgs) -> str:
    task = await board_service.create_task(ctx.session, ctx.project.id, TaskCreate(**args.model_dump()), by_agent_id=ctx.agent.id)
    return f"Created ticket #{task.number}."


@tool("update_task", "Update ticket fields or move it to another status.", UpdateTaskArgs, roles=(MANAGER,))
async def update_task(ctx: ToolContext, args: UpdateTaskArgs) -> str:
    task = await board_service.get_task_by_number(ctx.session, ctx.project.id, args.number)
    fields = args.model_dump(exclude_unset=True, exclude={"number"})
    task, _ = await board_service.update_task(ctx.session, task, TaskUpdate(**fields))
    return f"Updated: {ticket_line(task)}"


@tool("delete_task", "Delete a ticket permanently.", TicketRef, roles=(MANAGER,))
async def delete_task(ctx: ToolContext, args: TicketRef) -> str:
    task = await board_service.get_task_by_number(ctx.session, ctx.project.id, args.number)
    await board_service.delete_task(ctx.session, task)
    return f"Deleted ticket #{args.number}."


@tool("add_comment", "Comment on a ticket (visible to the user).", CommentArgs, roles=ALL)
async def add_comment(ctx: ToolContext, args: CommentArgs) -> str:
    task = await board_service.get_task_by_number(ctx.session, ctx.project.id, args.number)
    await board_service.add_comment(ctx.session, task, args.body, author_kind="agent", agent_id=ctx.agent.id)
    return f"Commented on #{args.number}."


local_tool("attach_file", "Attach a file from your workspace to a ticket (e.g. screenshot, report).", AttachFileArgs)


@tool("request_decision", "Ask the user to decide on a ticket; this marks the ticket on the board. Their answer arrives as an update.", RequestDecisionArgs, roles=(MANAGER,))
async def request_decision(ctx: ToolContext, args: RequestDecisionArgs) -> str:
    task = await board_service.get_task_by_number(ctx.session, ctx.project.id, args.task_number)
    decision = await decision_service.create(
        ctx.session, task, args.question, details=args.details, options=args.options, attachment_ids=args.attachment_ids, agent_id=ctx.agent.id
    )
    return f"Decision {decision.id} is waiting for the user on ticket #{task.number}."


@tool("resolve_decision", "Record the answer the user gave you directly, or withdraw the question.", ResolveDecisionArgs, roles=(MANAGER,))
async def resolve_decision(ctx: ToolContext, args: ResolveDecisionArgs) -> str:
    decision = await decision_service.get_in_project(ctx.session, ctx.project.id, args.decision_id)
    if args.withdraw:
        await decision_service.withdraw(ctx.session, decision)
        return f"Withdrew decision {decision.id}."
    option_index = args.option - 1 if args.option else None
    await decision_service.answer(ctx.session, decision, option_index=option_index, text=args.note, by="agent", agent_id=ctx.agent.id)
    return f"Recorded decision {decision.id}."
