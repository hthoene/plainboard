"""Task lists of team members."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..schemas import TodoCreate, TodoOut, TodoPage, TodoState, TodoUpdate
from ..services import agents as agent_service
from ..services import todos as todo_service
from .deps import Session

router = APIRouter(prefix="/api", tags=["todos"])


@router.get("/agents/{agent_id}/todos", response_model=TodoPage)
async def list_todos(
    agent_id: int, session: Session, state: TodoState = "open", limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0)
) -> TodoPage:
    agent = await agent_service.get(session, agent_id)
    items, total = await todo_service.page(session, agent.project_id, agent_id=agent.id, state=state, limit=limit, offset=offset)
    return TodoPage(items=[TodoOut.model_validate(t) for t in items], total=total)


@router.post("/agents/{agent_id}/todos", response_model=TodoOut, status_code=201)
async def create_todo(agent_id: int, body: TodoCreate, session: Session) -> TodoOut:
    agent = await agent_service.get(session, agent_id)
    (todo,) = await todo_service.create(session, agent, [body])
    return TodoOut.model_validate(todo)


@router.patch("/todos/{todo_id}", response_model=TodoOut)
async def update_todo(todo_id: int, body: TodoUpdate, session: Session) -> TodoOut:
    return TodoOut.model_validate(await todo_service.update(session, await todo_service.get(session, todo_id), body))


@router.delete("/todos/{todo_id}", status_code=204)
async def delete_todo(todo_id: int, session: Session) -> None:
    await todo_service.delete(session, [await todo_service.get(session, todo_id)])
