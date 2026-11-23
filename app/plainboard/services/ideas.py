"""A project's ideas: a room for planning. Collecting and sorting them starts nothing; tickets come from them
only when the user asks the manager (tools/ideas.py) or takes one idea over by hand.

A model (``orchestration/ideas.py``) reads what a person types (a new idea, an addition to one, several,
merging, or a question back), sorts ideas into topics (islands on the board) with similar ones and duplicates
(``IdeaLink``), and rearranges the topics now and then. What a person placed or named by hand stays (``manual``).
Every step is an ``IdeaChange`` that can be undone, every replaced text an ``IdeaVersion``: no idea and no text
is ever lost. Only a person deletes an idea, on purpose.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Agent, Idea, IdeaChange, IdeaLink, IdeaStatus, IdeaTopic, IdeaVersion, InboxItem, Project, Task, TaskStatus, utcnow
from ..schemas import IdeaChangeOut, IdeaCreate, IdeaOut, IdeaTicketIn, IdeaTopicIn, IdeaTopicOut, TaskCreate
from . import board as board_service
from .errors import NotFound, ServiceError
from .util import truncate

COLORS = 10  # the design's --chart-1..10
PENDING, SORTED, FAILED = "pending", "sorted", "failed"
SIMILAR, DUPLICATE = "similar", "duplicate"
SORT_TEXT = 600  # what the sorting model reads of an idea
CONTEXT_IDEAS = 300  # other ideas the model is shown
QUESTION_OPEN, QUESTION_ANSWERED, QUESTION_WITHDRAWN = "open", "answered", "withdrawn"
IDEA_FIELDS = (
    "title", "text", "status", "topic_id", "topic_manual", "task_id", "hint", "sort_state", "merged_into_id", "closed_at", "closed_reason",
    "question", "question_options", "question_status", "question_answer",
)  # fmt: skip
TOPIC_FIELDS = ("name", "color", "hint", "manual", "x", "y")


# --- reading -----------------------------------------------------------------------------------


async def get(session: AsyncSession, project_id: int, idea_id: int) -> Idea:
    idea = await session.get(Idea, idea_id)
    if idea is None or idea.project_id != project_id:
        raise NotFound(f"Idea {idea_id} not found")
    return idea


async def get_topic(session: AsyncSession, project_id: int, topic_id: int) -> IdeaTopic:
    topic = await session.get(IdeaTopic, topic_id)
    if topic is None or topic.project_id != project_id:
        raise NotFound("Topic not found")
    return topic


async def topics(session: AsyncSession, project_id: int) -> list[IdeaTopic]:
    return list((await session.scalars(select(IdeaTopic).where(IdeaTopic.project_id == project_id).order_by(IdeaTopic.id))).all())


async def open_ideas(session: AsyncSession, project_id: int) -> list[Idea]:
    query = select(Idea).where(Idea.project_id == project_id, Idea.status == IdeaStatus.OPEN).order_by(Idea.id)
    return list((await session.scalars(query)).all())


async def listed(session: AsyncSession, project_id: int, *, closed: bool = False) -> list[Idea]:
    query = select(Idea).where(Idea.project_id == project_id).order_by(Idea.id)
    if not closed:
        query = query.where(Idea.status == IdeaStatus.OPEN)
    return list((await session.scalars(query)).all())


async def versions(session: AsyncSession, idea: Idea) -> list[IdeaVersion]:
    return list((await session.scalars(select(IdeaVersion).where(IdeaVersion.idea_id == idea.id).order_by(IdeaVersion.id.desc()))).all())


async def get_change(session: AsyncSession, project_id: int, change_id: int) -> IdeaChange:
    change = await session.get(IdeaChange, change_id)
    if change is None or change.project_id != project_id:
        raise NotFound("Change not found")
    return change


async def changes(session: AsyncSession, project_id: int, limit: int = 20) -> list[IdeaChange]:
    query = select(IdeaChange).where(IdeaChange.project_id == project_id).order_by(IdeaChange.id.desc()).limit(limit)
    return list((await session.scalars(query)).all())


async def outs(session: AsyncSession, ideas: list[Idea]) -> list[IdeaOut]:
    """The ideas as the API shows them: with their ticket's number and the open ideas they are linked to."""
    if not ideas:
        return []
    ids = [i.id for i in ideas]
    links = (await session.scalars(select(IdeaLink).where(or_(IdeaLink.idea_id.in_(ids), IdeaLink.other_id.in_(ids))))).all()
    linked = {link.idea_id for link in links} | {link.other_id for link in links}
    still_open = set((await session.scalars(select(Idea.id).where(Idea.id.in_(linked), Idea.status == IdeaStatus.OPEN))).all()) if linked else set()
    task_ids = {i.task_id for i in ideas if i.task_id}
    numbers = dict((await session.execute(select(Task.id, Task.number).where(Task.id.in_(task_ids)))).all()) if task_ids else {}
    result = []
    for idea in ideas:
        out = IdeaOut.model_validate(idea)
        out.task_number = numbers.get(idea.task_id) if idea.task_id else None
        similar: list[int] = []
        for link in links:
            if link.idea_id == idea.id and link.kind == DUPLICATE and link.other_id in still_open:
                out.duplicate_of = link.other_id
            elif link.idea_id == idea.id and link.other_id in still_open:
                similar.append(link.other_id)
            elif link.other_id == idea.id and link.idea_id in still_open:
                similar.append(link.idea_id)
        out.similar = sorted(set(similar) - {out.duplicate_of})
        result.append(out)
    return result


