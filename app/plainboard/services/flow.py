"""What works where, is checked and lands: the live state of a project's flow and its history.

Read-only views over what the other services keep (assignments, runs, jobs, landings, trains) and the
flow log (``flow_log``). The live state is a handful of queries for the whole project, never one per row.
Changes are pushed to the UI as ``flow.changed`` (``_publish_changes`` below); the UI asks again.
"""

from __future__ import annotations

from datetime import datetime
from statistics import median

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from ..events import bus
from ..models import (
    Agent,
    Assignment,
    FlowEvent,
    Job,
    JobStatus,
    Landing,
    LandingStatus,
    Promotion,
    PromotionStatus,
    Repository,
    Run,
    RunStatus,
    Task,
    Train,
    TrainStatus,
    utcnow,
)
from ..schemas import FlowBranch, FlowHistory, FlowHistoryItem, FlowJob, FlowLive, FlowRepair, FlowRepo, FlowStep, FlowTicketBranch, FlowTrain, FlowWork
from . import landings as landing_service
from . import promotions as promotion_service

TYPICAL_OF = 5  # the median of this many of the latest check runs that landed
RECENTLY_LANDED = 5  # per repository
IN_FLIGHT = (LandingStatus.WAITING, LandingStatus.TRAIN, LandingStatus.BACK, LandingStatus.STUCK)
PHASE = {
    LandingStatus.WAITING: "waiting",
    LandingStatus.TRAIN: "checking",
    LandingStatus.BACK: "repairing",
    LandingStatus.MERGED: "merged",
    LandingStatus.STUCK: "stuck",
    LandingStatus.CLOSED: "closed",
}
# The events that say which branches a train carried, best first.
MEMBER_EVENTS = ("train.failed", "train.landed", "train.departed", "train.aborted", "train.stuck")
REPAIR_EVENTS = ("train.repair_ordered", "base.repair_ordered")


def _branch(landing: Landing, numbers: dict[int, int]) -> FlowBranch:
    return FlowBranch(
        id=landing.id, repository_id=landing.repository_id, branch=landing.branch, title=landing.title, agent_id=landing.agent_id,
        task_id=landing.task_id, task_number=numbers.get(landing.task_id or 0), status=PHASE[landing.status],  # type: ignore[arg-type]
        train_id=landing.train_id, failures=landing.failures, since=landing.updated_at, merged_at=landing.merged_at,
        conflict_files=list(landing.conflict_files or []),
        held=landing.hold if landing.hold in ("message", "manager") and landing.status in (LandingStatus.WAITING, LandingStatus.TRAIN, LandingStatus.BACK) else None,
        held_reason=landing.hold_reason or "",
    )  # fmt: skip


def _train(train: Train, branches: list[str]) -> FlowTrain:
    return FlowTrain(
        id=train.id, repository_id=train.repository_id, status=train.status, direct=train.direct, size=train.size,  # type: ignore[arg-type]
        started_at=train.created_at, finished_at=train.finished_at, check_seconds=train.check_seconds,
        failed_checks=[c for c in (train.failed_checks or "").split("\n") if c], repaired_group=train.group_no is not None,
        escalated=train.escalated, branches=branches,
    )  # fmt: skip


async def _numbers(session: AsyncSession, task_ids: set[int | None]) -> dict[int, int]:
    ids = {i for i in task_ids if i}
    if not ids:
        return {}
    return dict((await session.execute(select(Task.id, Task.number).where(Task.id.in_(ids)))).all())


async def _typical(session: AsyncSession, project_id: int) -> dict[int, int]:
    """The median of the last check runs per repository that landed with their own checks first."""
    rows = (await session.execute(
        select(Train.repository_id, Train.check_seconds)
        .where(Train.project_id == project_id, Train.status == TrainStatus.LANDED, Train.direct.is_(False), Train.size > 0, Train.check_seconds > 0)
        .order_by(Train.id.desc()).limit(60)
    )).all()  # fmt: skip
    seconds: dict[int, list[int]] = {}
    for repository_id, value in rows:
        if len(seconds.setdefault(repository_id, [])) < TYPICAL_OF:
            seconds[repository_id].append(value)
    return {k: int(median(v)) for k, v in seconds.items()}


