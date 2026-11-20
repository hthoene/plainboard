"""Tickets, comments and attachments."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import func, or_, select, update
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
    Landing,
    LandingStatus,
    Project,
    Repository,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    TaskTransition,
    Train,
    TrainStatus,
    utcnow,
)
from ..schemas import AssignmentOut, AttachmentOut, CommentOut, DecisionOut, PublicBoard, PublicTask, TaskCreate, TaskDetail, TaskOut, TaskUpdate
from . import flow_log, ticket_state
from .errors import NotFound, ServiceError
from .timing import duration
from .util import truncate

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


async def unmarked_in_review(session: AsyncSession, project_id: int) -> list[Task]:
    """Tickets in review that don't say what the user should look at: no user action, no open question."""
    tasks = [t for t in await list_tasks(session, project_id, status=TaskStatus.REVIEW) if not t.user_action]
    if not tasks:
        return []
    asked = set(await session.scalars(select(Decision.task_id).where(Decision.project_id == project_id, Decision.status == DecisionStatus.OPEN)))
    return [t for t in tasks if t.id not in asked]


async def accept_reviews(session: AsyncSession) -> int:
    """Moves tickets to done that lay in review unchanged for the project's review_days. Tickets that
    wait for the user (user action, open question) or are being worked on stay."""
    accepted = 0
    for project in (await session.scalars(select(Project).where(Project.review_days > 0))).all():
        cutoff = utcnow() - timedelta(days=project.review_days)
        working = set(await session.scalars(select(Run.task_id).where(Run.project_id == project.id, Run.status.in_(ACTIVE_RUN_STATUSES))))
        for task in await unmarked_in_review(session, project.id):
            if task.updated_at >= cutoff or task.id in working:
                continue
            await _apply_update(session, task, {"status": TaskStatus.DONE})
            await add_comment(session, task, f"Moved to done automatically: {project.review_days} days in review without a change.", author_kind="system")
            accepted += 1
    return accepted


async def finish_user_action(session: AsyncSession, task: Task, note: str = "", author_name: str | None = None) -> str:
    """The user did what the ticket waited for. Returns what that was."""
    action = task.user_action
    # Taken by whoever empties it: a second click while the first one is on its way tells the manager nothing twice.
    taken = await session.execute(update(Task).where(Task.id == task.id, Task.user_action == action, Task.user_action != "").values(user_action=""))
    if not taken.rowcount:
        await session.rollback()
        raise ServiceError("This ticket is not waiting for you")
    task.user_action = ""
    body = f"Done: {action}" + (f"\n\n{note.strip()}" if note.strip() else "")
    await add_comment(session, task, body, author_name=author_name)
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
    return await untouched(session, project_id, (TaskStatus.IN_PROGRESS,))


async def untouched(session: AsyncSession, project_id: int, statuses: tuple[str, ...]) -> list[Task]:
    """Tickets in these columns that nothing is happening on and that aren't waiting for the user:
    no turn, no open assignment, no branch on its way, no open question."""
    tasks = [t for status in statuses for t in await list_tasks(session, project_id, status=status) if not t.user_action]
    if not tasks:
        return []
    busy = {
        *await session.scalars(select(Run.task_id).where(Run.project_id == project_id, Run.status.in_(ACTIVE_RUN_STATUSES))),
        *await session.scalars(select(Decision.task_id).where(Decision.project_id == project_id, Decision.status == DecisionStatus.OPEN)),
        # An assignment stays open while it waits for its jobs, a branch is in flight until it has landed.
        *await session.scalars(select(Assignment.task_id).where(Assignment.project_id == project_id, Assignment.status.in_(OPEN_ASSIGNMENT_STATUSES))),
        *await session.scalars(select(Landing.task_id).where(Landing.project_id == project_id, Landing.status.in_(LANDING_IN_FLIGHT))),
    }
    return [t for t in tasks if t.id not in busy]


@dataclass
class Capacity:
    """Use of the parallel agents the user allows. All projects share the limit; managers don't count."""

    limit: int
    busy: int  # members with a turn working or waiting to start (not one that only waits for its own job, in a turn or between turns)
    waiting: list[str]  # this project's members whose turn has not started yet
    idle: list[str]  # this project's members with nothing to do: no turn and no open assignment
    open_work: bool  # tickets to do or in progress (and in the backlog, where the project goes on with it)

    @property
    def working(self) -> int:
        return min(self.busy, self.limit)

    @property
    def free(self) -> int:
        return max(self.limit - self.busy, 0)

    @property
    def over(self) -> int:
        return max(self.busy - self.limit, 0)