async def change_out(session: AsyncSession, change: IdeaChange, *, with_ideas: bool = False) -> IdeaChangeOut:
    out = IdeaChangeOut.model_validate(change)
    if with_ideas:
        ids = set(change.summary.get("ideas") or [])
        found = list((await session.scalars(select(Idea).where(Idea.id.in_(ids)).order_by(Idea.id))).all()) if ids else []
        out.ideas = await outs(session, found)
    return out


async def publish(session: AsyncSession, project_id: int, ideas: list[Idea]) -> None:
    for out in await outs(session, ideas):
        bus.publish(project_id, "idea.upserted", out)


def publish_topic(topic: IdeaTopic) -> None:
    bus.publish(topic.project_id, "idea_topic.upserted", IdeaTopicOut.model_validate(topic))


async def _linked(session: AsyncSession, ids: list[int]) -> set[int]:
    query = select(IdeaLink.idea_id, IdeaLink.other_id).where(or_(IdeaLink.idea_id.in_(ids), IdeaLink.other_id.in_(ids)))
    return {a for pair in (await session.execute(query)).all() for a in pair} - set(ids)


async def _publish_linked(session: AsyncSession, project_id: int, ids: list[int], others: set[int] | None = None) -> None:
    """Ideas linked to these: what they show of their links changed."""
    others = await _linked(session, ids) if others is None else others
    if others:
        await publish(session, project_id, list((await session.scalars(select(Idea).where(Idea.id.in_(others), Idea.status == IdeaStatus.OPEN))).all()))


# --- changes that can be undone ----------------------------------------------------------------


