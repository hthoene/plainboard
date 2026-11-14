"""A team member's own notes. They are always in that member's prompt, so they survive context
resets and compression; a fixed budget for all notes of a member keeps that prompt small."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Agent, AgentNote
from ..schemas import AgentNoteOut
from .errors import NotFound, ServiceError

BUDGET_CHARS = 3000  # all notes of one member together (~750 tokens)


async def for_agent(session: AsyncSession, agent_id: int) -> list[AgentNote]:
    return list((await session.scalars(select(AgentNote).where(AgentNote.agent_id == agent_id).order_by(AgentNote.id))).all())


def used(notes: list[AgentNote]) -> int:
    return sum(len(n.text) for n in notes)


async def get(session: AsyncSession, note_id: int, agent_id: int | None = None) -> AgentNote:
    note = await session.get(AgentNote, note_id)
    if note is None or (agent_id is not None and note.agent_id != agent_id):
        raise NotFound(f"Own note {note_id} not found")
    return note


async def save(session: AsyncSession, agent: Agent, text: str, *, note: AgentNote | None = None, by_agent_id: int | None = None) -> AgentNote:
    """Adds a note, or replaces the text of ``note``."""
    text = text.strip()
    if not text:
        raise ServiceError("The note is empty")
    others = used([n for n in await for_agent(session, agent.id) if note is None or n.id != note.id])
    if others + len(text) > BUDGET_CHARS:
        raise ServiceError(
            f"Own notes are limited to {BUDGET_CHARS} characters in total ({others} used, this one has {len(text)}). "
            "Shorten or delete notes first."
        )
    if note is None:
        note = AgentNote(project_id=agent.project_id, agent_id=agent.id, text=text, created_by_agent_id=by_agent_id)
        session.add(note)
    else:
        note.text = text
    await session.commit()
    bus.publish(note.project_id, "ownnote.upserted", AgentNoteOut.model_validate(note))
    return note


async def delete(session: AsyncSession, notes: list[AgentNote]) -> None:
    if not notes:
        return
    project_id, ids = notes[0].project_id, [n.id for n in notes]
    for note in notes:
        await session.delete(note)
    await session.commit()
    bus.publish(project_id, "ownnote.deleted", {"ids": ids})


async def counts(session: AsyncSession, agent_ids: list[int]) -> dict[int, int]:
    if not agent_ids:
        return {}
    q = select(AgentNote.agent_id, func.count()).where(AgentNote.agent_id.in_(agent_ids)).group_by(AgentNote.agent_id)
    return dict((await session.execute(q)).all())