async def capacity(
    session: AsyncSession, project_id: int, limit: int, *, except_run_id: int, waiting: set[int] | frozenset[int] = frozenset(), backlog: bool = False
) -> Capacity:
    """The project's own use of its limit; other projects count by themselves.
    ``waiting``: turns that only wait for a job right now (RunManager.waiting_for_job): they take no slot.
    ``backlog``: the project goes on with its Backlog, so tickets there are open work too."""
    members = select(Agent.id).where(~Agent.is_manager)
    top_level = (Run.project_id == project_id, Run.status.in_(ACTIVE_RUN_STATUSES), Run.parent_run_id.is_(None), Run.id != except_run_id, Run.agent_id.in_(members))
    rows = [(a, st, p) for run_id, a, st, p in (await session.execute(select(Run.id, Run.agent_id, Run.status, Run.project_id).where(*top_level))).all() if run_id not in waiting]
    running = {agent_id for agent_id, status, _project in rows if status == RunStatus.RUNNING}
    open_assignment = set(await session.scalars(select(Assignment.agent_id).where(Assignment.status == AssignmentStatus.RUNNING)))
    # A member that waits for its own job takes no slot, and its next assignment waits for that job, not for a slot.
    queued = {agent_id for agent_id, status, _project in rows if status == RunStatus.QUEUED and (agent_id in running or agent_id not in open_assignment)}
    team = list(await session.scalars(select(Agent).where(Agent.project_id == project_id, Agent.enabled, ~Agent.is_manager).order_by(Agent.id)))
    active = {agent_id for agent_id, _status, project in rows if project == project_id}
    columns = (TaskStatus.TODO, TaskStatus.IN_PROGRESS, *((TaskStatus.BACKLOG,) if backlog else ()))
    open_tickets = select(Task.id).where(Task.project_id == project_id, Task.archived_at.is_(None), Task.status.in_(columns))
    return Capacity(
        limit=limit,
        busy=len(running | queued),
        waiting=[a.name for a in team if a.id in queued - running],
        idle=[a.name for a in team if a.id not in active and a.id not in open_assignment],
        open_work=bool(await session.scalar(open_tickets.limit(1))),
    )


IN_FLIGHT_LINES = 12
LANDING_IN_FLIGHT = (LandingStatus.WAITING, LandingStatus.TRAIN, LandingStatus.BACK)
LONG_JOB_SECONDS = 2 * 3600  # an assignment waiting this long for one job may be stuck: its agent takes no other work meanwhile