async def live(session: AsyncSession, project_id: int) -> FlowLive:
    repos = list((await session.scalars(select(Repository).where(Repository.project_id == project_id).order_by(Repository.id))).all())
    typical = await _typical(session, project_id)
    red = {r.id for r in await landing_service.red_repositories(session, project_id)} if repos else set()

    landings = list((await session.scalars(select(Landing).where(Landing.project_id == project_id, Landing.status.in_(IN_FLIGHT)).order_by(Landing.id))).all())
    merged = (await session.scalars(select(Landing).where(Landing.project_id == project_id, Landing.status == LandingStatus.MERGED).order_by(Landing.merged_at.desc(), Landing.id.desc()).limit(40))).all()
    shown: dict[int, int] = {}
    for landing in merged:
        if shown.get(landing.repository_id, 0) < RECENTLY_LANDED:
            shown[landing.repository_id] = shown.get(landing.repository_id, 0) + 1
            landings.append(landing)
    trains = list((await session.scalars(select(Train).where(Train.project_id == project_id, Train.status.in_((TrainStatus.CHECKING, TrainStatus.BLOCKED))).order_by(Train.id))).all())
    members: dict[int, list[str]] = {}
    for landing in landings:
        if landing.train_id and landing.status != LandingStatus.MERGED:
            members.setdefault(landing.train_id, []).append(landing.branch)

    # Work: turns that run or wait, and the jobs agents left running.
    runs = list((await session.scalars(select(Run).where(Run.project_id == project_id, Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING))).order_by(Run.id))).all())
    jobs = list((await session.scalars(select(Job).where(Job.project_id == project_id, Job.status == JobStatus.RUNNING).order_by(Job.id))).all())
    assignments = {a.id: a for a in (await session.scalars(select(Assignment).where(Assignment.id.in_({r.assignment_id for r in runs if r.assignment_id})))).all()} if runs else {}
    by_id = {landing.id: landing for landing in landings}
    repair_landings = {a.landing_id for a in assignments.values() if a.landing_id} - by_id.keys()
    if repair_landings:
        by_id.update({landing.id: landing for landing in await session.scalars(select(Landing).where(Landing.id.in_(repair_landings)))})
    numbers = await _numbers(session, {r.task_id for r in runs} | {landing.task_id for landing in by_id.values()})

    work: dict[int, FlowWork] = {}
    for run in runs:
        item = work.get(run.agent_id)
        if item is not None and item.status == "running":
            continue  # one turn per agent runs; a waiting one behind it says nothing new
        assignment = assignments.get(run.assignment_id or 0)
        branch = None
        if assignment and assignment.landing_id and assignment.landing_id in by_id:
            branch = by_id[assignment.landing_id].branch
        elif assignment and assignment.train_id:
            branch = ", ".join(members.get(assignment.train_id, [])) or None
        work[run.agent_id] = FlowWork(
            agent_id=run.agent_id, status="running" if run.status == RunStatus.RUNNING else "queued", run_id=run.id, trigger=str(run.trigger),
            since=run.started_at or run.created_at, task_id=run.task_id, task_number=numbers.get(run.task_id or 0),
            title=assignment.title if assignment else "", repair=bool(assignment and (assignment.landing_id or assignment.train_id)), branch=branch,
        )  # fmt: skip
    for job in jobs:
        item = work.setdefault(job.agent_id, FlowWork(agent_id=job.agent_id, status="background"))
        item.jobs.append(FlowJob(id=job.id, command=" ".join(job.command.split())[:200], started_at=job.created_at))

    return FlowLive(
        now=utcnow(),
        repositories=[
            FlowRepo(id=r.id, name=r.name, base=r.default_branch or "", land_first=r.land_first, typical_check_seconds=typical.get(r.id),
                     check_target_seconds=r.check_target_seconds, red=r.id in red, release=r.release_branch or "")
            for r in repos
        ],  # fmt: skip
        work=list(work.values()),
        branches=[_branch(landing, numbers) for landing in landings],
        trains=[_train(t, members.get(t.id, [])) for t in trains],
        promotions=[promotion_service.out(p) for p in await promotion_service.latest(session, project_id)] if any(r.release_branch for r in repos) else [],
    )


