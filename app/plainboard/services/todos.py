"""Task lists of team members (todos). Built to stay small for agents however long a list gets:
agents see open items only, done items move to the archive after ARCHIVE_AFTER_DAYS and are deleted
after DELETE_AFTER_DAYS."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Agent, Todo, utcnow
from ..schemas import TodoCreate, TodoOut, TodoState, TodoUpdate
from .errors import NotFound, ServiceError

ARCHIVE_AFTER_DAYS = 7
DELETE_AFTER_DAYS = 90


def _archive_cutoff() -> datetime:
    return utcnow() - timedelta(days=ARCHIVE_AFTER_DAYS)


def _in_state(q, state: TodoState):
    if state == "open":
        return q.where(Todo.done_at.is_(None))
    if state == "done":
        return q.where(Todo.done_at >= _archive_cutoff())
    return q.where(Todo.done_at < _archive_cutoff())


def _publish(todo: Todo) -> None:
    bus.publish(todo.project_id, "todo.upserted", TodoOut.model_validate(todo))


async def create(session: AsyncSession, agent: Agent, items: list[TodoCreate], *, by_agent_id: int | None = None) -> list[Todo]:
    todos = [Todo(project_id=agent.project_id, agent_id=agent.id, created_by_agent_id=by_agent_id, **item.model_dump()) for item in items]
    session.add_all(todos)
    await session.commit()
    for todo in todos:
        _publish(todo)
    return todos


async def get(session: AsyncSession, todo_id: int, project_id: int | None = None) -> Todo:
    todo = await session.get(Todo, todo_id)
    if todo is None or (project_id is not None and todo.project_id != project_id):
        raise NotFound(f"Task {todo_id} not found")
    return todo


async def update(session: AsyncSession, todo: Todo, data: TodoUpdate) -> Todo:
    fields = data.model_dump(exclude_unset=True)
    if "done" in fields:
        done = fields.pop("done")
        if done and todo.done_at is None:
            todo.done_at = utcnow()
        elif done is False:
            todo.done_at = None
    if fields.get("agent_id") is not None and fields["agent_id"] != todo.agent_id:
        target = await session.get(Agent, fields["agent_id"])
        if target is None or target.project_id != todo.project_id:
            raise ServiceError("That team member is not in this project")
    for key, value in fields.items():
        if value is not None:
            setattr(todo, key, value)
    await session.commit()
    _publish(todo)
    return todo


async def delete(session: AsyncSession, todos: list[Todo]) -> None:
    if not todos:
        return
    project_id, ids = todos[0].project_id, [t.id for t in todos]
    for todo in todos:
        await session.delete(todo)
    await session.commit()
    bus.publish(project_id, "todo.deleted", {"ids": ids})


async def page(
    session: AsyncSession, project_id: int, *, agent_id: int | None = None, state: TodoState = "open", limit: int = 50, offset: int = 0
) -> tuple[list[Todo], int]:
    """One page of a list (open: oldest first; done/archived: last finished first) and its total."""
    q = _in_state(select(Todo).where(Todo.project_id == project_id), state)
    if agent_id is not None:
        q = q.where(Todo.agent_id == agent_id)
    total = await session.scalar(select(func.count()).select_from(q.subquery())) or 0
    order = (Todo.id,) if state == "open" else (Todo.done_at.desc(), Todo.id.desc())
    items = (await session.scalars(q.order_by(*order).offset(offset).limit(limit))).all()
    return list(items), total


async def open_counts(session: AsyncSession, agent_ids: list[int]) -> dict[int, int]:
    if not agent_ids:
        return {}
    q = select(Todo.agent_id, func.count()).where(Todo.agent_id.in_(agent_ids), Todo.done_at.is_(None)).group_by(Todo.agent_id)
    return dict((await session.execute(q)).all())


async def purge(session: AsyncSession) -> int:
    """Deletes items that were done more than DELETE_AFTER_DAYS ago (the scheduler calls this)."""
    cutoff = utcnow() - timedelta(days=DELETE_AFTER_DAYS)
    result = await session.execute(sql_delete(Todo).where(Todo.done_at < cutoff))
    await session.commit()
    return result.rowcount or 0
