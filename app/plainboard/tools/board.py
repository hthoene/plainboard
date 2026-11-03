"""Ticket tools. Status changes are reserved for the manager (it owns ticket flow)."""

from __future__ import annotations

from sqlalchemy import func, select
from pydantic import BaseModel, Field

from ..models import Assignment, Task
from ..schemas import CUSTOMER_DESCRIPTION_MAX, CUSTOMER_TITLE_MAX, USER_ACTION_MAX, DecisionOption, LabelColor, Priority, Status, TaskCreate, TaskUpdate
from ..services import board as board_service
from ..services import decisions as decision_service
from ..services import projects as project_service
from ..services.util import truncate
from .registry import ALL, MANAGER, ToolContext, ToolError, local_tool, tool


class ListTasksArgs(BaseModel):
    status: Status | None = None
    priority: Priority | None = None
    label: str | None = None
    query: str | None = Field(default=None, description="Text search in title/description")
    archived: bool = Field(default=False, description="true = only archived tickets (off the board)")
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


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


class CreateTasksArgs(BaseModel):
    tickets: list[CreateTaskArgs] = Field(min_length=1, max_length=50)


class TicketNumbers(BaseModel):
    numbers: list[int] = Field(min_length=1, max_length=200, description="Ticket numbers")


class UpdateTasksArgs(TicketNumbers):
    status: Status | None = None
    priority: Priority | None = None
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)
    archived: bool | None = Field(default=None, description="true = take off the board, false = restore")


class UpdateTaskArgs(BaseModel):
    number: int
    title: str | None = None
    description: str | None = None
    customer_title: str | None = Field(default=None, max_length=CUSTOMER_TITLE_MAX, description="Public, for end users: says clearly what this is, simple words; empty = hidden")
    customer_description: str | None = Field(default=None, max_length=CUSTOMER_DESCRIPTION_MAX, description="Public, for end users: 1-2 short sentences on what they get")
    status: Status | None = None
    priority: Priority | None = None
    labels: list[str] | None = None
    user_action: str | None = Field(default=None, max_length=USER_ACTION_MAX, description="What only the user can do (e.g. test on a phone); highlights the ticket until done; empty = clear")


class SortTicketsArgs(BaseModel):
    status: Status
    numbers: list[int] = Field(min_length=1, description="Ticket numbers, top first; other tickets of the column follow below")


class ColorLabelArgs(BaseModel):
    label: str
    color: LabelColor | None = Field(default=None, description="Empty = default color")


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
    waiting = f" (user to do: {task.user_action})" if task.user_action else ""
    return f"#{task.number} [{task.status}{', archived' if task.archived_at else ''}] ({task.priority}) {task.title}{labels}{waiting}"


def numbers_text(tasks: list[Task]) -> str:
    return ", ".join(f"#{t.number}" for t in tasks)


@tool("list_tasks", "List tickets on the board (compact). Filter by status, priority, label or text.", ListTasksArgs)
async def list_tasks(ctx: ToolContext, args: ListTasksArgs) -> str:
    tasks = await board_service.list_tasks(
        ctx.session, ctx.project.id, status=args.status, priority=args.priority, label=args.label, query=args.query, archived=args.archived
    )
    page = tasks[args.offset : args.offset + args.limit]
    if not page:
        return f"No tickets at offset {args.offset} ({len(tasks)} match)." if tasks else "No tickets."
    text = "\n".join(ticket_line(t) for t in page)
    if len(page) < len(tasks):
        text += f"\n(showing {args.offset + 1}-{args.offset + len(page)} of {len(tasks)})"
    return text


@tool("get_board_summary", "Ticket counts per status plus all open tickets.")
async def get_board_summary(ctx: ToolContext, _args) -> str:
    on_board = select(Task.status, func.count()).where(Task.project_id == ctx.project.id, Task.archived_at.is_(None)).group_by(Task.status)
    counts = ", ".join(f"{status}: {n}" for status, n in (await ctx.session.execute(on_board)).all()) or "empty board"
    archived = await ctx.session.scalar(select(func.count()).select_from(Task).where(Task.project_id == ctx.project.id, Task.archived_at.is_not(None)))
    counts += f", archived: {archived}" if archived else ""
    open_tasks = [t for t in await board_service.list_tasks(ctx.session, ctx.project.id) if t.status != "done"]
    colors = ", ".join(f"{label}={color}" for label, color in ctx.project.label_colors.items())
    more = f"\n(+{len(open_tasks) - 40} more open: list_tasks)" if len(open_tasks) > 40 else ""
    return f"Counts: {counts}\n" + (f"Label colors: {colors}\n" if colors else "") + "\n".join(ticket_line(t) for t in open_tasks[:40]) + more