async def history(session: AsyncSession, project_id: int, *, repository_id: int | None = None, before: datetime | None = None, limit: int = 30) -> FlowHistory:
    """Trains (with their branches and the repairs ordered for them), branch sweeps and branches handed to the manager, newest first."""
    repos = {r.id: r.name for r in await session.scalars(select(Repository).where(Repository.project_id == project_id))}
    names = {name: rid for rid, name in repos.items()}
    when = func.coalesce(Train.finished_at, Train.created_at)
    q = select(Train).where(Train.project_id == project_id, Train.status != TrainStatus.CHECKING)
    if repository_id is not None:
        q = q.where(Train.repository_id == repository_id)
    if before is not None:
        q = q.where(when < before)
    trains = list((await session.scalars(q.order_by(when.desc(), Train.id.desc()).limit(limit))).all())

    eq = select(FlowEvent).where(FlowEvent.project_id == project_id, FlowEvent.kind.in_(("branches.tidied", "landing.stuck")))
    if before is not None:
        eq = eq.where(FlowEvent.at < before)
    events = list((await session.scalars(eq.order_by(FlowEvent.at.desc(), FlowEvent.id.desc()).limit(limit))).all())
    stuck_landings = {e.landing_id: None for e in events if e.kind == "landing.stuck" and e.landing_id}
    if stuck_landings:
        stuck_landings = dict((await session.execute(select(Landing.id, Landing.repository_id).where(Landing.id.in_(list(stuck_landings))))).all())

    ids = [t.id for t in trains]
    branches: dict[int, list[str]] = {}
    repairs: dict[int, list[FlowRepair]] = {}
    if ids:
        for e in await session.scalars(select(FlowEvent).where(FlowEvent.project_id == project_id, FlowEvent.train_id.in_(ids), FlowEvent.kind.in_(MEMBER_EVENTS + REPAIR_EVENTS)).order_by(FlowEvent.id)):
            if e.kind in REPAIR_EVENTS:
                repairs.setdefault(e.train_id or 0, []).append(FlowRepair(agent_id=e.agent_id, assignment_id=e.assignment_id, at=e.at))
            elif e.train_id not in branches and e.data.get("branches"):
                branches[e.train_id or 0] = [str(b) for b in e.data["branches"]]
        for train_id, branch in (await session.execute(select(Landing.train_id, Landing.branch).where(Landing.train_id.in_([i for i in ids if i not in branches])).order_by(Landing.id))).all():
            branches.setdefault(train_id, []).append(branch)

    items = [
        FlowHistoryItem(id=f"train-{t.id}", kind="train", at=t.finished_at or t.created_at, repository_id=t.repository_id, train=_train(t, branches.get(t.id, [])), repairs=repairs.get(t.id, []))
        for t in trains
    ]  # fmt: skip
    for e in events:
        if e.kind == "branches.tidied":
            repo_id = names.get(str(e.data.get("repository", "")))
            if repository_id is not None and repo_id != repository_id:
                continue
            removed = list((e.data.get("branches") or {}).keys())
            items.append(FlowHistoryItem(id=f"sweep-{e.id}", kind="sweep", at=e.at, repository_id=repo_id, detail=", ".join(removed[:6]) + (f" +{len(removed) - 6}" if len(removed) > 6 else "")))
        else:
            repo_id = stuck_landings.get(e.landing_id) if e.landing_id else None
            if repository_id is not None and repo_id != repository_id:
                continue
            items.append(FlowHistoryItem(id=f"stuck-{e.id}", kind="stuck", at=e.at, repository_id=repo_id, detail=f"{e.data.get('branch', '')}: {e.data.get('why', '')}".strip(": ")))
    items.sort(key=lambda i: (i.at, i.id), reverse=True)
    more = len(items) > limit or len(trains) == limit or len(events) == limit
    items = items[:limit]
    return FlowHistory(items=items, next_before=items[-1].at if more and items else None)