async def in_flight(session: AsyncSession, project_id: int, now: datetime | None = None) -> tuple[list[str], int]:
    """Open assignments and branches on their way to the base branch, one line each, and how many more there are. The
    manager gets this on every turn: what is already ordered comes from here, not from its memory."""
    now = now or utcnow()
    names = await agent_names(session, project_id)
    assignments = list(await session.scalars(select(Assignment).where(Assignment.project_id == project_id, Assignment.status.in_(OPEN_ASSIGNMENT_STATUSES)).order_by(Assignment.id)))
    landings = list(await session.scalars(select(Landing).where(Landing.project_id == project_id, Landing.status.in_(LANDING_IN_FLIGHT)).order_by(Landing.id)))
    trains = {t.id: t for t in await session.scalars(select(Train).where(Train.id.in_([x.train_id for x in landings if x.train_id])))} if landings else {}
    task_ids = {x.task_id for x in (*assignments, *landings) if x.task_id}
    numbers = dict((await session.execute(select(Task.id, Task.number).where(Task.id.in_(task_ids)))).all()) if task_ids else {}
    ids = [a.id for a in assignments]
    turns: dict[int, str] = {}
    jobs: dict[int, datetime] = {}
    paused = set(await session.scalars(select(Agent.id).where(Agent.project_id == project_id, ~Agent.enabled))) if ids else set()
    if ids:
        for assignment_id, status in (await session.execute(select(Run.assignment_id, Run.status).where(Run.assignment_id.in_(ids), Run.status.in_(ACTIVE_RUN_STATUSES)))).all():
            if status == RunStatus.RUNNING or assignment_id not in turns:
                turns[assignment_id] = status
        started = select(Job.assignment_id, func.min(Job.created_at)).where(Job.assignment_id.in_(ids), Job.status == JobStatus.RUNNING).group_by(Job.assignment_id)
        jobs = dict((await session.execute(started)).all())

    def age(since: datetime) -> str:
        return duration((now - since).total_seconds())

    lines = []
    for a in assignments:
        if turns.get(a.id) == RunStatus.RUNNING:
            state = "working"
        elif a.agent_id in paused:
            state = "paused with its agent; it goes on by itself when the agent is enabled again"
        elif a.status == AssignmentStatus.QUEUED:
            state = "not started yet"
        elif a.id in jobs:
            long = (now - jobs[a.id]).total_seconds() >= LONG_JOB_SECONDS
            state = f"waiting for its own job ({age(jobs[a.id])}" + ("; if it hangs: stop_job)" if long else ")")
        else:
            state = "continues with its next turn"
        ticket = f" for #{numbers[a.task_id]}" if a.task_id in numbers else ""
        lines.append(f'- Assignment #{a.id}{ticket}, {names.get(a.agent_id, "?")}: "{truncate(a.title, 70)}" · {state}, ordered {age(a.created_at)} ago')
    for landing in landings:
        ticket = f" for #{numbers[landing.task_id]}" if landing.task_id in numbers else ""
        train = trains.get(landing.train_id or 0)
        if landing.status == LandingStatus.BACK:
            state = "did not pass the checks, it is being repaired"
        elif landing.status == LandingStatus.TRAIN and train and train.status == TrainStatus.BLOCKED:
            state = f"passed its checks, waits for pull request #{train.number} to be merged by hand"
        elif landing.status == LandingStatus.TRAIN and train:
            state = f"being checked with {train.size - 1} other branch{'es' if train.size != 2 else ''} ({age(train.created_at)})" if train.size > 1 else f"being checked ({age(train.created_at)})"
        else:
            state = "goes on the next train"
        lines.append(f'- Branch {truncate(landing.branch, 60)}{ticket}, {names.get(landing.agent_id or 0, "?")}: {state}; PlainBoard lands it')
    return lines[:IN_FLIGHT_LINES], max(len(lines) - IN_FLIGHT_LINES, 0)


async def red_bases(session: AsyncSession, project_id: int, now: datetime | None = None) -> list[str]:
    """For the manager's turn: base branches that fail their checks, however long ago that began, and whether
    finished branches wait for the repair. From the database, like everything in flight: who repairs it, and whether anybody does."""
    from . import landings as landing_service

    now = now or utcnow()
    lines = []
    for repo in list(await session.scalars(select(Repository).where(Repository.project_id == project_id).order_by(Repository.id))):
        red = await landing_service.red_now(session, repo.id)
        if red is None:
            continue
        fixes = [a.id for a in red.repairs]
        waiting = list(await session.scalars(select(Landing).where(Landing.repository_id == repo.id, Landing.status == LandingStatus.WAITING)))
        held = sum(1 for w in waiting if w.assignment_id not in fixes)
        open_repairs = [a for a in red.repairs if a.status in OPEN_ASSIGNMENT_STATUSES]
        checking = await session.scalar(select(Train.id).where(Train.repository_id == repo.id, Train.direct, Train.status == TrainStatus.CHECKING).limit(1))
        if open_repairs:
            repair = f"Assignment #{open_repairs[0].id} repairs it"
        elif any(w.assignment_id in fixes for w in waiting):
            repair = "Its fix is handed over and lands next"
        elif checking:
            repair = "The checks run again with what landed since"
        else:
            repair = "Nobody repairs it: delegate the repair with base_repair=true, so its branch lands while the others wait"
        checks = ", ".join(red.train.failed_checks.split("\n")[:4]) or "its checks"
        waits = (
            f"Finished branches wait for its repair ({held} now), at most {duration((red.until - now).total_seconds())} more; then they land as they are."
            if now < red.until
            else "Finished branches land again as they are, on a red base."
        )
        lines.append(f"{red.train.base} of {repo.name} has failed its checks for {duration((now - red.since).total_seconds())} ({truncate(checks, 120)}). {waits} {repair}.")
    return lines


