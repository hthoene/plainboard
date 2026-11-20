"""Where the time goes. An agent has no clock and its sense of how long work takes comes from people:
it treats its own work as slow and a test run as quick, when here it is the other way round. So the
time is measured (runs, jobs, pull request checks) and shown in plain numbers where the agent decides:
when it starts a slow command, when it continues an assignment, in every report and on request."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Agent, Assignment, AssignmentStatus, Job, JobStatus, Run, RunTrigger, Train, TrainStatus, utcnow
from .util import truncate

SLOW_SECONDS = 180  # a command that takes this long is worth planning around
FACTS_DAYS = 7
REPEATED_RUNS = 3  # the same slow command this often in one assignment is a loop worth naming
LOOP_SECONDS = 600  # ... once those runs took this long together
ENDED = (JobStatus.SUCCEEDED, JobStatus.FAILED)


def duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    return f"{hours // 24} d {hours % 24} h"


def _span(start: datetime | None, end: datetime | None) -> int:
    return max(int((end - start).total_seconds()), 0) if start and end else 0


def command_key(command: str) -> str:
    """The command without a leading change of directory, so the same check counts as one wherever it ran."""
    text = " ".join(command.split())
    while text.startswith("cd ") and " && " in text:
        text = text.split(" && ", 1)[1]
    return text


@dataclass
class Ledger:
    """How long an assignment took and what the time was spent on."""

    total: int = 0
    work: int = 0  # turns, without the part of them spent waiting for a job
    waiting: int = 0  # for jobs, in a turn or between turns
    queued: int = 0  # before a turn could start
    turns: int = 0
    jobs: int = 0

    def text(self) -> str:
        parts = [f"{duration(self.work)} of work in {self.turns} turn{'s' if self.turns != 1 else ''}"]
        if self.waiting >= 60 or self.jobs:
            parts.append(f"{duration(self.waiting)} waiting for {self.jobs} job{'s' if self.jobs != 1 else ''}")
        if self.queued >= 60:
            parts.append(f"{duration(self.queued)} queued")
        return f"{duration(self.total)}: " + ", ".join(parts)


def _ledger(assignment: Assignment, runs: list[tuple], jobs: int, now: datetime) -> Ledger:
    """``runs``: (created_at, started_at, finished_at, job_wait_seconds) of the assignment's turns."""
    ledger = Ledger(total=_span(assignment.created_at, assignment.finished_at or now), jobs=jobs)
    for created_at, started_at, finished_at, job_wait in runs:
        ledger.queued += _span(created_at, started_at or now)
        if started_at:
            ledger.turns += 1
            ledger.work += max(_span(started_at, finished_at or now) - (job_wait or 0), 0)
    ledger.waiting = max(ledger.total - ledger.work - ledger.queued, 0)
    return ledger


async def ledger(session: AsyncSession, assignment: Assignment, now: datetime | None = None) -> Ledger:
    runs = (await session.execute(select(Run.created_at, Run.started_at, Run.finished_at, Run.job_wait_seconds).where(Run.assignment_id == assignment.id))).all()
    jobs = await session.scalar(select(func.count()).select_from(Job).where(Job.assignment_id == assignment.id)) or 0
    return _ledger(assignment, [tuple(r) for r in runs], jobs, now or utcnow())


async def usual_seconds(session: AsyncSession, agent_id: int, command: str) -> tuple[int, int] | None:
    """How long this agent's last runs of the same command took (median) and how many they were."""
    rows = await session.execute(
        select(Job.created_at, Job.finished_at).where(Job.agent_id == agent_id, Job.command == command, Job.status.in_(ENDED)).order_by(Job.id.desc()).limit(5)
    )
    spans = [_span(start, end) for start, end in rows]
    return (int(median(spans)), len(spans)) if spans else None


async def runs_in_assignment(session: AsyncSession, assignment_id: int, command: str) -> tuple[int, int]:
    """How often an assignment has run this command already, and how long that took together."""
    rows = (await session.execute(select(Job.created_at, Job.finished_at).where(Job.assignment_id == assignment_id, Job.command == command, Job.finished_at.is_not(None)))).all()
    return len(rows), sum(_span(start, end) for start, end in rows)


