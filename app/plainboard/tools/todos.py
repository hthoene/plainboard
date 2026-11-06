"""Task lists: the manager plans work per team member; members see and finish their own items."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import Agent, Todo
from ..schemas import TodoCreate, TodoState, TodoUpdate
from ..services import agents as agent_service
from ..services import todos as todo_service
from ..services.util import truncate
from .registry import MANAGER, ToolContext, ToolError, tool


class ListTodosArgs(BaseModel):
    agent: str | None = Field(default=None, description="Manager: whose list (slug/name/id); empty = everyone. Others: always their own")
    state: TodoState = Field(default="open", description="done = finished in the last 7 days; archived = older")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class TodoRef(BaseModel):
    todo_id: int


class AddTodosArgs(BaseModel):
    agent: str | None = Field(default=None, description="Manager: whose list (slug/name/id). Others: always their own")
    todos: list[TodoCreate] = Field(min_length=1, max_length=50)


class UpdateTodoArgs(BaseModel):
    todo_id: int
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    done: bool | None = Field(default=None, description="true = done, false = open again")
    agent: str | None = Field(default=None, description="Manager only: move it to another member's list")


class TodoIds(BaseModel):
    todo_ids: list[int] = Field(min_length=1)


def _line(todo: Todo, owner: str | None = None) -> str:
    who = f" [{owner}]" if owner else ""
    done = f" (done {todo.done_at:%Y-%m-%d})" if todo.done_at else ""
    details = " +details" if todo.description.strip() else ""
    return f"- {todo.id}{who}: {truncate(todo.title, 120)}{done}{details}"


async def _target(ctx: ToolContext, ref: str | None) -> Agent | None:
    """The list a call works on: workers always their own; the manager the named one (None = all)."""
    if not ctx.agent.is_manager:
        return ctx.agent
    return await agent_service.resolve(ctx.session, ctx.project.id, ref) if ref else None


async def _todo(ctx: ToolContext, todo_id: int) -> Todo:
    todo = await todo_service.get(ctx.session, todo_id, ctx.project.id)
    if not ctx.agent.is_manager and todo.agent_id != ctx.agent.id:
        raise ToolError(f"Task {todo_id} is not on your list.")
    return todo


@tool("list_todos", "Task list of a team member (open items by default), paged.", ListTodosArgs)
async def list_todos(ctx: ToolContext, args: ListTodosArgs) -> str:
    agent = await _target(ctx, args.agent)
    items, total = await todo_service.page(
        ctx.session, ctx.project.id, agent_id=agent.id if agent else None, state=args.state, limit=args.limit, offset=args.offset
    )
    whose = "your" if agent is ctx.agent else (agent.slug if agent else "everyone's")
    if not items:
        return f"No {args.state} tasks on {whose} list" + (f" at offset {args.offset} ({total} total)." if total else ".")
    names = {} if agent else {a.id: a.slug for a in await agent_service.list_for_project(ctx.session, ctx.project.id)}
    head = f"{args.state.capitalize()} tasks on {whose} list: {args.offset + 1}-{args.offset + len(items)} of {total}"
    if args.offset + len(items) < total:
        head += f" (more: offset={args.offset + len(items)})"
    return head + "\n" + "\n".join(_line(t, names.get(t.agent_id)) for t in items)


@tool("get_todo", "One task with its full description.", TodoRef)
async def get_todo(ctx: ToolContext, args: TodoRef) -> str:
    todo = await _todo(ctx, args.todo_id)
    owner = await agent_service.get(ctx.session, todo.agent_id)
    state = f"done {todo.done_at:%Y-%m-%d}" if todo.done_at else "open"
    return f"Task {todo.id} for {owner.slug} [{state}]: {todo.title}" + (f"\n\n{todo.description.strip()}" if todo.description.strip() else "")


@tool("add_todos", "Add tasks (title + detailed description) to a team member's list.", AddTodosArgs)
async def add_todos(ctx: ToolContext, args: AddTodosArgs) -> str:
    agent = await _target(ctx, args.agent)
    if agent is None:
        raise ToolError("Name the team member (agent).")
    todos = await todo_service.create(ctx.session, agent, args.todos, by_agent_id=ctx.agent.id)
    return f"Added {', '.join(str(t.id) for t in todos)} to {agent.slug}'s list."


@tool("update_todo", "Edit a task, mark it done (or open again), or move it to another member.", UpdateTodoArgs)
async def update_todo(ctx: ToolContext, args: UpdateTodoArgs) -> str:
    todo = await _todo(ctx, args.todo_id)
    fields = args.model_dump(exclude_unset=True, exclude={"todo_id", "agent"})
    if args.agent:
        if not ctx.agent.is_manager:
            raise ToolError("Only the manager moves tasks between lists.")
        fields["agent_id"] = (await agent_service.resolve(ctx.session, ctx.project.id, args.agent)).id
    todo = await todo_service.update(ctx.session, todo, TodoUpdate(**fields))
    return f"Task {todo.id} {'done' if todo.done_at else 'updated'}."


@tool("delete_todos", "Delete tasks for good (done ones are archived and deleted automatically).", TodoIds, roles=(MANAGER,))
async def delete_todos(ctx: ToolContext, args: TodoIds) -> str:
    todos = [await _todo(ctx, i) for i in args.todo_ids]
    await todo_service.delete(ctx.session, todos)
    return f"Deleted {len(todos)} task(s)."
