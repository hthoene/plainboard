"""Project statistics per day (UTC) and per agent, from what the board and the runs record anyway."""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Assignment, AssignmentStatus, Run, RunStatus, Task, TaskStatus, TaskTransition, utcnow
from ..schemas import StatsAgent, StatsDay, StatsOut


async def overview(session: AsyncSession, project_id: int, days: int = 30, now: datetime | None = None) -> StatsOut:
    today = (now or utcnow()).date()
    since = datetime.combine(today - timedelta(days=days - 1), datetime.min.time())
    by_day = {d.date: d for d in (StatsDay(date=(today - timedelta(days=n)).isoformat()) for n in range(days - 1, -1, -1))}

    transitions = await session.scalars(select(TaskTransition).where(TaskTransition.project_id == project_id).order_by(TaskTransition.at, TaskTransition.id))
    started: dict[int, datetime] = {}
    created: dict[int, datetime] = {}
    done_on: set[tuple[str, int]] = set()  # moved back and forth on one day: still one ticket
    cycles: list[float] = []
    for t in transitions:
        if t.from_status is None:
            created.setdefault(t.task_number, t.at)
        if t.status == TaskStatus.IN_PROGRESS:
            started.setdefault(t.task_number, t.at)
        day = by_day.get(t.at.date().isoformat())
        if day is None:
            continue
        if t.from_status is None:
            day.tickets_created += 1
        if t.status == TaskStatus.DONE and (day.date, t.task_number) not in done_on:
            done_on.add((day.date, t.task_number))
            day.tickets_done += 1
            cycles.append((t.at - started.get(t.task_number, created.get(t.task_number, t.at))).total_seconds())

    agents: dict[int, StatsAgent] = {}
    runs = await session.execute(
        select(Run.agent_id, Run.status, Run.created_at, Run.started_at, Run.finished_at, Run.input_tokens, Run.output_tokens, Run.cost_usd).where(
            Run.project_id == project_id, Run.created_at >= since
        )
    )
    for agent_id, status, created_at, started_at, finished_at, input_tokens, output_tokens, cost in runs:
        day = by_day.get(created_at.date().isoformat())
        agent = agents.setdefault(agent_id, StatsAgent(agent_id=agent_id))
        for item in (day, agent):
            if item is not None:
                item.runs += 1
                item.failed_runs += status == RunStatus.FAILED
                item.tokens += input_tokens + output_tokens
                item.cost_usd += cost
        if started_at and finished_at:
            agent.busy_seconds += int((finished_at - started_at).total_seconds())

    finished = await session.execute(
        select(Assignment.agent_id, func.count())
        .where(Assignment.project_id == project_id, Assignment.status == AssignmentStatus.DONE, Assignment.finished_at >= since)
        .group_by(Assignment.agent_id)
    )
    for agent_id, count in finished:
        agents.setdefault(agent_id, StatsAgent(agent_id=agent_id)).assignments_done = count

    open_tickets = await session.scalar(select(func.count()).select_from(Task).where(Task.project_id == project_id, Task.status != TaskStatus.DONE, Task.archived_at.is_(None)))
    return StatsOut(
        days=list(by_day.values()),
        agents=sorted(agents.values(), key=lambda a: a.runs, reverse=True),
        open_tickets=open_tickets or 0,
        median_cycle_seconds=int(median(cycles)) if cycles else None,
    )