async def job_note(session: AsyncSession, agent_id: int, assignment_id: int | None, command: str) -> str:
    """What starting this command costs, said at the moment it is started."""
    parts = []
    if (usual := await usual_seconds(session, agent_id, command)) and usual[0] >= 60:
        parts.append(f"It took about {duration(usual[0])} the last {usual[1]} time{'s' if usual[1] != 1 else ''}.")
    if assignment_id:
        count, spent = await runs_in_assignment(session, assignment_id, command)
        if count and spent >= 60:
            parts.append(f"This is run {count + 1} of it in this assignment; the earlier ones took {duration(spent)} together.")
            if count + 1 >= REPEATED_RUNS:
                parts.append("Before another run: fix everything the last one showed, and check with something faster first.")
    return " ".join(parts)


# --- per project: measured facts for prompts and the manager's report -----------------------


@dataclass
class SlowCommand:
    command: str
    runs: int
    median: int
    total: int


@dataclass
class Overview:
    hours: int
    finished: int = 0  # assignments
    median_total: int = 0
    median_work: int = 0
    work: int = 0  # all turns of members and the manager
    waiting: int = 0  # jobs
    queued: int = 0
    commands: list[SlowCommand] = field(default_factory=list)
    loops: list[tuple[int, str, int, str]] = field(default_factory=list)  # assignment, agent, runs, command
    check_rounds: int = 0  # trains: check runs for landing branches
    check_failed: int = 0
    check_median: int = 0
    branches_landed: int = 0
    message_turns: int = 0


async def overview(session: AsyncSession, project_id: int, hours: int = 24, now: datetime | None = None) -> Overview:
    now = now or utcnow()
    since = now - timedelta(hours=hours)
    out = Overview(hours=hours)

    runs = (
        await session.execute(
            select(Run.assignment_id, Run.created_at, Run.started_at, Run.finished_at, Run.job_wait_seconds, Run.trigger).where(Run.project_id == project_id, Run.created_at >= since)
        )
    ).all()
    by_assignment: dict[int, list[tuple]] = defaultdict(list)
    for assignment_id, created_at, started_at, finished_at, job_wait, trigger in runs:
        out.queued += _span(created_at, started_at or now)
        out.work += max(_span(started_at, finished_at or now) - (job_wait or 0), 0) if started_at else 0
        out.message_turns += trigger == RunTrigger.MESSAGE
        if assignment_id:
            by_assignment[assignment_id].append((created_at, started_at, finished_at, job_wait))

    jobs = (await session.execute(select(Job.assignment_id, Job.command, Job.created_at, Job.finished_at, Job.status).where(Job.project_id == project_id, Job.created_at >= since))).all()
    per_command: dict[str, list[int]] = defaultdict(list)
    per_assignment_command: dict[tuple[int, str], int] = defaultdict(int)
    job_counts: dict[int, int] = defaultdict(int)
    for assignment_id, command, created_at, finished_at, status in jobs:
        if assignment_id:
            job_counts[assignment_id] += 1
        if assignment_id is None and status == JobStatus.RUNNING:
            continue  # a server: nobody waits for it
        span = _span(created_at, finished_at or now)
        out.waiting += span
        if status in ENDED:
            key = command_key(command)
            per_command[key].append(span)
            if assignment_id and span >= SLOW_SECONDS:
                per_assignment_command[(assignment_id, key)] += 1
    out.commands = sorted(
        (SlowCommand(key, len(spans), int(median(spans)), sum(spans)) for key, spans in per_command.items() if median(spans) >= SLOW_SECONDS),
        key=lambda c: c.total,
        reverse=True,
    )

    finished = list(
        await session.scalars(select(Assignment).where(Assignment.project_id == project_id, Assignment.status == AssignmentStatus.DONE, Assignment.finished_at >= since))
    )
    ledgers = [_ledger(a, by_assignment.get(a.id, []), job_counts.get(a.id, 0), now) for a in finished]
    out.finished = len(ledgers)
    if ledgers:
        out.median_total, out.median_work = int(median(item.total for item in ledgers)), int(median(item.work for item in ledgers))

    if loops := {key: count for key, count in per_assignment_command.items() if count >= REPEATED_RUNS}:
        names = dict((await session.execute(select(Assignment.id, Agent.name).join(Agent, Agent.id == Assignment.agent_id).where(Assignment.id.in_([a for a, _c in loops])))).all())
        out.loops = sorted(((a, names.get(a, "?"), count, command) for (a, command), count in loops.items()), key=lambda item: item[2], reverse=True)

    for status, size, seconds, direct in await session.execute(
        select(Train.status, Train.size, Train.check_seconds, Train.direct).where(Train.project_id == project_id, Train.size > 0, Train.finished_at >= since, Train.status.in_((TrainStatus.LANDED, TrainStatus.FAILED)))
    ):
        out.check_rounds += 1
        out.check_failed += status == TrainStatus.FAILED
        out.branches_landed += size if status == TrainStatus.LANDED or direct else 0  # landed first: on the base branch whatever the checks said
        out.waiting += seconds
        out.check_median += seconds  # summed here, averaged below
    if out.check_rounds:
        out.check_median //= out.check_rounds
    return out


