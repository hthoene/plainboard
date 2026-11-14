"""Task lists and own notes of team members."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..schemas import AgentNoteBody, AgentNoteOut, TodoCreate, TodoOut, TodoPage, TodoState, TodoUpdate
from ..services import agents as agent_service
from ..services import own_notes as own_note_service
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


@router.get("/agents/{agent_id}/notes", response_model=list[AgentNoteOut])
async def list_own_notes(agent_id: int, session: Session) -> list[AgentNoteOut]:
    agent = await agent_service.get(session, agent_id)
    return [AgentNoteOut.model_validate(n) for n in await own_note_service.for_agent(session, agent.id)]


@router.post("/agents/{agent_id}/notes", response_model=AgentNoteOut, status_code=201)
async def create_own_note(agent_id: int, body: AgentNoteBody, session: Session) -> AgentNoteOut:
    agent = await agent_service.get(session, agent_id)
    return AgentNoteOut.model_validate(await own_note_service.save(session, agent, body.text))


@router.patch("/agent-notes/{note_id}", response_model=AgentNoteOut)
async def update_own_note(note_id: int, body: AgentNoteBody, session: Session) -> AgentNoteOut:
    note = await own_note_service.get(session, note_id)
    agent = await agent_service.get(session, note.agent_id)
    return AgentNoteOut.model_validate(await own_note_service.save(session, agent, body.text, note=note))


@router.delete("/agent-notes/{note_id}", status_code=204)
async def delete_own_note(note_id: int, session: Session) -> None:
    await own_note_service.delete(session, [await own_note_service.get(session, note_id)])


@router.delete("/todos/{todo_id}", status_code=204)
async def delete_todo(todo_id: int, session: Session) -> None:
    await todo_service.delete(session, [await todo_service.get(session, todo_id)])