async def open_assignments(session: AsyncSession, task_id: int) -> list[Assignment]:
    q = select(Assignment).where(Assignment.task_id == task_id, Assignment.status.in_(OPEN_ASSIGNMENT_STATUSES)).order_by(Assignment.id)
    return list((await session.scalars(q)).all())


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
    if state := await ticket_state.get(session, task.id):
        detail.state, detail.state_updated_at = state.text, state.updated_at
    # Only the newest comments; older ones are paged in via GET /tasks/{id}/comments.
    detail.comments = [CommentOut.model_validate(c) for c in await recent_comments(session, task.id, UI_COMMENT_PAGE)]
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
    session: AsyncSession, task: Task, body: str, *, author_kind: str = "user", agent_id: int | None = None, author_name: str | None = None
) -> Comment:
    comment = Comment(task_id=task.id, body=body, author_kind=author_kind, author_agent_id=agent_id, author_name=author_name)
    session.add(comment)
    await session.commit()
    bus.publish(task.project_id, "comment.created", CommentOut.model_validate(comment))
    await publish_task(session, task)
    return comment


COMMENT_PAGE = 10  # what agents see by default
UI_COMMENT_PAGE = 20  # what the ticket view loads at first


async def recent_comments(session: AsyncSession, task_id: int, limit: int = COMMENT_PAGE, *, offset: int = 0, before: int | None = None) -> list[Comment]:
    """The newest `limit` comments (after skipping `offset` newer ones, or older than id `before`), oldest first."""
    q = select(Comment).where(Comment.task_id == task_id)
    if before is not None:
        q = q.where(Comment.id < before)
    q = q.order_by(Comment.id.desc()).offset(offset).limit(limit)
    return list(reversed((await session.scalars(q)).all()))


async def count_comments(session: AsyncSession, task_id: int) -> int:
    return await session.scalar(select(func.count()).select_from(Comment).where(Comment.task_id == task_id)) or 0


# --- attachments ----------------------------------------------------------------------------


def storage_path(attachment: Attachment) -> Path:
    return get_config().uploads_dir / attachment.storage_name


def runner_path(attachment: Attachment) -> str:
    """Path of the file inside the runner container (uploads volume mounted read-only)."""
    return f"{get_config().runner_uploads_dir}/{attachment.storage_name}"


def _remove_file(attachment: Attachment) -> None:
    storage_path(attachment).unlink(missing_ok=True)


# What an attachment may be shown as in the browser: told by its first bytes, not by what the uploader
# said. Everything else is only downloaded (HTML, SVG, XML, scripts...): it would run on PlainBoard's origin.
_SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"%PDF-", "application/pdf"))
_TEXT_SUFFIXES = {".txt", ".log", ".md", ".markdown", ".csv", ".json", ".diff", ".patch"}  # shown as plain text, never as a page


def inline_type(path: Path, filename: str) -> str | None:
    """The type an attachment is shown inline as; None: it is downloaded only."""
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    for signature, kind in _SIGNATURES:
        if head.startswith(signature):
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if Path(filename).suffix.lower() in _TEXT_SUFFIXES:
        return "text/plain; charset=utf-8"
    return None


def _write_limited(path: Path, source: bytes | BinaryIO, max_bytes: int) -> int:
    """Writes the file without holding it in memory; stops at the limit. Returns its size."""
    if isinstance(source, bytes):
        if len(source) > max_bytes:
            raise ServiceError(f"File too large (max {get_config().max_upload_mb} MB)", 413)
        path.write_bytes(source)
        return len(source)
    size = 0
    try:
        with path.open("wb") as out:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ServiceError(f"File too large (max {get_config().max_upload_mb} MB)", 413)
                out.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return size


async def store_attachment(
    session: AsyncSession,
    project_id: int,
    filename: str,
    content_type: str,
    data: bytes | BinaryIO,
    *,
    task_id: int | None = None,
    agent_id: int | None = None,
) -> Attachment:
    """``data``: the content, or a file to read it from (an upload: never read whole into memory)."""
    max_bytes = get_config().max_upload_mb * 1024 * 1024
    suffix = Path(filename).suffix[:16]
    attachment = Attachment(
        project_id=project_id,
        task_id=task_id,
        filename=Path(filename).name[:300] or "file",
        content_type=content_type or "application/octet-stream",
        storage_name=f"{uuid.uuid4().hex}{suffix}",
        uploaded_by_agent_id=agent_id,
    )
    path = storage_path(attachment)
    path.parent.mkdir(parents=True, exist_ok=True)
    attachment.size = await asyncio.to_thread(_write_limited, path, data, max_bytes)
    session.add(attachment)
    await session.commit()
    return attachment


