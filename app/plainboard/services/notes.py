from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Note
from ..schemas import NoteCreate, NoteOut, NoteUpdate
from .errors import NotFound


TIDY_EVERY = 30  # remind the manager to consolidate notes at 30, 60, 90 ... notes


async def create(session: AsyncSession, project_id: int, data: NoteCreate, *, by_agent_id: int | None = None) -> Note:
    note = Note(project_id=project_id, created_by_agent_id=by_agent_id, **data.model_dump())
    session.add(note)
    await session.commit()
    bus.publish(project_id, "note.upserted", NoteOut.model_validate(note))
    count = await session.scalar(select(func.count(Note.id)).where(Note.project_id == project_id)) or 0
    if count % TIDY_EVERY == 0:
        await _remind_manager(session, project_id, count)
    return note


async def _remind_manager(session: AsyncSession, project_id: int, count: int) -> None:
    from ..orchestration import inbox
    from . import agents as agent_service

    manager = await agent_service.get_manager(session, project_id)
    await inbox.push(
        session,
        manager.id,
        f"The project now has {count} notes. When convenient, review them: merge overlapping notes, "
        "rewrite outdated ones and delete what is obsolete.",
    )


async def update(session: AsyncSession, note: Note, data: NoteUpdate) -> Note:
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(note, key, value)
    await session.commit()
    bus.publish(note.project_id, "note.upserted", NoteOut.model_validate(note))
    return note


async def delete(session: AsyncSession, note: Note) -> None:
    project_id, note_id = note.project_id, note.id
    await session.delete(note)
    await session.commit()
    bus.publish(project_id, "note.deleted", {"id": note_id})


async def get(session: AsyncSession, note_id: int, project_id: int | None = None) -> Note:
    note = await session.get(Note, note_id)
    if note is None or (project_id is not None and note.project_id != project_id):
        raise NotFound(f"Note {note_id} not found")
    return note


async def list_for_project(session: AsyncSession, project_id: int, query: str | None = None) -> list[Note]:
    q = select(Note).where(Note.project_id == project_id)
    if query:
        like = f"%{query}%"
        q = q.where(or_(Note.title.ilike(like), Note.content.ilike(like)))
    return list((await session.scalars(q.order_by(Note.pinned.desc(), Note.updated_at.desc()))).all())


async def pinned(session: AsyncSession, project_id: int) -> list[Note]:
    q = select(Note).where(Note.project_id == project_id, Note.pinned.is_(True)).order_by(Note.id)
    return list((await session.scalars(q)).all())