@tool("get_task", "Full ticket: description, attachments (paths), recent comments, assignments.", TicketRef)
async def get_task(ctx: ToolContext, args: TicketRef) -> str:
    task = await board_service.get_task_by_number(ctx.session, ctx.project.id, args.number)
    text = await board_service.ticket_text(ctx.session, task, comment_limit=10)
    assignments = (await ctx.session.scalars(select(Assignment).where(Assignment.task_id == task.id).order_by(Assignment.id))).all()
    if assignments:
        text += "\n\nAssignments:\n" + "\n".join(f"- #{a.id} [{a.status}] agent {a.agent_id}: {a.title}" + (f" → {truncate(a.result, 200)}" if a.result else "") for a in assignments)
    return text


@tool("create_tasks", "Create one or more tickets (e.g. follow-ups you discovered). They land in backlog unless status given.", CreateTasksArgs)
async def create_tasks(ctx: ToolContext, args: CreateTasksArgs) -> str:
    items = [TaskCreate(**t.model_dump()) for t in args.tickets]
    tasks = await board_service.create_tasks(ctx.session, ctx.project.id, items, by_agent_id=ctx.agent.id)
    return f"Created {numbers_text(tasks)}."


@tool("update_task", "Update ticket fields or move it to another status.", UpdateTaskArgs, roles=(MANAGER,))
async def update_task(ctx: ToolContext, args: UpdateTaskArgs) -> str:
    task = await board_service.get_task_by_number(ctx.session, ctx.project.id, args.number)
    fields = args.model_dump(exclude_unset=True, exclude={"number"})
    task, _ = await board_service.update_task(ctx.session, task, TaskUpdate(**fields))
    return f"Updated: {ticket_line(task)}"


@tool("update_tasks", "Apply the same change to many tickets at once: move, set priority, add/remove labels, archive.", UpdateTasksArgs, roles=(MANAGER,))
async def update_tasks(ctx: ToolContext, args: UpdateTasksArgs) -> str:
    fields = args.model_dump(exclude_unset=True, exclude={"numbers", "add_labels", "remove_labels"})
    if not fields and not args.add_labels and not args.remove_labels:
        raise ToolError("Nothing to change.")
    tasks, missing = await board_service.update_tasks(
        ctx.session, ctx.project.id, args.numbers, TaskUpdate(**fields), add_labels=args.add_labels, remove_labels=args.remove_labels
    )
    text = f"Updated {numbers_text(tasks)}." if tasks else "Nothing updated."
    return text + (f" Not found: {', '.join(f'#{n}' for n in missing)}." if missing else "")


@tool("sort_tickets", "Set the order of tickets within a board column.", SortTicketsArgs, roles=(MANAGER,))
async def sort_tickets(ctx: ToolContext, args: SortTicketsArgs) -> str:
    await board_service.sort_column(ctx.session, ctx.project.id, args.status, args.numbers)
    return f"Sorted {args.status}."


@tool("color_label", "Give a label one color across the board.", ColorLabelArgs, roles=(MANAGER,))
async def color_label(ctx: ToolContext, args: ColorLabelArgs) -> str:
    await project_service.set_label_color(ctx.session, ctx.project, args.label, args.color)
    return f"Label {args.label}: {args.color or 'default'}."


@tool("delete_tasks", "Delete tickets permanently.", TicketNumbers, roles=(MANAGER,))
async def delete_tasks(ctx: ToolContext, args: TicketNumbers) -> str:
    tasks = await board_service.get_tasks_by_number(ctx.session, ctx.project.id, args.numbers)
    text = numbers_text(tasks)
    await board_service.delete_tasks(ctx.session, tasks)
    return f"Deleted {text}."


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