def _plain(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


class Snap:
    """The state before a change, of what it touches: an idea or topic is taken before its first change."""

    def __init__(self) -> None:
        self.ideas: dict[int, dict[str, Any]] = {}
        self.topics: dict[int, dict[str, Any]] = {}
        self.new_ideas: list[int] = []
        self.new_topics: list[int] = []

    def idea(self, idea: Idea) -> Idea:
        if idea.id not in self.ideas and idea.id not in self.new_ideas:
            self.ideas[idea.id] = {f: _plain(getattr(idea, f)) for f in IDEA_FIELDS}
        return idea

    def topic(self, topic: IdeaTopic) -> IdeaTopic:
        if topic.id not in self.topics and topic.id not in self.new_topics:
            self.topics[topic.id] = {f: getattr(topic, f) for f in TOPIC_FIELDS}
        return topic

    def data(self) -> dict[str, Any]:
        return {"ideas": {str(k): v for k, v in self.ideas.items()}, "topics": {str(k): v for k, v in self.topics.items()}, "new_ideas": self.new_ideas, "new_topics": self.new_topics}


def _record(session: AsyncSession, project_id: int, kind: str, source: str, author: str, summary: dict[str, Any], snap: Snap) -> IdeaChange:
    """Not committed. ``summary["ideas"]``: the ideas it touched, for the UI."""
    summary.setdefault("ideas", sorted(set(snap.ideas) | set(snap.new_ideas)))
    change = IdeaChange(project_id=project_id, kind=kind, source=source, author=author[:100], summary=summary, undo=snap.data())
    session.add(change)
    return change


async def _publish_change(session: AsyncSession, change: IdeaChange) -> None:
    bus.publish(change.project_id, "idea_change.upserted", await change_out(session, change))


def _version(session: AsyncSession, idea: Idea, source: str) -> None:
    session.add(IdeaVersion(project_id=idea.project_id, idea_id=idea.id, title=idea.title, text=idea.text, source=source))


async def undo(session: AsyncSession, change: IdeaChange) -> IdeaChange:
    """Back to the state before ``change``: texts, topics, what was merged or taken. Ideas it created are set
    aside (dismissed, "undone"), never deleted; the replaced texts stay in the versions."""
    if change.undone_at is not None:
        raise ServiceError("This was undone already.", 409)
    data = change.undo or {}
    project_id = change.project_id
    shown_topics: list[IdeaTopic] = []
    for key, fields in (data.get("topics") or {}).items():
        topic = await session.get(IdeaTopic, int(key))
        if topic is None:
            topic = IdeaTopic(id=int(key), project_id=project_id, **fields)
            session.add(topic)
        else:
            for field, value in fields.items():
                setattr(topic, field, value)
        shown_topics.append(topic)
    await session.flush()
    touched: list[Idea] = []
    for key, fields in (data.get("ideas") or {}).items():
        idea = await session.get(Idea, int(key))
        if idea is None or idea.project_id != project_id:
            continue  # deleted by a person since
        if (idea.title, idea.text) != (fields.get("title"), fields.get("text")):
            _version(session, idea, "undo")
        for field, value in fields.items():
            if field == "closed_at":
                value = datetime.fromisoformat(value) if value else None
            elif field in ("topic_id", "merged_into_id", "task_id") and value is not None:
                table = {"topic_id": IdeaTopic, "merged_into_id": Idea, "task_id": Task}[field]
                value = value if await session.get(table, value) is not None else None
            setattr(idea, field, value)
        touched.append(idea)
    now = utcnow()
    for idea_id in data.get("new_ideas") or []:
        idea = await session.get(Idea, idea_id)
        if idea is not None and idea.status == IdeaStatus.OPEN:
            idea.status, idea.closed_at, idea.closed_reason = IdeaStatus.DISMISSED, now, "undone"
            touched.append(idea)
    await session.flush()
    gone: list[int] = []
    for topic_id in data.get("new_topics") or []:
        topic = await session.get(IdeaTopic, topic_id)
        used = await session.scalar(select(Idea.id).where(Idea.topic_id == topic_id, Idea.status == IdeaStatus.OPEN).limit(1))
        if topic is not None and not topic.manual and used is None:
            gone.append(topic.id)
            await session.delete(topic)
    change.undone_at = now
    await session.commit()
    for topic_id in gone:
        bus.publish(project_id, "idea_topic.deleted", {"id": topic_id})
    for topic in shown_topics:
        publish_topic(topic)
    await publish(session, project_id, touched)
    await _publish_change(session, change)
    return change


# --- what a person does ------------------------------------------------------------------------


async def create(session: AsyncSession, project_id: int, data: IdeaCreate, author: str) -> Idea:
    """An idea as typed, without the model reading it first (``orchestration.ideas.Ideas.read_input`` does)."""
    if data.topic_id is not None:
        await get_topic(session, project_id, data.topic_id)
    placed = data.topic_id is not None or data.x is not None or data.y is not None
    idea = Idea(
        project_id=project_id, title=data.title.strip(), text=data.text.strip(), author=author[:100], topic_id=data.topic_id, x=data.x, y=data.y,
        topic_manual=placed, sort_state=PENDING,
    )  # fmt: skip
    session.add(idea)
    await session.commit()
    await publish(session, project_id, [idea])
    return idea


async def update(session: AsyncSession, idea: Idea, fields: dict[str, Any], author: str) -> tuple[Idea, IdeaChange | None]:
    """A person's change. A new title or text keeps the old one as a version, can be undone, and has the idea
    sorted again. Moving it (topic or place) pins it: the model leaves its topic alone from then on."""
    if fields.get("topic_id") is not None:
        await get_topic(session, idea.project_id, fields["topic_id"])
    title = fields.pop("title", None)
    text = fields.pop("text", None)
    title = title.strip() if title is not None else idea.title
    text = text.strip() if text is not None else idea.text
    change = None
    if (title, text) != (idea.title, idea.text):
        snap = Snap()
        before = {"title": idea.title, "text": idea.text}
        _set_text(session, snap, idea, title, text, "user")
        change = _record(session, idea.project_id, "edit", "user", author, {"idea_id": idea.id, "before": before, "after": {"title": title, "text": text}}, snap)
    if {"topic_id", "x", "y"} & set(fields):
        idea.topic_manual = True
        if "topic_id" in fields and idea.sort_state != SORTED and change is None:
            idea.sort_state = SORTED
    for key, value in fields.items():
        setattr(idea, key, value)
    await session.commit()
    await publish(session, idea.project_id, [idea])
    if change is not None:
        await _publish_change(session, change)
    return idea, change


async def restore_version(session: AsyncSession, idea: Idea, version_id: int, author: str) -> tuple[Idea, IdeaChange | None]:
    version = await session.get(IdeaVersion, version_id)
    if version is None or version.idea_id != idea.id:
        raise NotFound("Version not found")
    return await update(session, idea, {"title": version.title, "text": version.text}, author)


async def remove(session: AsyncSession, idea: Idea) -> None:
    """Deleted on purpose by a person: gone for good, with its links and versions."""
    project_id, idea_id = idea.project_id, idea.id
    others = await _linked(session, [idea_id])
    await session.delete(idea)
    await session.commit()
    bus.publish(project_id, "idea.deleted", {"id": idea_id})
    await _publish_linked(session, project_id, [idea_id], others)


async def withdraw_question(session: AsyncSession, idea: Idea) -> Idea:
    if idea.question_status != QUESTION_OPEN:
        raise ServiceError("This idea has no open question.", 409)
    idea.question_status = QUESTION_WITHDRAWN
    await session.commit()
    await publish(session, idea.project_id, [idea])
    return idea


async def take_answer(session: AsyncSession, idea: Idea, option_index: int | None, text: str) -> str:
    """The person's answer to the model's question about the idea, in words. The model then works it into the idea."""
    text = text.strip()
    if idea.question_status != QUESTION_OPEN:
        raise ServiceError("This idea has no open question.", 409)
    if option_index is None and not text:
        raise ServiceError("Pick an option or write an answer.")
    if option_index is not None and option_index >= len(idea.question_options):
        raise ServiceError("Unknown option.")
    parts = [idea.question_options[option_index]["label"]] if option_index is not None else []
    answer = " — ".join(parts + ([text] if text else []))
    idea.question_status, idea.question_answer = QUESTION_ANSWERED, answer
    await session.commit()
    await publish(session, idea.project_id, [idea])
    return answer


async def _free_color(session: AsyncSession, project_id: int) -> int:
    used = list((await session.scalars(select(IdeaTopic.color).where(IdeaTopic.project_id == project_id))).all())
    return next((c for c in range(COLORS) if c not in used), len(used) % COLORS)


async def create_topic(session: AsyncSession, project_id: int, data: IdeaTopicIn) -> IdeaTopic:
    if not data.name:
        raise ServiceError("A topic needs a name.")
    color = data.color if data.color is not None else await _free_color(session, project_id)
    topic = IdeaTopic(project_id=project_id, name=data.name.strip(), color=color, x=data.x, y=data.y, manual=True)
    session.add(topic)
    await session.commit()
    publish_topic(topic)
    return topic


async def update_topic(session: AsyncSession, topic: IdeaTopic, data: IdeaTopicIn) -> IdeaTopic:
    """Renamed by a person: the model keeps the name from then on."""
    fields = data.model_dump(exclude_unset=True)
    if fields.get("name"):
        fields["name"] = fields["name"].strip()
        topic.manual = topic.manual or fields["name"] != topic.name
    for key, value in fields.items():
        if value is not None or key in ("x", "y"):
            setattr(topic, key, value)
    await session.commit()
    publish_topic(topic)
    return topic


async def remove_topic(session: AsyncSession, topic: IdeaTopic) -> None:
    """Its ideas stay, unsorted."""
    project_id, topic_id = topic.project_id, topic.id
    ideas = list((await session.scalars(select(Idea).where(Idea.topic_id == topic_id))).all())
    for idea in ideas:
        idea.topic_id, idea.topic_manual = None, False
    await session.delete(topic)
    await session.commit()
    bus.publish(project_id, "idea_topic.deleted", {"id": topic_id})
    await publish(session, project_id, [i for i in ideas if i.status == IdeaStatus.OPEN])


async def _drop_empty_topics(session: AsyncSession, project_id: int, snap: Snap | None = None) -> list[int]:
    """Topics without an open idea that no person made or named; not committed."""
    used = select(Idea.topic_id).where(Idea.project_id == project_id, Idea.status == IdeaStatus.OPEN, Idea.topic_id.is_not(None))
    query = select(IdeaTopic).where(IdeaTopic.project_id == project_id, IdeaTopic.manual.is_(False), IdeaTopic.id.not_in(used))
    empty = list((await session.scalars(query)).all())
    for topic in empty:
        if snap is not None:
            snap.topic(topic)
        await session.delete(topic)
    return [t.id for t in empty]


# --- the model's answers -----------------------------------------------------------------------


def parse_json(text: str | None) -> dict[str, Any]:
    """The JSON object in a model's answer: also inside a code fence, with words around it, or with trailing
    commas. ValueError if there is none."""
    text = (text or "").strip()
    candidates = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    candidates.append(text)
    for candidate in candidates:
        for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                value = json.loads(attempt)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("no JSON object in the answer")


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and (digits := value.strip().lstrip("#iI")).isdigit():
        return int(digits)
    return None


def _ints(value: Any) -> list[int]:
    return [n for n in (_int(v) for v in (value if isinstance(value, list) else [value])) if n is not None]


def _str(value: Any, limit: int) -> str:
    return truncate(value.strip(), limit) if isinstance(value, str) else ""


def _options(value: Any) -> list[dict[str, str]]:
    found = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str) and item.strip():
            found.append({"label": _str(item, 100), "description": ""})
        elif isinstance(item, dict) and _str(item.get("label"), 100):
            found.append({"label": _str(item.get("label"), 100), "description": _str(item.get("description"), 300)})
    return found[:4]