async def of_ticket(session: AsyncSession, task_id: int) -> list[FlowTicketBranch]:
    """The way each branch of a ticket takes: handed over, checks failed, repaired, checked on a train, landed."""
    landings = list((await session.scalars(select(Landing).where(Landing.task_id == task_id).order_by(Landing.id))).all())
    if not landings:
        return []
    numbers = await _numbers(session, {task_id})
    # A branch a repair branch carries (landings.carry) goes that one's way: its checks and landing are the carrier's.
    carriers = {c.id: c for c in await session.scalars(select(Landing).where(Landing.id.in_([x.carried_by for x in landings if x.carried_by and x.status == LandingStatus.CLOSED])))}
    trains = {t.id: t for t in await session.scalars(select(Train).where(Train.id.in_([x.train_id for x in (*landings, *carriers.values()) if x.train_id])))}
    repos = {r.id: r.name for r in await session.scalars(select(Repository).where(Repository.id.in_({x.repository_id for x in landings})))}
    events: dict[int, list[FlowEvent]] = {}
    for e in await session.scalars(select(FlowEvent).where(FlowEvent.landing_id.in_([x.id for x in landings]), FlowEvent.kind.in_(("landing.back", "landing.stuck"))).order_by(FlowEvent.id)):
        events.setdefault(e.landing_id or 0, []).append(e)
    released = await promotion_service.of_landings(session, [i for x in landings for i in (x.id, x.carried_by) if i])
    out = []
    for landing in landings:
        carrier = carriers.get(landing.carried_by or 0) if landing.status == LandingStatus.CLOSED else None
        way = carrier or landing
        train = trains.get(way.train_id or 0)
        steps = [FlowStep(key="handed", state="done", at=landing.created_at)]
        back = [e for e in events.get(landing.id, []) if e.kind == "landing.back"]
        for i, e in enumerate(back):
            steps.append(FlowStep(key="failed", state="done", at=e.at, detail=str(e.data.get("why", ""))[:200]))
            steps.append(FlowStep(key="repair", state="now" if landing.status == LandingStatus.BACK and i == len(back) - 1 else "done"))
        status = way.status
        if status == LandingStatus.STUCK:
            steps.append(FlowStep(key="stuck", state="now", at=next((e.at for e in reversed(events.get(landing.id, [])) if e.kind == "landing.stuck"), None)))
        elif train is not None and train.direct and status == LandingStatus.MERGED:
            steps.append(FlowStep(key="landed", state="done", at=way.merged_at))
            steps.append(FlowStep(key="checking", state="done" if train.status != TrainStatus.CHECKING else "now", at=train.created_at))
        elif status == LandingStatus.MERGED:
            steps.append(FlowStep(key="checking", state="done", at=train.finished_at if train else None))
            steps.append(FlowStep(key="landed", state="done", at=way.merged_at))
        else:
            steps.append(FlowStep(key="checking", state="now" if status == LandingStatus.TRAIN else "todo", at=train.created_at if train and status == LandingStatus.TRAIN else None))
            steps.append(FlowStep(key="landed", state="todo"))
        if status == LandingStatus.MERGED and (promotion := released.get(way.id)):
            done = promotion.status == PromotionStatus.PROMOTED
            steps.append(FlowStep(key="released", state="done" if done else "now", at=promotion.finished_at, detail=promotion.release + (f" ({promotion.tag})" if promotion.tag else "") + ("" if done else f": {promotion.url}")))
        branch = _branch(landing, numbers)
        if carrier is not None:
            shown = _branch(carrier, numbers)
            branch = branch.model_copy(update={"title": carrier.title, "status": shown.status, "train_id": carrier.train_id, "merged_at": carrier.merged_at, "held": shown.held, "held_reason": shown.held_reason})
        out.append(FlowTicketBranch(branch=branch, repository=repos.get(landing.repository_id, ""), train=_train(train, [way.branch]) if train else None, steps=steps))
    return out


# --- the UI asks again when this changes -------------------------------------------------------------


@event.listens_for(Session, "before_flush")
def _note_changes(session: Session, _ctx, _instances) -> None:  # noqa: ANN001
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, (Landing, Train, Job, Promotion)) or (isinstance(obj, FlowEvent) and obj in session.new and obj.kind in ("branches.tidied", "landing.stuck")):
            session.info.setdefault("flow_projects", set()).add(obj.project_id)


@event.listens_for(Session, "after_rollback")
def _forget_changes(session: Session) -> None:
    session.info.pop("flow_projects", None)


@event.listens_for(Session, "after_commit")
def _publish_changes(session: Session) -> None:
    for project_id in session.info.pop("flow_projects", ()):
        bus.publish(project_id, "flow.changed")
