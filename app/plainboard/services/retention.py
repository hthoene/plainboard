"""Files of done tickets are deleted after a project's retention (#97); by default never.

``Project.attachment_retention_days`` (0: never, the default) counts from the time a ticket was last moved
to done (``TaskTransition``; for older tickets the time it last changed): reopened, it starts over when done again.
Only the status counts: a ticket in any other status, archived or not, is never touched. ``attachment_retention_min_mb``: only files from this size.
Only the file goes: the attachment keeps its name and size and says when and why (``purged_at``,
``purged_after_days``). A file still in use stays: named by an open decision, shown in a chat (send_files),
or named by its file name in a note, an own note, an idea, or the text or comments of an open ticket.
Each deletion is in the work log; the project's manager hears one line per run.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..models import Agent, AgentNote, Attachment, Comment, Conversation, Decision, DecisionStatus, Idea, Message, Note, Project, Task, TaskStatus, TaskTransition
from ..schemas import RetentionCount, RetentionPreviewOut
from . import flow_log
from .util import format_bytes

log = logging.getLogger(__name__)

SOON_DAYS = 30  # the preview also says what goes within this many days
MB = 1024 * 1024
CHUNK = 50  # file names looked for in one text query


@dataclass
class Due:
    attachment: Attachment
    task_number: int
    at: datetime  # when its retention ends


def _done_since(task: Task, last_done: dict[int, datetime]) -> datetime:
    return last_done.get(task.number) or task.updated_at


async def _due(session: AsyncSession, project_id: int, days: int, min_mb: int, until: datetime) -> list[Due]:
    """Files of done tickets whose retention ends by ``until`` and that nothing else uses."""
    if days <= 0:
        return []
    tasks = {t.id: t for t in await session.scalars(select(Task).where(Task.project_id == project_id, Task.status == TaskStatus.DONE))}
    if not tasks:
        return []
    rows = await session.execute(
        select(TaskTransition.task_number, func.max(TaskTransition.at))
        .where(TaskTransition.project_id == project_id, TaskTransition.status == TaskStatus.DONE)
        .group_by(TaskTransition.task_number)
    )
    last_done = {number: at for number, at in rows.all()}
    q = select(Attachment).where(Attachment.project_id == project_id, Attachment.task_id.in_(list(tasks)), Attachment.purged_at.is_(None))
    if min_mb > 0:
        q = q.where(Attachment.size >= min_mb * MB)
    found: list[Due] = []
    for a in await session.scalars(q.order_by(Attachment.id)):
        task = tasks[a.task_id]
        at = _done_since(task, last_done) + timedelta(days=days)
        if at <= until:
            found.append(Due(a, task.number, at))
    if not found:
        return []
    used = await _in_use(session, project_id, [d.attachment for d in found])
    return [d for d in found if d.attachment.id not in used]


def _ids(values: Iterable[int]) -> set[int]:
    return {int(v) for v in values if isinstance(v, int) or (isinstance(v, str) and v.isdigit())}


async def _in_use(session: AsyncSession, project_id: int, attachments: list[Attachment]) -> set[int]:
    """Which of these files are still used outside their done ticket."""
    used: set[int] = set()
    for d in await session.scalars(select(Decision).where(Decision.project_id == project_id, Decision.status == DecisionStatus.OPEN)):
        used |= _ids(d.attachment_ids or [])
        for option in d.options or []:
            used |= _ids(option.get("attachment_ids") or [])
    chats = select(Conversation.id).join(Agent, Agent.id == Conversation.agent_id).where(Agent.project_id == project_id)
    for meta in await session.scalars(select(Message.meta).where(Message.kind == "files", Message.conversation_id.in_(chats))):
        used |= _ids(item.get("id") for item in (meta or {}).get("attachments", []))
    rest = [a for a in attachments if a.id not in used]
    open_tasks = select(Task.id).where(Task.project_id == project_id, Task.status != TaskStatus.DONE)
    texts = (
        (Note.content, Note.project_id == project_id),
        (AgentNote.text, AgentNote.project_id == project_id),
        (Idea.text, Idea.project_id == project_id),
        (Task.description, Task.id.in_(open_tasks)),
        (Comment.body, Comment.task_id.in_(open_tasks)),
        (Decision.details, Decision.project_id == project_id, Decision.status == DecisionStatus.OPEN),
    )
    for start in range(0, len(rest), CHUNK):
        names = {a.storage_name: a.id for a in rest[start : start + CHUNK]}
        for column, *where in texts:
            hit = select(column).where(*where, or_(*(column.contains(n, autoescape=True) for n in names)))
            for text in await session.scalars(hit):
                used |= {i for n, i in names.items() if n in (text or "")}
    return used


async def preview(session: AsyncSession, project: Project, days: int, min_mb: int, now: datetime) -> RetentionPreviewOut:
    """What this setting would delete: at the next run, and within ``SOON_DAYS`` after it."""
    due = await _due(session, project.id, days, min_mb, now + timedelta(days=SOON_DAYS))
    at_once = [d for d in due if d.at <= now]
    later = [d for d in due if d.at > now]
    return RetentionPreviewOut(
        now=RetentionCount(count=len(at_once), bytes=sum(d.attachment.size for d in at_once)),
        soon=RetentionCount(count=len(later), bytes=sum(d.attachment.size for d in later)),
        soon_days=SOON_DAYS,
    )


def notice(count: int, size: int, days: int) -> str:
    return (
        f"Attachment retention: the files of {count} attachment{'s' if count != 1 else ''} ({format_bytes(size)}) of tickets done more than "
        f"{days} days ago were deleted (project setting). Their entries stay on the tickets with name and size."
    )


async def purge(session: AsyncSession, now: datetime) -> dict[int, list[Due]]:
    """Delete the files whose retention ended, in every project that has one. Per project what went."""
    out: dict[int, list[Due]] = {}
    uploads = get_config().uploads_dir
    for project in list(await session.scalars(select(Project).where(Project.attachment_retention_days > 0))):
        days = project.attachment_retention_days
        due = await _due(session, project.id, days, project.attachment_retention_min_mb, now)
        if not due:
            continue
        for d in due:
            a = d.attachment
            try:
                (uploads / a.storage_name).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Attachment %s (%s) not deleted: %s", a.id, a.filename, exc)
                continue
            a.purged_at = now
            a.purged_after_days = days
            log.info("Retention: deleted the file of attachment %s %r (%d bytes) of ticket #%d in project %s", a.id, a.filename, a.size, d.task_number, project.id)
            await flow_log.record(
                session, project.id, "attachment.purged", commit=False, task_id=a.task_id, attachment=a.id, task=d.task_number, filename=a.filename, size=a.size, days=days
            )
        await session.commit()
        if done := [d for d in due if d.attachment.purged_at == now]:
            out[project.id] = done
    return out


async def purged_names(session: AsyncSession, project_ids: list[int]) -> list[str]:
    """The files of these projects that retention deleted: an external runner's copies go too."""
    if not project_ids:
        return []
    q = select(Attachment.storage_name).where(Attachment.project_id.in_(project_ids), Attachment.purged_at.is_not(None))
    return list(await session.scalars(q))