def _idea_line(idea: Idea, limit: int) -> str:
    text = " ".join(idea.text.split())
    head = f"{idea.title.strip()}: " if idea.title.strip() else ""
    return f"i{idea.id} {truncate(head + text, limit)}"


def _project_head(project: Project) -> str:
    return f"Project: {project.name}" + (f"\n{truncate(project.description, 1000)}" if project.description.strip() else "")


def _set_text(session: AsyncSession, snap: Snap, idea: Idea, title: str, text: str, source: str) -> None:
    """A new title and text; the old ones are kept as a version. The idea is sorted again."""
    snap.idea(idea)
    _version(session, idea, source)
    idea.title, idea.text, idea.sort_state = truncate(title, 200), text, PENDING


def _merge_into(snap: Snap, idea: Idea, target: Idea) -> None:
    snap.idea(idea)
    idea.status, idea.merged_into_id, idea.closed_at, idea.closed_reason = IdeaStatus.MERGED, target.id, utcnow(), f"merged into i{target.id}"


async def _new_idea(session: AsyncSession, snap: Snap, project_id: int, title: str, text: str, author: str) -> Idea:
    idea = Idea(project_id=project_id, title=truncate(title, 200), text=text, author=author[:100], sort_state=PENDING)
    session.add(idea)
    await session.flush()
    snap.new_ideas.append(idea.id)
    return idea


# --- reading an input --------------------------------------------------------------------------

INTENT_SYSTEM = """\
You read what a person typed into the board of ideas of a software project and decide what they mean. You \
change nothing yourself; ideas are only collected here, nothing is built.

Answer with one JSON object and nothing else, one of:
{"action": "new", "ideas": [{"title": "...", "text": "..."}], "similar": [5]}
  A new idea. Several entries in "ideas" if the text holds several independent ideas. "similar": ids of \
existing ideas about nearly the same thing.
{"action": "amend", "target": 12, "title": "...", "text": "..."}
  The text changes or adds to idea 12: its whole new title and text. Keep everything it said unless the \
person asks to drop it.
{"action": "merge", "ids": [3, 7], "title": "...", "text": "..."}
  The person asks to put these ideas together: the whole title and text of the merged idea.
{"action": "question", "question": "...", "options": [{"label": "...", "description": "..."}]}
  Only if you cannot tell at all what is meant: a short question with 2-4 options, the recommended first.

Only amend or merge when the person clearly means that existing idea. When in doubt, make it a new idea and \
name the close ones in "similar": never overwrite an idea silently. Keep the person's words and language. A \
title has 2-6 words. Ideas are named by id (i12).
"""

RESOLVE_NOTE = """\
Idea i{id} was unclear, so the person was asked: {question}
Their answer: {answer}
Work the answer into the ideas: "amend" i{id} with it, or "merge" it with the idea it belongs to (ids \
including {id}), or "new" with several ideas if it splits up (the first replaces i{id}). Do not ask again."""


async def intent_prompt(session: AsyncSession, project: Project, text: str, draft: Idea | None = None, answer: str = "") -> str:
    ideas = await open_ideas(session, project.id)
    names = {t.id: t.name for t in await topics(session, project.id)}
    parts = [_project_head(project)]
    if ideas:
        parts.append("Ideas:\n" + "\n".join(f"{_idea_line(i, SORT_TEXT if draft and i.id == draft.id else 300)} [{names.get(i.topic_id, 'no topic')}]" for i in ideas[-CONTEXT_IDEAS:]))
    if draft is not None:
        parts.append(RESOLVE_NOTE.format(id=draft.id, question=draft.question, answer=answer))
    else:
        parts.append(f"The person typed:\n{text}")
    return "\n\n".join(parts)


