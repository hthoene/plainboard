"""Tickets, comments and attachments."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
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
    Job,
    JobStatus,
    Project,
    Run,
    Task,
    TaskStatus,
    TaskTransition,
    utcnow,
)
from ..schemas import AssignmentOut, AttachmentOut, CommentOut, DecisionOut, PublicBoard, PublicTask, TaskCreate, TaskDetail, TaskOut, TaskUpdate
from .errors import NotFound, ServiceError

OPEN_ASSIGNMENT_STATUSES = (AssignmentStatus.QUEUED, AssignmentStatus.RUNNING)


async def _next_position(session: AsyncSession, project_id: int, status: str) -> float:
    current = await session.scalar(select(func.max(Task.position)).where(Task.project_id == project_id, Task.status == status))
    return (current or 0.0) + 1024.0


async def create_task(session: AsyncSession, project_id: int, data: TaskCreate, *, by_agent_id: int | None = None) -> Task:
    (task,) = await create_tasks(session, project_id, [data], by_agent_id=by_agent_id)
    return task


async def create_tasks(session: AsyncSession, project_id: int, items: list[TaskCreate], *, by_agent_id: int | None = None) -> list[Task]:
    project = await session.get(Project, project_id)
    if project is None:
        raise NotFound("Project not found")
    tasks = []
    for data in items:
        project.task_counter += 1
        task = Task(
            project_id=project_id,
            number=project.task_counter,
            title=data.title,
            description=data.description,
            customer_title=data.customer_title,
            customer_description=data.customer_description,
            status=data.status,
            priority=data.priority,
            labels=data.labels,
            position=await _next_position(session, project_id, data.status),
            created_by_agent_id=by_agent_id,
        )
        session.add(task)
        session.add(TaskTransition(project_id=project_id, task_number=task.number, from_status=None, status=task.status))
        await session.flush()  # the next ticket's position counts this one
        tasks.append(task)
    await session.commit()
    for task in tasks:
        await publish_task(session, task)
    return tasks


async def public_board(session: AsyncSession, project: Project) -> PublicBoard:
    """What customers may see: only tickets with a customer title, and nothing else about them."""
    tasks = [t for t in await list_tasks(session, project.id) if t.customer_title.strip()]
    return PublicBoard(
        project=project.name,
        tasks=[
            PublicTask(number=t.number, title=t.customer_title.strip(), description=t.customer_description.strip(), status=t.status, updated_at=t.updated_at)
            for t in tasks
        ],
    )


async def update_task(session: AsyncSession, task: Task, data: TaskUpdate) -> tuple[Task, str]:
    """Returns the task and its previous status."""
    previous = await _apply_update(session, task, data.model_dump(exclude_unset=True))
    await session.commit()
    await publish_task(session, task)
    return task, previous


async def update_tasks(
    session: AsyncSession,
    project_id: int,
    numbers: list[int],
    data: TaskUpdate,
    *,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> tuple[list[Task], list[int]]:
    """Applies the same change to several tickets at once. Returns the updated tickets and the numbers not found."""
    found = await _by_number(session, project_id, numbers)
    tasks = [found[n] for n in dict.fromkeys(numbers) if n in found]
    removed = {label.lower() for label in remove_labels or []}
    for task in tasks:
        fields = data.model_dump(exclude_unset=True)
        if add_labels or remove_labels:
            labels = [label for label in fields.get("labels", task.labels) if label.lower() not in removed]
            have = {label.lower() for label in labels}
            fields["labels"] = labels + [label for label in add_labels or [] if label.lower() not in have]
        await _apply_update(session, task, fields)
        await session.flush()  # the next ticket moved into the same column goes below this one
    await session.commit()
    for task in tasks:
        await publish_task(session, task)
    return tasks, [n for n in dict.fromkeys(numbers) if n not in found]


async def _apply_update(session: AsyncSession, task: Task, fields: dict) -> str:
    previous = task.status
    if "status" in fields and fields["status"] != previous and "position" not in fields:
        fields["position"] = await _next_position(session, task.project_id, fields["status"])
    archived = fields.pop("archived", None)
    if archived is not None:
        task.archived_at = (task.archived_at or utcnow()) if archived else None
    elif fields.get("status", previous) != previous:
        task.archived_at = None  # moved again: back on the board
    for key, value in fields.items():
        setattr(task, key, value)
    if task.status != previous:
        session.add(TaskTransition(project_id=task.project_id, task_number=task.number, from_status=previous, status=task.status))
    return previous


async def sort_column(session: AsyncSession, project_id: int, status: str, numbers: list[int]) -> None:
    """Puts the given tickets at the top of a column in this order; the others keep theirs below them."""
    tasks = await list_tasks(session, project_id, status=status)
    by_number = {t.number: t for t in tasks}
    if missing := [n for n in numbers if n not in by_number]:
        raise ServiceError(f"Not in {status}: {', '.join(f'#{n}' for n in missing)}")
    first = list(dict.fromkeys(numbers))
    ordered = [by_number[n] for n in first] + [t for t in tasks if t.number not in first]
    changed = []
    for index, task in enumerate(ordered, 1):
        if task.position != index * 1024.0:
            task.position = index * 1024.0
            changed.append(task)
    await session.commit()
    for task in changed:
        await publish_task(session, task)


async def archive_tasks(session: AsyncSession, project_id: int, *, done_before: datetime | None = None) -> int:
    """Takes done tickets off the board (all, or those finished before a time). Returns how many."""
    q = select(Task).where(Task.project_id == project_id, Task.status == TaskStatus.DONE, Task.archived_at.is_(None))
    if done_before:
        q = q.where(Task.updated_at < done_before)
    tasks = (await session.scalars(q)).all()
    if not tasks:
        return 0
    now = utcnow()
    for task in tasks:
        task.archived_at = now
    await session.commit()
    bus.publish(project_id, "task.archived", {"ids": [t.id for t in tasks]})
    return len(tasks)


async def auto_archive(session: AsyncSession) -> int:
    """Archives done tickets of every project once its auto_archive_days have passed."""
    archived = 0
    for project in (await session.scalars(select(Project).where(Project.auto_archive_days > 0))).all():
        archived += await archive_tasks(session, project.id, done_before=utcnow() - timedelta(days=project.auto_archive_days))
    return archived


async def finish_user_action(session: AsyncSession, task: Task, note: str = "") -> str:
    """The user did what the ticket waited for. Returns what that was."""
    action = task.user_action
    if not action:
        raise ServiceError("This ticket is not waiting for you")
    task.user_action = ""
    body = f"Done: {action}" + (f"\n\n{note.strip()}" if note.strip() else "")
    await add_comment(session, task, body)
    return action


async def delete_task(session: AsyncSession, task: Task) -> None:
    await delete_tasks(session, [task])


async def delete_tasks(session: AsyncSession, tasks: list[Task]) -> None:
    for task in tasks:
        for attachment in (await session.scalars(select(Attachment).where(Attachment.task_id == task.id))).all():
            _remove_file(attachment)
        await session.delete(task)
    deleted = [(t.project_id, t.id) for t in tasks]
    await session.commit()
    for project_id, task_id in deleted:
        bus.publish(project_id, "task.deleted", {"id": task_id})


async def _by_number(session: AsyncSession, project_id: int, numbers: list[int]) -> dict[int, Task]:
    return {t.number: t for t in await session.scalars(select(Task).where(Task.project_id == project_id, Task.number.in_(numbers)))}


async def get_tasks_by_number(session: AsyncSession, project_id: int, numbers: list[int]) -> list[Task]:
    tasks = await _by_number(session, project_id, numbers)
    if missing := [n for n in dict.fromkeys(numbers) if n not in tasks]:
        raise NotFound(f"Not found: {', '.join(f'#{n}' for n in missing)}")
    return [tasks[n] for n in dict.fromkeys(numbers)]


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


async def idle_in_progress(session: AsyncSession, project_id: int) -> list[Task]:
    """Tickets in progress that nothing is happening on and that aren't waiting for the user."""
    tasks = [t for t in await list_tasks(session, project_id, status=TaskStatus.IN_PROGRESS) if not t.user_action]
    if not tasks:
        return []
    with_job = select(Job.agent_id).where(Job.project_id == project_id, Job.status == JobStatus.RUNNING)
    busy = {
        *await session.scalars(select(Run.task_id).where(Run.project_id == project_id, Run.status.in_(ACTIVE_RUN_STATUSES))),
        *await session.scalars(select(Decision.task_id).where(Decision.project_id == project_id, Decision.status == DecisionStatus.OPEN)),
        # A job outlives its assignment's report and still belongs to that work.
        *await session.scalars(select(Assignment.task_id).where(Assignment.project_id == project_id, Assignment.agent_id.in_(with_job))),
    }
    return [t for t in tasks if t.id not in busy]