async def add_attachment(
    session: AsyncSession, task: Task, filename: str, content_type: str, data: bytes | BinaryIO, *, agent_id: int | None = None
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


async def project_attachments(session: AsyncSession, project_id: int, ids: list[int]) -> list[Attachment]:
    """Attachments of a project by id, in the given order: from tickets and from chats. Unknown ids are an error."""
    found = {a.id: a for a in await session.scalars(select(Attachment).where(Attachment.project_id == project_id, Attachment.id.in_(ids)))}
    if missing := [i for i in ids if i not in found]:
        raise NotFound(f"Unknown attachment: {', '.join(map(str, missing))}")
    return [found[i] for i in dict.fromkeys(ids)]


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


async def agent_names(session: AsyncSession, project_id: int) -> dict[int, str]:
    return {a.id: a.name for a in (await session.scalars(select(Agent).where(Agent.project_id == project_id))).all()}


async def ticket_text(session: AsyncSession, task: Task, comment_limit: int = COMMENT_PAGE) -> str:
    """Compact ticket description for prompts and tools."""
    from ..orchestration.prompts import ticket_brief

    names = await agent_names(session, task.project_id)
    decisions = await list_decisions(session, task.id)
    comments = await recent_comments(session, task.id, comment_limit)
    total = await count_comments(session, task.id)
    brief = ticket_brief(task, await list_attachments(session, task.id), comments, names, runner_path, decisions, comment_total=total)
    stands = ticket_state.lines(await ticket_state.get(session, task.id))
    work = await work_lines(session, task, names)
    return "\n\n".join(part for part in (brief, "\n".join(stands), "\n".join(work)) if part)


BRIEF_ASSIGNMENTS = 5  # finished assignments an agent sees with a ticket; older ones: list_assignments


async def work_lines(session: AsyncSession, task: Task, names: dict[int, str]) -> list[str]:
    """The work on a ticket as an agent sees it: what is open and the latest reports. A report stays
    with its assignment and is no comment, so comments are only what someone meant to say."""
    on_ticket = select(Assignment).where(Assignment.task_id == task.id)
    total = await session.scalar(select(func.count()).select_from(Assignment).where(Assignment.task_id == task.id)) or 0
    if not total:
        return []
    still_open = Assignment.status.in_(OPEN_ASSIGNMENT_STATUSES)
    newest = Assignment.id.desc()
    shown = [
        *await session.scalars(on_ticket.where(still_open).order_by(newest).limit(BRIEF_ASSIGNMENTS)),
        *await session.scalars(on_ticket.where(~still_open).order_by(newest).limit(BRIEF_ASSIGNMENTS)),
    ]
    more = f" (last {len(shown)} of {total}; older: list_assignments)" if total > len(shown) else ""
    return [f"Assignments{more}:"] + [
        f"- #{a.id} [{a.status}] {names.get(a.agent_id, '?')}: {a.title}" + (f" → {truncate(a.result, 400)}" if a.result else "") for a in sorted(shown, key=lambda a: a.id)
    ]


SPLIT_AFTER = 10  # assignments (the median ticket takes 3) after which a ticket is no longer one piece of work
SPLIT_AGAIN = 5  # ... and after how many more it is said again


async def split_hint(session: AsyncSession, task: Task) -> str:
    """Said to the manager in the moment it orders yet more work on a ticket that has outgrown one
    piece of work: no turn of its own, and nothing is said about tickets of a normal size."""
    ordered = select(func.count()).select_from(Assignment).where(Assignment.task_id == task.id, Assignment.landing_id.is_(None), Assignment.train_id.is_(None))
    count = await session.scalar(ordered) or 0
    if count < SPLIT_AFTER or (count - SPLIT_AFTER) % SPLIT_AGAIN:
        return ""
    await flow_log.record(session, task.project_id, "ticket.split_hint", task_id=task.id, assignments=count)
    return (
        f" Ticket #{task.number} has {count} assignments now, more than one ticket should carry: nobody can follow it any more. "
        "If work remains after this one, split it into smaller tickets (create_tasks), each with one result, and finish this ticket instead of ordering more on it."
    )