async def apply_intent(session: AsyncSession, project_id: int, data: dict[str, Any] | None, text: str, author: str, *, draft: Idea | None = None) -> IdeaChange:
    """Do what the model read in ``text``, or (``draft``) what it made of the answer to its question about
    that idea. An answer it got wrong, or none, never loses the text: it becomes a new idea, or the answer is
    added to the draft."""
    data = data if isinstance(data, dict) else {}
    action = data.get("action")
    by_id = {i.id: i for i in await open_ideas(session, project_id)}
    if draft is not None:
        by_id[draft.id] = draft
    snap = Snap()
    summary: dict[str, Any] = {"input": text}
    kind = ""
    title, new_text = _str(data.get("title"), 200), data.get("text").strip() if isinstance(data.get("text"), str) else ""
    if action == "amend" and (target := by_id.get(_int(data.get("target")) or -1)) is not None and new_text:
        summary.update(idea_id=target.id, before={"title": target.title, "text": target.text}, after={"title": title or target.title, "text": new_text})
        _set_text(session, snap, target, title or target.title, new_text, "ai")
        if draft is not None and draft.id != target.id:
            _merge_into(snap, draft, target)
            summary["merged"] = [draft.id]
        kind = "amend"
    elif action == "merge" and new_text:
        ids = [i for i in dict.fromkeys(_ints(data.get("ids"))) if i in by_id]
        if draft is not None and draft.id not in ids:
            ids.append(draft.id)
        if len(ids) >= 2:
            target = by_id[ids[0]]
            summary.update(idea_id=target.id, merged=ids[1:], before={str(i): {"title": by_id[i].title, "text": by_id[i].text} for i in ids}, after={"title": title or target.title, "text": new_text})
            _set_text(session, snap, target, title or target.title, new_text, "ai")
            for idea_id in ids[1:]:
                _merge_into(snap, by_id[idea_id], target)
            kind = "merge"
    elif action in ("new", "several"):
        items = [(_str(i.get("title"), 200), i["text"].strip()) for i in data.get("ideas") or [] if isinstance(i, dict) and isinstance(i.get("text"), str) and i["text"].strip()]
        if items:
            made: list[Idea] = []
            for n, (item_title, item_text) in enumerate(items[:20]):
                if n == 0 and draft is not None:
                    summary.update(idea_id=draft.id, before={"title": draft.title, "text": draft.text}, after={"title": item_title or draft.title, "text": item_text})
                    _set_text(session, snap, draft, item_title or draft.title, item_text, "ai")
                    made.append(draft)
                else:
                    made.append(await _new_idea(session, snap, project_id, item_title, item_text, author))
            similar = [i for i in dict.fromkeys(_ints(data.get("similar"))) if i in by_id]
            for idea in made:
                for other in similar:
                    if other != idea.id:
                        session.add(IdeaLink(project_id=project_id, idea_id=idea.id, other_id=other, kind=SIMILAR))
            summary.update(created=snap.new_ideas[:], similar=similar)
            kind = "several" if len(made) > 1 else "new"
    elif action == "question" and draft is None and (question := _str(data.get("question"), 500)) and (options := _options(data.get("options"))):
        idea = await _new_idea(session, snap, project_id, "", text, author)
        idea.question, idea.question_options, idea.question_status, idea.question_at = question, options, QUESTION_OPEN, utcnow()
        summary.update(created=[idea.id], question=question)
        kind = "question"
    if not kind:  # nothing usable: keep what the person wrote
        summary["fallback"] = True
        if draft is not None:
            summary.update(idea_id=draft.id, before={"title": draft.title, "text": draft.text})
            _set_text(session, snap, draft, draft.title, f"{draft.text}\n\n{draft.question}\n→ {text}", "ai")
            summary["after"] = {"title": draft.title, "text": draft.text}
            kind = "amend"
        else:
            idea = await _new_idea(session, snap, project_id, "", text, author)
            summary["created"] = [idea.id]
            kind = "new"
    change = _record(session, project_id, kind, "answer" if draft is not None else "input", author, summary, snap)
    await session.commit()
    touched = [i for i in (await session.scalars(select(Idea).where(Idea.id.in_(change.summary["ideas"])))).all()]
    await publish(session, project_id, touched)
    await _publish_change(session, change)
    return change


# --- sorting -----------------------------------------------------------------------------------

SORT_SYSTEM = """\
You sort the ideas of a software project into topics, for a board of ideas. You only sort: you plan and \
start nothing.

Answer with one JSON object and nothing else:
{"topics": [{"name": "...", "hint": "..."}],
 "ideas": [{"id": 12, "topic": "...", "similar": [5], "duplicate_of": null, "hint": "...", "unsure": false}]}

- "ideas": one entry for every idea under "Sort these", by its id.
- "topic": the name of an existing topic if the idea fits it (exactly as written), else a new one: one to \
three words, in the language of the ideas.
- "similar": ids of other ideas about nearly the same thing. "duplicate_of": the id of an idea that says the \
same, if there is one. Only ids from this message.
- "hint": a few words for the person, only where there is something to say, e.g. "similar to i5". Else "".
- "unsure": true if the idea fits no topic well or the topics should be rearranged.
- "question": rarely, only if an idea is so unclear that it can neither be sorted nor later planned: \
{"question": "...", "options": [{"label": "...", "description": "..."}]}, 2-4 options, the recommended \
first. At most one question per answer.
- "topics": every topic you used, with a short hint about it, e.g. "3 ideas about payment".
"""