async def list_tasks(
    session: AsyncSession,
    project_id: int,
    *,
    status: str | None = None,
    query: str | None = None,
    priority: str | None = None,
    label: str | None = None,
    archived: bool = False,
    limit: int | None = None,
) -> list[Task]:
    q = select(Task).where(Task.project_id == project_id, Task.archived_at.is_not(None) if archived else Task.archived_at.is_(None))
    if status:
        q = q.where(Task.status == status)
    if priority:
        q = q.where(Task.priority == priority)
    if query:
        like = f"%{query}%"
        q = q.where(or_(Task.title.ilike(like), Task.description.ilike(like)))
    q = q.order_by(Task.archived_at.desc(), Task.number.desc()) if archived else q.order_by(Task.status, Task.position)
    if label:  # labels are a JSON list: filtered here, so the limit applies afterwards
        tasks = [t for t in (await session.scalars(q)).all() if label.lower() in (x.lower() for x in t.labels)]
        return tasks[:limit] if limit else tasks
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


async def store_attachment(
    session: AsyncSession, project_id: int, filename: str, content_type: str, data: bytes, *, task_id: int | None = None, agent_id: int | None = None
) -> Attachment:
    max_bytes = get_config().max_upload_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise ServiceError(f"File too large (max {get_config().max_upload_mb} MB)", 413)
    suffix = Path(filename).suffix[:16]
    attachment = Attachment(
        project_id=project_id,
        task_id=task_id,
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
    return attachment


async def add_attachment(
    session: AsyncSession, task: Task, filename: str, content_type: str, data: bytes, *, agent_id: int | None = None
) -> Attachment:
    attachment = await store_attachment(session, task.project_id, filename, content_type, data, task_id=task.id, agent_id=agent_id)
    bus.publish(task.project_id, "attachment.created", AttachmentOut.model_validate(attachment))
    await publish_task(session, task)
    return attachment


async def list_attachments(session: AsyncSession, task_id: int) -> list[Attachment]:
    return list((await session.scalars(select(Attachment).where(Attachment.task_id == task_id).order_by(Attachment.id))).all())


async def chat_attachments(session: AsyncSession, project_id: int, ids: list[int]) -> list[Attachment]:
    """Files of a chat message (they belong to the project, not to a ticket)."""
    q = select(Attachment).where(Attachment.project_id == project_id, Attachment.task_id.is_(None), Attachment.id.in_(ids))
    return list((await session.scalars(q.order_by(Attachment.id))).all())


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