def report_text(o: Overview, waiting_prs: list[str]) -> str:
    """The manager's time report: plain numbers, the largest wait first."""
    span = f"{o.hours} h" if o.hours < 48 else f"{o.hours // 24} days"
    lines = [f"Last {span}: {duration(o.work)} of work by the team, {duration(o.waiting)} waiting for jobs and checks, {duration(o.queued)} queued."]
    if o.finished:
        lines.append(f"{o.finished} assignments finished: {duration(o.median_total)} each (median), of that {duration(o.median_work)} of work.")
    if o.commands:
        lines.append("Slow commands (total = runs x median):")
        lines += [f"- {duration(c.total)} = {c.runs} x {duration(c.median)}: {truncate(c.command, 100)}" for c in o.commands[:6]]
    if o.check_rounds:
        lines.append(f"Landing: {o.check_rounds} check runs of about {duration(o.check_median)} brought {o.branches_landed} branches to the base branch; {o.check_failed} runs failed.")
    if waiting_prs:
        lines.append("Being checked now: " + "; ".join(waiting_prs[:8]) + ".")
    if o.loops:
        lines.append("Assignments that ran the same slow command several times: " + "; ".join(f"#{a} ({name}) {count} x {truncate(command, 50)}" for a, name, count, command in o.loops[:5]) + ".")
    if o.message_turns:
        lines.append(f"Turns that only handled a message: {o.message_turns}.")
    if len(lines) == 1 and not (o.work or o.waiting):
        return f"Nothing measured in the last {span}."
    if o.waiting > 2 * max(o.work, 1) and o.commands:
        top = o.commands[0]
        lines.append(f"Most of the time is waiting, most of it for: {truncate(top.command, 80)}. Making that faster, or needed less often, saves more than anything else.")
    return "\n".join(lines)


def waiting_line(o: Overview) -> str:
    """For the manager's turn, only while waiting is what the time goes into."""
    if o.waiting < 3600 or o.waiting < 3 * max(o.work, 1):
        return ""
    line = f"Time, last {o.hours} h: {duration(o.work)} of work, {duration(o.waiting)} waiting for jobs and checks."
    if o.commands:
        top = o.commands[0]
        line += f" Most of it: {top.runs} runs of `{truncate(top.command, 70)}` ({duration(top.median)} each)."
    return line + " time_report has the details."


def facts_text(o: Overview) -> str:
    """For every assignment: what is fast and what is slow in this project, in measured numbers."""
    parts = []
    if o.finished >= 3 and o.median_work:
        parts.append(f"an assignment's own work takes {duration(o.median_work)} (median)")
    slow = [f"`{truncate(c.command, 60)}` {duration(c.median)} per run" for c in sorted(o.commands, key=lambda c: c.runs, reverse=True)[:3]]
    if o.check_rounds:
        slow.append(f"the check run that lands branches {duration(o.check_median)}")
    if not slow:
        return ""
    head = f"Measured here in the last {o.hours // 24} days: " + (parts[0] + "; " if parts else "")
    return head + "what takes long is waiting: " + ", ".join(slow) + ". Plan your slow runs, not your work."


_CACHE: dict[tuple[int, int], tuple[float, Overview]] = {}
CACHE_SECONDS = 300


async def cached_overview(session: AsyncSession, project_id: int, hours: int) -> Overview:
    """The overview, at most a few minutes old: it is asked for on every manager turn and assignment."""
    key = (project_id, hours)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = await overview(session, project_id, hours)
    _CACHE[key] = (time.monotonic(), value)
    return value


def forget_cache() -> None:
    _CACHE.clear()


async def running_trains(session: AsyncSession, project_id: int, now: datetime | None = None) -> list[str]:
    now = now or utcnow()
    rows = await session.scalars(select(Train).where(Train.project_id == project_id, Train.size > 0, Train.status == TrainStatus.CHECKING).order_by(Train.id))
    return [f"{train.size} branch{'es' if train.size != 1 else ''} ({duration(_span(train.created_at, now))})" for train in rows]

