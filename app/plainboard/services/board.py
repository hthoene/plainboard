"""Tickets, comments and attachments."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..events import bus
from ..models import (
    ACTIVE_RUN_STATUSES,
    Agent,
    Assignment,
    AssignmentStatus,
    Attachment,
    Comment,
    Decision,
    DecisionStatus,
    Project,
    Run,
    Task,
    TaskStatus,
)
from ..schemas import AssignmentOut, AttachmentOut, CommentOut, DecisionOut, TaskCreate, TaskDetail, TaskOut, TaskUpdate
from .errors import NotFound, ServiceError

OPEN_ASSIGNMENT_STATUSES = (AssignmentStatus.QUEUED, AssignmentStatus.RUNNING)


async def _next_position(session: AsyncSession, project_id: int, status: str) -> float:
    current = await session.scalar(select(func.max(Task.position)).where(Task.project_id == project_id, Task.status == status))
    return (current or 0.0) + 1024.0


async def create_task(session: AsyncSession, project_id: int, data: TaskCreate, *, by_agent_id: int | None = None) -> Task:
    project = await session.get(Project, project_id)
    if project is None:
        raise NotFound("Project not found")
    project.task_counter += 1
    task = Task(
        project_id=project_id,
        number=project.task_counter,
        title=data.title,
        description=data.description,
        status=data.status,
        priority=data.priority,
        labels=data.labels,
        position=await _next_position(session, project_id, data.status),
        created_by_agent_id=by_agent_id,
    )
    session.add(task)
    await session.commit()
    await publish_task(session, task)
    return task


async def update_task(session: AsyncSession, task: Task, data: TaskUpdate) -> tuple[Task, str]:
    """Returns the task and its previous status."""
    previous = task.status
    fields = data.model_dump(exclude_unset=True)
    if "status" in fields and fields["status"] != previous and "position" not in fields:
        fields["position"] = await _next_position(session, task.project_id, fields["status"])
    for key, value in fields.items():
        setattr(task, key, value)
    await session.commit()
    await publish_task(session, task)
    return task, previous


async def delete_task(session: AsyncSession, task: Task) -> None:
    project_id, task_id = task.project_id, task.id
    for attachment in (await session.scalars(select(Attachment).where(Attachment.task_id == task_id))).all():
        _remove_file(attachment)
    await session.delete(task)
    await session.commit()
    bus.publish(project_id, "task.deleted", {"id": task_id})


async def get_task(session: AsyncSession, task_id: int) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFound("Ticket not found")
    return task


async def get_task_by_number(session: AsyncSession, project_id: int, number: int) -> Task:
    task = await session.scalar(select(Task).where(Task.project_id == project_id, Task.number == number))
    if task is None:
        raise NotFound(f"Ticket #{number} not found")
    return task


async def list_tasks(
    session: AsyncSession, project_id: int, *, status: str | None = None, query: str | None = None, limit: int | None = None
) -> list[Task]:
    q = select(Task).where(Task.project_id == project_id)
    if status:
        q = q.where(Task.status == status)
    if query:
        like = f"%{query}%"
        q = q.where(or_(Task.title.ilike(like), Task.description.ilike(like)))
    q = q.order_by(Task.status, Task.position)
    if limit:
        q = q.limit(limit)
    return list((await session.scalars(q)).all())


async def task_outs(session: AsyncSession, tasks: list[Task]) -> list[TaskOut]:
    if not tasks:
        return []
    ids = [t.id for t in tasks]
    comments = dict((await session.execute(select(Comment.task_id, func.count()).where(Comment.task_id.in_(ids)).group_by(Comment.task_id))).all())
    files = dict((await session.execute(select(Attachment.task_id, func.count()).where(Attachment.task_id.in_(ids)).group_by(Attachment.task_id))).all())
    decisions = dict(
        (await session.execute(select(Decision.task_id, func.count()).where(Decision.task_id.in_(ids), Decision.status == DecisionStatus.OPEN).group_by(Decision.task_id))).all()
    )
    active: dict[int, set[int]] = {}
    for task_id, agent_id in (
        await session.execute(
            select(Assignment.task_id, Assignment.agent_id).where(Assignment.task_id.in_(ids), Assignment.status.in_(OPEN_ASSIGNMENT_STATUSES))
        )
    ).all():
        active.setdefault(task_id, set()).add(agent_id)
    manager_busy = set(
        (
            await session.scalars(
                select(Run.task_id).join(Agent, Agent.id == Run.agent_id).where(
                    Run.task_id.in_(ids), Run.status.in_(ACTIVE_RUN_STATUSES), Agent.is_manager.is_(True)
                )
            )
        ).all()
    )
    out = []
    for task in tasks:
        item = TaskOut.model_validate(task)
        item.comment_count = comments.get(task.id, 0)
        item.attachment_count = files.get(task.id, 0)
        item.active_agent_ids = sorted(active.get(task.id, set()))
        item.manager_active = task.id in manager_busy
        item.open_decisions = decisions.get(task.id, 0)
        out.append(item)
    return out


async def task_detail(session: AsyncSession, task: Task) -> TaskDetail:
    (base,) = await task_outs(session, [task])
    detail = TaskDetail.model_validate({**dict(base), "comments": [], "attachments": [], "assignments": [], "decisions": []})
    detail.decisions = [DecisionOut.model_validate(d) for d in await list_decisions(session, task.id)]
    detail.comments = [CommentOut.model_validate(c) for c in (await session.scalars(select(Comment).where(Comment.task_id == task.id).order_by(Comment.id))).all()]
    detail.attachments = [AttachmentOut.model_validate(a) for a in await list_attachments(session, task.id)]
    detail.assignments = [
        AssignmentOut.model_validate(a)
        for a in (await session.scalars(select(Assignment).where(Assignment.task_id == task.id).order_by(Assignment.id))).all()
    ]
    return detail


async def publish_task(session: AsyncSession, task: Task) -> None:
    (out,) = await task_outs(session, [task])
    bus.publish(task.project_id, "task.upserted", out)


async def publish_task_id(session: AsyncSession, task_id: int | None) -> None:
    if task_id is None:
        return
    task = await session.get(Task, task_id)
    if task is not None:
        await session.refresh(task)
        await publish_task(session, task)


# --- comments -------------------------------------------------------------------------------


async def add_comment(
    session: AsyncSession, task: Task, body: str, *, author_kind: str = "user", agent_id: int | None = None
) -> Comment:
    comment = Comment(task_id=task.id, body=body, author_kind=author_kind, author_agent_id=agent_id)
    session.add(comment)
    await session.commit()
    bus.publish(task.project_id, "comment.created", CommentOut.model_validate(comment))
    await publish_task(session, task)
    return comment


async def recent_comments(session: AsyncSession, task_id: int, limit: int = 5) -> list[Comment]:
    q = select(Comment).where(Comment.task_id == task_id).order_by(Comment.id.desc()).limit(limit)
    return list(reversed((await session.scalars(q)).all()))


# --- attachments ----------------------------------------------------------------------------


def storage_path(attachment: Attachment) -> Path:
    return get_config().uploads_dir / attachment.storage_name


def runner_path(attachment: Attachment) -> str:
    """Path of the file inside the runner container (uploads volume mounted read-only)."""
    return f"{get_config().runner_uploads_dir}/{attachment.storage_name}"


def _remove_file(attachment: Attachment) -> None:
    storage_path(attachment).unlink(missing_ok=True)


async def add_attachment(
    session: AsyncSession, task: Task, filename: str, content_type: str, data: bytes, *, agent_id: int | None = None
) -> Attachment:
    max_bytes = get_config().max_upload_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise ServiceError(f"File too large (max {get_config().max_upload_mb} MB)", 413)
    suffix = Path(filename).suffix[:16]
    attachment = Attachment(
        project_id=task.project_id,
        task_id=task.id,
        filename=Path(filename).name[:300] or "file",
        content_type=content_type or "application/octet-stream",
        size=len(data),
        storage_name=f"{uuid.uuid4().hex}{suffix}",
        uploaded_by_agent_id=agent_id,
    )
    path = storage_path(attachment)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    session.add(attachment)
    await session.commit()
    bus.publish(task.project_id, "attachment.created", AttachmentOut.model_validate(attachment))
    await publish_task(session, task)
    return attachment


async def list_attachments(session: AsyncSession, task_id: int) -> list[Attachment]:
    return list((await session.scalars(select(Attachment).where(Attachment.task_id == task_id).order_by(Attachment.id))).all())


async def get_attachment(session: AsyncSession, attachment_id: int) -> Attachment:
    attachment = await session.get(Attachment, attachment_id)
    if attachment is None:
        raise NotFound("Attachment not found")
    return attachment


async def delete_attachment(session: AsyncSession, attachment: Attachment) -> None:
    project_id, attachment_id, task_id = attachment.project_id, attachment.id, attachment.task_id
    _remove_file(attachment)
    await session.delete(attachment)
    await session.commit()
    bus.publish(project_id, "attachment.deleted", {"id": attachment_id, "task_id": task_id})
    await publish_task_id(session, task_id)


async def list_decisions(session: AsyncSession, task_id: int) -> list[Decision]:
    return list((await session.scalars(select(Decision).where(Decision.task_id == task_id).order_by(Decision.id))).all())


def is_open(task: Task) -> bool:
    return task.status not in (TaskStatus.DONE,)


async def ticket_text(session: AsyncSession, task: Task, comment_limit: int = 5) -> str:
    """Compact ticket description for prompts and tools."""
    from ..orchestration.prompts import ticket_brief

    agents = (await session.scalars(select(Agent).where(Agent.project_id == task.project_id))).all()
    names = {a.id: a.name for a in agents}
    decisions = await list_decisions(session, task.id)
    return ticket_brief(task, await list_attachments(session, task.id), await recent_comments(session, task.id, comment_limit), names, runner_path, decisions)
