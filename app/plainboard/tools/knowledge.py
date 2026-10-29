"""Project notes: shared, durable knowledge. Listing returns titles only (load content on demand)."""

from __future__ import annotations

from difflib import SequenceMatcher

from pydantic import BaseModel, Field

from ..schemas import NoteCreate, NoteUpdate
from ..services import notes as note_service
from ..models import Note, utcnow
from .registry import MANAGER, ToolContext, ToolError, tool

SIMILAR_TITLE_RATIO = 0.8


class ListNotesArgs(BaseModel):
    query: str | None = Field(default=None, description="Text search")


class NoteRef(BaseModel):
    note_id: int


class CreateNoteArgs(BaseModel):
    title: str
    content: str = Field(description="Markdown")
    pinned: bool = Field(default=False, description="Pinned notes are included in every agent's prompt; use sparingly")
    force: bool = Field(default=False, description="Create even if a note with a similar title exists")


class UpdateNoteArgs(BaseModel):
    note_id: int
    title: str | None = None
    content: str | None = None
    pinned: bool | None = None


def _age(note: Note) -> str:
    days = (utcnow() - note.updated_at).days
    return "today" if days == 0 else f"{days}d ago"


def similar(a: str, b: str) -> bool:
    a, b = a.strip().lower(), b.strip().lower()
    return a == b or a in b or b in a or SequenceMatcher(None, a, b).ratio() >= SIMILAR_TITLE_RATIO


@tool("list_notes", "List project notes (id, title, age, size). Use get_note to read one.", ListNotesArgs)
async def list_notes(ctx: ToolContext, args: ListNotesArgs) -> str:
    notes = await note_service.list_for_project(ctx.session, ctx.project.id, args.query)
    lines = [f"{n.id}: {n.title}{' (pinned)' if n.pinned else ''} · updated {_age(n)} · {len(n.content)} chars" for n in notes]
    return "\n".join(lines) or "No notes."


@tool("get_note", "Read a note.", NoteRef)
async def get_note(ctx: ToolContext, args: NoteRef) -> str:
    note = await note_service.get(ctx.session, args.note_id, ctx.project.id)
    return f"# {note.title}\n\n{note.content}"


@tool("create_note", "Store durable knowledge as a new note (one note per topic; prefer update_note).", CreateNoteArgs)
async def create_note(ctx: ToolContext, args: CreateNoteArgs) -> str:
    if not args.force:
        existing = [n for n in await note_service.list_for_project(ctx.session, ctx.project.id) if similar(n.title, args.title)]
        if existing:
            found = "; ".join(f"{n.id}: {n.title}" for n in existing[:3])
            raise ToolError(f"Similar note exists ({found}). Update it with update_note, or pass force=true if this is really a different topic.")
    note = await note_service.create(ctx.session, ctx.project.id, NoteCreate(**args.model_dump(exclude={"force"})), by_agent_id=ctx.agent.id)
    return f"Created note {note.id}."


@tool("update_note", "Edit a note (replace title/content, pin/unpin).", UpdateNoteArgs)
async def update_note(ctx: ToolContext, args: UpdateNoteArgs) -> str:
    note = await note_service.get(ctx.session, args.note_id, ctx.project.id)
    await note_service.update(ctx.session, note, NoteUpdate(**args.model_dump(exclude_unset=True, exclude={"note_id"})))
    return f"Updated note {note.id}."


@tool("delete_note", "Delete a note.", NoteRef, roles=(MANAGER,))
async def delete_note(ctx: ToolContext, args: NoteRef) -> str:
    note = await note_service.get(ctx.session, args.note_id, ctx.project.id)
    await note_service.delete(ctx.session, note)
    return f"Deleted note {args.note_id}."