def sort_prompt(project: Project, existing: list[IdeaTopic], ideas: list[Idea], batch: list[Idea]) -> str:
    names = {t.id: t.name for t in existing}
    batch_ids = {i.id for i in batch}
    parts = [_project_head(project)]
    if existing:
        parts.append("Topics:\n" + "\n".join(f"- {t.name}" + (f" ({t.hint})" if t.hint else "") for t in existing))
    others = [i for i in ideas if i.id not in batch_ids]
    if others:
        parts.append("Ideas already sorted:\n" + "\n".join(f"{_idea_line(i, 200)} [{names.get(i.topic_id, 'no topic')}]" for i in others[-CONTEXT_IDEAS:]))
    parts.append("Sort these:\n" + "\n".join(_idea_line(i, SORT_TEXT) + (" [asked before: no question]" if i.question_status else "") for i in batch))
    return "\n\n".join(parts)


async def sort_failed(session: AsyncSession, project_id: int, ids: list[int]) -> None:
    ideas = list((await session.scalars(select(Idea).where(Idea.id.in_(ids), Idea.sort_state == PENDING))).all())
    for idea in ideas:
        idea.sort_state = FAILED
    await session.commit()
    await publish(session, project_id, ideas)


async def apply_sort(session: AsyncSession, project_id: int, ids: list[int], data: dict[str, Any]) -> tuple[int, bool]:
    """Store what the model answered for the ideas ``ids``. What it got wrong is left out: an idea it did not
    sort stays unsorted (failed), a link to an id it made up is not made, an idea a person placed keeps its
    topic. Returns how many it sorted and whether it was unsure."""
    batch = {i.id: i for i in (await session.scalars(select(Idea).where(Idea.id.in_(ids), Idea.status == IdeaStatus.OPEN))).all()}
    valid = set((await session.scalars(select(Idea.id).where(Idea.project_id == project_id, Idea.status == IdeaStatus.OPEN))).all())
    by_name = {t.name.casefold(): t for t in await topics(session, project_id)}
    touched: dict[int, IdeaTopic] = {}
    unsure, asked = False, False

    async def topic_named(name: str) -> IdeaTopic:
        topic = by_name.get(name.casefold())
        if topic is None:
            topic = IdeaTopic(project_id=project_id, name=name, color=await _free_color(session, project_id))
            session.add(topic)
            await session.flush()
            by_name[name.casefold()] = topic
        return topic

    done: set[int] = set()
    items = data.get("ideas")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or (idea := batch.get(_int(item.get("id")) or -1)) is None or idea.id in done:
            continue
        done.add(idea.id)
        name = _str(item.get("topic"), 100)
        if name and not idea.topic_manual:
            topic = await topic_named(name)
            idea.topic_id = topic.id
            touched[topic.id] = topic
        idea.hint = _str(item.get("hint"), 300)
        idea.sort_state = SORTED
        unsure = unsure or item.get("unsure") is True
        question = item.get("question")
        if not asked and not idea.question_status and isinstance(question, dict) and (text := _str(question.get("question"), 500)) and (options := _options(question.get("options"))):
            idea.question, idea.question_options, idea.question_status, idea.question_at = text, options, QUESTION_OPEN, utcnow()
            asked = True
        await session.execute(delete(IdeaLink).where(IdeaLink.idea_id == idea.id))
        duplicate = _int(item.get("duplicate_of"))
        if duplicate in valid and duplicate != idea.id:
            session.add(IdeaLink(project_id=project_id, idea_id=idea.id, other_id=duplicate, kind=DUPLICATE))
        for other in dict.fromkeys(_ints(item.get("similar"))):
            if other in valid and other not in (idea.id, duplicate):
                session.add(IdeaLink(project_id=project_id, idea_id=idea.id, other_id=other, kind=SIMILAR))
    for idea in batch.values():
        if idea.id not in done and idea.sort_state == PENDING:
            idea.sort_state = FAILED
    named = data.get("topics")
    for item in named if isinstance(named, list) else []:
        if isinstance(item, dict) and (topic := by_name.get(_str(item.get("name"), 100).casefold())) is not None and isinstance(item.get("hint"), str):
            topic.hint = _str(item["hint"], 300)
            touched[topic.id] = topic
    await session.commit()
    for topic in touched.values():
        publish_topic(topic)
    await publish(session, project_id, list(batch.values()))
    await _publish_linked(session, project_id, list(done))
    return len(done), unsure


# --- rearranging the topics --------------------------------------------------------------------

REORDER_SYSTEM = """\
You keep the board of ideas of a software project clear. Rearrange its topics: split a topic that grew \
big or mixes things, merge topics that belong together, rename unclear ones, make a topic where several \
ideas share one, move ideas to where they fit best. A clear overview matters more than keeping things as \
they are. A topic marked (fixed) keeps its name; an idea marked (fixed) stays where it is.

Answer with one JSON object and nothing else:
{"topics": [{"id": 4, "name": "...", "hint": "...", "ideas": [12, 15]}, {"id": null, "name": "...", "hint": "...", "ideas": [3, 8]}]}
- Every idea id exactly once. "id": an existing topic to keep or rename, null for a new one. Existing topics \
you leave out are removed.
- Names have one to three words, in the language of the ideas; "hint" is a short note, e.g. "3 ideas about \
payment". Usually 3 to 10 topics, with 2 or more ideas each.
"""


def reorder_prompt(project: Project, existing: list[IdeaTopic], ideas: list[Idea]) -> str:
    parts = [_project_head(project)]
    if existing:
        parts.append("Topics:\n" + "\n".join(f"- id {t.id}: {t.name}" + (" (fixed)" if t.manual else "") + (f" - {t.hint}" if t.hint else "") for t in existing))
    parts.append("Ideas [topic id]:\n" + "\n".join(f"{_idea_line(i, 250)} [{i.topic_id or 'none'}]" + (" (fixed)" if i.topic_manual else "") for i in ideas))
    return "\n\n".join(parts)


