"""Project notes: shared, durable knowledge. Listing returns titles only (load content on demand).
Own notes (remember/forget): private to one member and always in its prompt."""

from __future__ import annotations

from difflib import SequenceMatcher

from pydantic import BaseModel, Field

from ..orchestration import prompts
from ..schemas import NoteCreate, NoteUpdate
from ..services import agents as agent_service
from ..services import notes as note_service
from ..services import own_notes as own_note_service
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


class RememberArgs(BaseModel):
    text: str = Field(min_length=1, max_length=500, description="One fact or rule, short")
    note_id: int | None = Field(default=None, description="Replace this own note instead of adding one")


class ForgetArgs(BaseModel):
    note_ids: list[int] = Field(min_length=1)


async def _own_block(ctx: ToolContext) -> str:
    return prompts.own_notes_block(await own_note_service.for_agent(ctx.session, ctx.agent.id))


async def _mark_seen(ctx: ToolContext, before: str) -> None:
    """The agent knows what it just wrote, so the change needs no briefing update in its next turn
    (unless its notes had changed in a way it has not seen yet)."""
    conv = await agent_service.conversation_for(ctx.session, ctx.agent.id)
    seen = dict(conv.prompt_sections or {})
    if seen.get("own_notes", "") != before:
        return
    if after := await _own_block(ctx):
        seen["own_notes"] = after
    else:
        seen.pop("own_notes", None)
    conv.prompt_sections = seen
    await ctx.session.commit()


@tool("remember", "Save to your own notes: always in your prompt, also after a context reset.", RememberArgs)
async def remember(ctx: ToolContext, args: RememberArgs) -> str:
    before = await _own_block(ctx)
    note = await own_note_service.get(ctx.session, args.note_id, ctx.agent.id) if args.note_id else None
    note = await own_note_service.save(ctx.session, ctx.agent, args.text, note=note, by_agent_id=ctx.agent.id)
    await _mark_seen(ctx, before)
    used = own_note_service.used(await own_note_service.for_agent(ctx.session, ctx.agent.id))
    return f"Saved as own note {note.id} ({used}/{own_note_service.BUDGET_CHARS} chars)."


@tool("forget", "Delete own notes that no longer apply.", ForgetArgs)
async def forget(ctx: ToolContext, args: ForgetArgs) -> str:
    before = await _own_block(ctx)
    notes = [await own_note_service.get(ctx.session, i, ctx.agent.id) for i in args.note_ids]
    await own_note_service.delete(ctx.session, notes)
    await _mark_seen(ctx, before)
    return f"Deleted {len(notes)} own note(s)."


@tool("list_notes","List project notes (id, title, age, size). Use get_note to read one.", ListNotesArgs)
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