async def apply_reorder(session: AsyncSession, project_id: int, data: dict[str, Any], source: str) -> IdeaChange | None:
    """Rearrange the topics as the model proposes, within the rules: a person's topic keeps its name, a
    person's idea stays, an idea the answer forgot keeps its topic, a topic is removed only once it is empty.
    Returns the change (what was split, merged, renamed, moved), or None if nothing changed."""
    ideas = await open_ideas(session, project_id)
    by_id = {i.id: i for i in ideas}
    existing = {t.id: t for t in await topics(session, project_id)}
    old_names = {t.id: t.name for t in existing.values()}
    snap = Snap()
    placed: dict[int, IdeaTopic] = {}
    kept: set[int] = set()
    renamed: list[dict[str, Any]] = []
    new_topics: list[IdeaTopic] = []
    hinted: list[IdeaTopic] = []
    items = data.get("topics")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        name, topic_id = _str(item.get("name"), 100), _int(item.get("id"))
        members = [i for i in dict.fromkeys(_ints(item.get("ideas"))) if i in by_id and i not in placed]
        topic = existing.get(topic_id) if topic_id not in kept else None
        if topic is None and name:
            topic = next((t for t in existing.values() if t.id not in kept and t.name.casefold() == name.casefold()), None)
        if topic is not None:
            kept.add(topic.id)
            snap.topic(topic)
            if name and name != topic.name and not topic.manual:
                renamed.append({"topic_id": topic.id, "from": topic.name, "to": name})
                topic.name = name
        elif name and members:
            topic = IdeaTopic(project_id=project_id, name=name, color=await _free_color(session, project_id))
            session.add(topic)
            await session.flush()
            snap.new_topics.append(topic.id)
            new_topics.append(topic)
        else:
            continue
        if isinstance(item.get("hint"), str) and _str(item["hint"], 300) != topic.hint:
            topic.hint = _str(item["hint"], 300)
            hinted.append(topic)
        for idea_id in members:
            placed[idea_id] = topic
    moved: list[dict[str, Any]] = []
    sources: dict[int, set[str]] = {}
    for idea_id, topic in placed.items():
        idea = by_id[idea_id]
        if idea.topic_manual:
            continue
        if idea.topic_id != topic.id:
            snap.idea(idea)
            moved.append({"idea_id": idea.id, "title": idea.title or truncate(idea.text, 60), "from": old_names.get(idea.topic_id), "to": topic.name})
            sources.setdefault(topic.id, set()).add(old_names.get(idea.topic_id) or "")
            idea.topic_id = topic.id
        if idea.sort_state != SORTED:
            snap.idea(idea)
            idea.sort_state = SORTED
    await session.flush()
    destinations: dict[int, set[str]] = {}
    for move in moved:
        for topic_id, name in old_names.items():
            if move["from"] == name:
                destinations.setdefault(topic_id, set()).add(move["to"])
    gone = await _drop_empty_topics(session, project_id, snap)
    removed = [{"topic_id": t, "name": old_names[t], "into": sorted(destinations.get(t, set()))} for t in gone if t in old_names]
    created = [{"topic_id": t.id, "name": t.name, "from": sorted(n for n in sources.get(t.id, set()) if n)} for t in new_topics if t.id not in gone]
    change = None
    if renamed or moved or removed or created:
        summary = {"renamed": renamed, "created": created, "removed": removed, "moved": moved, "ideas": sorted(m["idea_id"] for m in moved)}
        change = _record(session, project_id, "reorder", source, "", summary, snap)
    await session.commit()
    for topic_id in gone:
        bus.publish(project_id, "idea_topic.deleted", {"id": topic_id})
    for topic in {t.id: t for t in [*existing.values(), *new_topics] if t.id not in gone}.values():
        if topic.id in snap.topics or topic in new_topics or topic in hinted:
            publish_topic(topic)
    await publish(session, project_id, [by_id[i] for i in snap.ideas if i in by_id])
    if change is not None:
        await _publish_change(session, change)
    return change


# --- into tickets ------------------------------------------------------------------------------


async def _open_for_taking(session: AsyncSession, project_id: int, ids: list[int]) -> dict[int, Idea]:
    found = {i.id: i for i in (await session.scalars(select(Idea).where(Idea.id.in_(ids), Idea.project_id == project_id))).all()} if ids else {}
    if missing := [i for i in ids if i not in found]:
        raise NotFound(f"Idea {missing[0]} not found")
    if closed := [i for i in found.values() if i.status != IdeaStatus.OPEN]:
        raise ServiceError(f"Idea {closed[0].id} is no longer open: it is {closed[0].status}.", 409)
    return found


async def take_one(session: AsyncSession, idea: Idea, data: IdeaTicketIn) -> Task:
    """One idea straight into a ticket, by a person. The manager reads of it with its next turn."""
    await _open_for_taking(session, idea.project_id, [idea.id])
    first_line = next((line.strip() for line in idea.text.splitlines() if line.strip()), "Idea")
    title = data.title or idea.title or truncate(first_line, 120)
    description = data.description if data.description is not None else idea.text
    (task,) = await board_service.create_tasks(session, idea.project_id, [TaskCreate(title=title, description=description, status=data.status, priority=data.priority, labels=data.labels)])
    idea.status, idea.task_id, idea.closed_at = IdeaStatus.TAKEN, task.id, utcnow()
    manager = await session.scalar(select(Agent).where(Agent.project_id == idea.project_id, Agent.is_manager.is_(True)))
    if manager is not None:
        column = "To do" if data.status == TaskStatus.TODO else "Backlog"
        text = f"The user turned idea i{idea.id} into ticket #{task.number} {truncate(task.title, 80)} in {column}. Nothing needs doing because of this note: the board's usual rules apply."
        session.add(InboxItem(agent_id=manager.id, text=text))  # read with its next turn; costs no turn
    await _close_and_publish(session, idea.project_id, [idea])
    return task


async def take(session: AsyncSession, project_id: int, task: Task, ids: list[int]) -> list[Idea]:
    """The manager made the ticket from these ideas: they leave the board of ideas, kept with their ticket."""
    ideas = await _open_for_taking(session, project_id, list(dict.fromkeys(ids)))
    now = utcnow()
    for idea in ideas.values():
        idea.status, idea.task_id, idea.closed_at = IdeaStatus.TAKEN, task.id, now
    await _close_and_publish(session, project_id, list(ideas.values()))
    return list(ideas.values())


async def leave_out(session: AsyncSession, project_id: int, ids: list[int], reason: str, duplicate_of: int | None = None) -> list[Idea]:
    """Left out on purpose (a duplicate, covered already, dropped by the user): kept, with the reason."""
    ideas = await _open_for_taking(session, project_id, list(dict.fromkeys(ids)))
    if duplicate_of is not None:
        await get(session, project_id, duplicate_of)
    now = utcnow()
    for idea in ideas.values():
        idea.status, idea.closed_at, idea.closed_reason = IdeaStatus.DISMISSED, now, truncate(reason, 300)
        if duplicate_of is not None and duplicate_of != idea.id:
            session.add(IdeaLink(project_id=project_id, idea_id=idea.id, other_id=duplicate_of, kind=DUPLICATE))
    await _close_and_publish(session, project_id, list(ideas.values()))
    return list(ideas.values())


async def _close_and_publish(session: AsyncSession, project_id: int, ideas: list[Idea]) -> None:
    gone = await _drop_empty_topics(session, project_id)
    await session.commit()
    for topic_id in gone:
        bus.publish(project_id, "idea_topic.deleted", {"id": topic_id})
    await publish(session, project_id, ideas)
    await _publish_linked(session, project_id, [i.id for i in ideas])


# --- for the manager (tools/ideas.py) ----------------------------------------------------------


async def count_open(session: AsyncSession, project_id: int) -> int:
    return await session.scalar(select(func.count(Idea.id)).where(Idea.project_id == project_id, Idea.status == IdeaStatus.OPEN)) or 0


async def manager_list(session: AsyncSession, project_id: int, *, topic: str | None, query: str | None, status: str, limit: int) -> str:
    stmt = select(Idea).where(Idea.project_id == project_id).order_by(Idea.topic_id, Idea.id)
    if status != "all":
        stmt = stmt.where(Idea.status == status)
    ideas = list((await session.scalars(stmt)).all())
    names = {t.id: t for t in await topics(session, project_id)}
    if topic:
        ideas = [i for i in ideas if i.topic_id in names and topic.casefold() in names[i.topic_id].name.casefold()]
    if query:
        ideas = [i for i in ideas if query.casefold() in f"{i.title} {i.text}".casefold()]
    if not ideas:
        return "No ideas."
    outs_by_id = {o.id: o for o in await outs(session, ideas[:limit])}
    lines: list[str] = []
    current: int | None = -1
    for idea in ideas[:limit]:
        if idea.topic_id != current:
            current = idea.topic_id
            t = names.get(current) if current is not None else None
            lines.append(f"{t.name}" + (f" ({t.hint})" if t.hint else "") if t else "No topic")
        out = outs_by_id[idea.id]
        marks = [f"similar {', '.join(f'i{s}' for s in out.similar)}"] if out.similar else []
        marks += [f"duplicate of i{out.duplicate_of}"] if out.duplicate_of else []
        marks += [idea.status + (f" #{out.task_number}" if out.task_number else "")] if idea.status != IdeaStatus.OPEN else []
        marks += ["open question"] if idea.question_status == QUESTION_OPEN else []
        lines.append(f"  {_idea_line(idea, 160)}" + (f" [{'; '.join(marks)}]" if marks else ""))
    more = f"\n(+{len(ideas) - limit} more)" if len(ideas) > limit else ""
    return "\n".join(lines) + more


async def manager_detail(session: AsyncSession, idea: Idea) -> str:
    (out,) = await outs(session, [idea])
    topic = await session.get(IdeaTopic, idea.topic_id) if idea.topic_id else None
    lines = [f"i{idea.id} {idea.title or '(no title)'} — {idea.status}" + (f" into #{out.task_number}" if out.task_number else "") + (f" (into i{idea.merged_into_id})" if idea.merged_into_id else "")]
    lines.append(f"Topic: {topic.name if topic else '-'}" + (f"; by {idea.author}" if idea.author else ""))
    if out.similar or out.duplicate_of:
        lines.append("Links: " + ", ".join([f"similar i{s}" for s in out.similar] + ([f"duplicate of i{out.duplicate_of}"] if out.duplicate_of else [])))
    if idea.closed_reason:
        lines.append(f"Reason: {idea.closed_reason}")
    if idea.question_status:
        lines.append(f"Question ({idea.question_status}): {idea.question}" + (f" → {idea.question_answer}" if idea.question_answer else ""))
    lines.append(f"\n{idea.text}")
    older = await versions(session, idea)
    if older:
        lines.append("\nEarlier versions (newest first):\n" + "\n".join(f"- {truncate((v.title + ': ' if v.title else '') + ' '.join(v.text.split()), 300)}" for v in older[:10]))
    return "\n".join(lines)
