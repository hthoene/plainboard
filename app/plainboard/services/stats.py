"""Project statistics per day (UTC) and per agent, from what the board and the runs record anyway."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, Agent, Assignment, Landing, TrainStatus, AssignmentStatus, Comment, Conversation, FlowEvent, InboxItem, Message, Run, RunStatus, Task, TaskStatus, TaskTransition, Train, utcnow
from ..schemas import StatsAgent, StatsDay, StatsOut, StatsPeriod, StatsVersion
from . import version as version_service
from . import timing


async def overview(session: AsyncSession, project_id: int, days: int = 30, now: datetime | None = None) -> StatsOut:
    today = (now or utcnow()).date()
    since = datetime.combine(today - timedelta(days=days - 1), datetime.min.time())
    by_day = {d.date: d for d in (StatsDay(date=(today - timedelta(days=n)).isoformat()) for n in range(days - 1, -1, -1))}
    before = since - timedelta(days=days)  # the period right before, for comparison
    current, previous = StatsPeriod(), StatsPeriod()

    def period(at: datetime | None) -> StatsPeriod | None:
        return None if at is None or at < before else current if at >= since else previous

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
            key = (t.at.date().isoformat(), t.task_number)
            if t.status == TaskStatus.DONE and t.at >= before and key not in done_on:
                done_on.add(key)
                previous.tickets_done += 1
            continue
        if t.from_status is None:
            day.tickets_created += 1
        if t.status == TaskStatus.DONE and (day.date, t.task_number) not in done_on:
            done_on.add((day.date, t.task_number))
            day.tickets_done += 1
            current.tickets_done += 1
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

    # How fast work reaches the base branch, per day and for this period against the one before.
    took: dict[str, list[float]] = defaultdict(list)
    for created_at, merged_at in await session.execute(select(Landing.created_at, Landing.merged_at).where(Landing.project_id == project_id, Landing.merged_at >= before)):
        seconds = max((merged_at - created_at).total_seconds(), 0.0)
        for key in (merged_at.date().isoformat(), "current" if merged_at >= since else "previous"):
            took[key].append(seconds)
        period(merged_at).branches_landed += 1  # type: ignore[union-attr]
        if day := by_day.get(merged_at.date().isoformat()):
            day.branches_landed += 1
    for key, values in took.items():
        target = current if key == "current" else previous if key == "previous" else by_day.get(key)
        if target is not None:
            target.landing_seconds = int(median(values))
    checked = select(Train.status, Train.finished_at).where(Train.project_id == project_id, Train.size > 0, Train.finished_at >= before, Train.status.in_((TrainStatus.LANDED, TrainStatus.FAILED)))
    for status, finished_at in await session.execute(checked):
        for item in (period(finished_at), by_day.get(finished_at.date().isoformat())):
            if item is not None:
                item.check_runs += 1
                item.check_runs_failed += status == TrainStatus.FAILED
    done = select(Assignment.finished_at, Assignment.landing_id, Assignment.train_id).where(Assignment.project_id == project_id, Assignment.status == AssignmentStatus.DONE, Assignment.finished_at >= before)
    for finished_at, landing_id, train_id in await session.execute(done):
        for item in (period(finished_at), by_day.get(finished_at.date().isoformat())):
            if item is not None:
                item.assignments_done += 1
                item.repairs += landing_id is not None or train_id is not None
    current.cost_usd = round(sum(d.cost_usd for d in by_day.values()), 4)
    previous.cost_usd = round(await session.scalar(select(func.sum(Run.cost_usd)).where(Run.project_id == project_id, Run.created_at >= before, Run.created_at < since)) or 0.0, 4)
    versions = [StatsVersion(at=v["at"], commit=v["commit"]) for v in await version_service.history(session) if v["at"] >= since]
    for v in versions:
        if day := by_day.get(v.at.date().isoformat()):
            day.updates += 1

    open_tickets = await session.scalar(select(func.count()).select_from(Task).where(Task.project_id == project_id, Task.status != TaskStatus.DONE, Task.archived_at.is_(None)))
    return StatsOut(
        days=list(by_day.values()),
        agents=sorted(agents.values(), key=lambda a: a.runs, reverse=True),
        open_tickets=open_tickets or 0,
        median_cycle_seconds=int(median(cycles)) if cycles else None,
        current=current,
        previous=previous,
        versions=versions,
    )


SHELL_COMMAND = re.compile(r'"command": "((?:cd \S+ && )?)([\w./-]+)')
REPORT_TICKETS = 300


def _seconds(start: datetime | None, end: datetime | None) -> int:
    return int((end - start).total_seconds()) if start and end else 0


async def work_report(session: AsyncSession, project_id: int, days: int = 14) -> dict:
    """Where time, turns and money went, to find overhead. Numbers and tool names only: no prompts,
    messages, ticket texts or code."""
    since = utcnow() - timedelta(days=days)
    agents = list(await session.scalars(select(Agent).where(Agent.project_id == project_id).order_by(Agent.id)))
    is_manager = {a.id: a.is_manager for a in agents}

    def totals() -> dict:
        return {"runs": 0, "failed": 0, "turns": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0, "cost_usd": 0.0, "busy_seconds": 0, "wait_seconds": 0, "job_wait_seconds": 0}

    def add(item: dict, run: Run) -> None:
        item["runs"] += 1
        item["failed"] += run.status == RunStatus.FAILED
        item["turns"] += run.num_turns
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            item[key] += getattr(run, key)
        item["cost_usd"] = round(item["cost_usd"] + run.cost_usd, 4)
        item["busy_seconds"] += _seconds(run.started_at, run.finished_at)
        item["wait_seconds"] += _seconds(run.created_at, run.started_at)
        item["job_wait_seconds"] += run.job_wait_seconds  # part of busy_seconds spent waiting for a job in the turn

    by_trigger: dict[tuple[int, str], dict] = defaultdict(totals)
    by_ticket: dict[int, dict] = defaultdict(lambda: {"manager": totals(), "members": totals()})
    # Per day and per account, so that a change of the system can be compared before and after.
    by_day: dict[str, dict] = defaultdict(lambda: {**totals(), "by_trigger": Counter()})
    by_account: dict[str | None, dict] = defaultdict(totals)
    no_ticket = {"manager": totals(), "members": totals()}
    for run in await session.scalars(select(Run).where(Run.project_id == project_id, Run.created_at >= since)):
        add(by_trigger[(run.agent_id, run.trigger)], run)
        add(by_account[run.account_key], run)
        day = by_day[run.created_at.date().isoformat()]
        add(day, run)
        day["by_trigger"][run.trigger] += 1
        side = "manager" if is_manager.get(run.agent_id) else "members"
        add((by_ticket[run.task_id] if run.task_id else no_ticket)[side], run)

    # Tool calls per agent; for shell calls also the program, to tell building from git and testing.
    tools: dict[int, Counter] = defaultdict(Counter)
    shell: dict[int, Counter] = defaultdict(Counter)
    refused: dict[int, Counter] = defaultdict(Counter)  # calls that came back as an error: each one is a step for nothing
    calls = await session.execute(
        select(Conversation.agent_id, Message.content, Message.meta)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.agent_id.in_(list(is_manager)), Message.kind == "tool", Message.created_at >= since)
    )
    for agent_id, name, meta in calls:
        tools[agent_id][name] += 1
        if (meta or {}).get("is_error"):
            refused[agent_id][name] += 1
        if name == "Bash" and (found := SHELL_COMMAND.search(str((meta or {}).get("input", "")))):
            shell[agent_id][found.group(2).rsplit("/", 1)[-1]] += 1

    numbers = {t.id: t for t in await session.scalars(select(Task).where(Task.id.in_(list(by_ticket))))} if by_ticket else {}
    assignments: dict[int, list[Assignment]] = defaultdict(list)
    lengths = {"count": 0, "instruction_chars": 0, "result_chars": 0}
    statuses: Counter = Counter()
    for a in await session.scalars(select(Assignment).where(Assignment.project_id == project_id, Assignment.created_at >= since)):
        statuses[a.status] += 1
        lengths["count"] += 1
        lengths["instruction_chars"] += len(a.instructions)
        lengths["result_chars"] += len(a.result or "")
        if a.task_id:
            assignments[a.task_id].append(a)
    moves: dict[int, list[str]] = defaultdict(list)
    for t in await session.scalars(select(TaskTransition).where(TaskTransition.project_id == project_id, TaskTransition.at >= since).order_by(TaskTransition.at, TaskTransition.id)):
        moves[t.task_number].append(t.status)

    # Comments by who wrote them (reports are no comments: many of them on a ticket are noise).
    comments: dict[int, dict[str, int]] = defaultdict(dict)
    written = select(Comment.task_id, Comment.author_kind, func.count()).join(Task, Task.id == Comment.task_id).where(Task.project_id == project_id, Comment.created_at >= since)
    for task_id, kind, count in await session.execute(written.group_by(Comment.task_id, Comment.author_kind)):
        comments[task_id][kind] = count
    # Updates: how many agents got, and how long one lay there before a turn read it.
    read_after = [
        _seconds(created, consumed)
        for created, consumed in await session.execute(select(InboxItem.created_at, InboxItem.consumed_at).where(InboxItem.agent_id.in_(list(is_manager)), InboxItem.created_at >= since))
        if consumed
    ]
    events = dict((await session.execute(select(FlowEvent.kind, func.count()).where(FlowEvent.project_id == project_id, FlowEvent.at >= since).group_by(FlowEvent.kind))).all())
    trains = list(await session.scalars(select(Train).where(Train.project_id == project_id, Train.created_at >= since).order_by(Train.id.desc()).limit(REPORT_TICKETS)))

    tickets = []
    for task_id, sides in by_ticket.items():
        task = numbers.get(task_id)
        work = assignments.get(task_id, [])
        tickets.append(
            {
                "ticket": task.number if task else None,
                "status": task.status if task else "deleted",
                "moves": moves.get(task.number, []) if task else [],
                "assignments": len(work),
                "assigned_agents": len({a.agent_id for a in work}),
                "comments": comments.get(task_id, {}),
                "manager": sides["manager"],
                "members": sides["members"],
            }
        )
    tickets.sort(key=lambda t: t["manager"]["cost_usd"] + t["members"]["cost_usd"], reverse=True)

    overview_days = (await overview(session, project_id, days)).days
    accounts = {a.key: a for a in await session.scalars(select(Account))}
    time = await timing.overview(session, project_id, days * 24)
    return {
        "generated_at": utcnow().isoformat() + "Z",
        "days": days,
        "agents": [
            {
                "id": a.id,
                "name": a.name,
                "role": a.role,
                "manager": a.is_manager,
                "enabled": a.enabled,
                "provider": a.provider,
                "model": a.model,
                "effort": a.effort,
                "toolset": a.toolset,
                "instruction_chars": len(a.instructions),
                "by_trigger": {trigger: item for (agent_id, trigger), item in by_trigger.items() if agent_id == a.id},
                "tool_calls": dict(tools[a.id].most_common()),
                "tool_errors": dict(refused[a.id].most_common()),
                "shell_programs": dict(shell[a.id].most_common(40)),
            }
            for a in agents
        ],
        "tickets": tickets[:REPORT_TICKETS],
        "tickets_left_out": max(len(tickets) - REPORT_TICKETS, 0),
        "without_ticket": no_ticket,
        "assignments": {"by_status": dict(statuses), **lengths},
        "updates": {"count": len(read_after), "median_seconds_until_read": int(median(read_after)) if read_after else 0},
        # What PlainBoard did by itself (services/flow_log.py), and every check run for landing, newest first.
        "flow_events": events,
        "trains": [
            {"id": t.id, "status": t.status, "mode": t.mode, "branches": t.size, "repaired_group": t.group_no is not None, "check_seconds": t.check_seconds, "started_at": t.created_at.isoformat() + "Z"}
            for t in trains
        ],
        # Work against waiting: what the team's time went into. Commands by their first two words only.
        "time": {
            "work_seconds": time.work,
            "waiting_seconds": time.waiting,
            "queued_seconds": time.queued,
            "assignments_finished": time.finished,
            "assignment_median_seconds": time.median_total,
            "assignment_median_work_seconds": time.median_work,
            "slow_commands": [{"command": " ".join(c.command.split()[:2]), "runs": c.runs, "median_seconds": c.median, "total_seconds": c.total} for c in time.commands[:15]],
            "repeated_slow_runs": len(time.loops),
            # Landing: finished branches are checked together, so check runs should be far fewer than branches.
            "landing_check_runs": time.check_rounds,
            "landing_check_runs_failed": time.check_failed,
            "landing_check_run_seconds": time.check_median,
            "branches_landed": time.branches_landed,
        },
        "per_day": [
            {"date": d.date, "tickets_created": d.tickets_created, "tickets_done": d.tickets_done, "runs": d.runs, "cost_usd": round(d.cost_usd, 2)}
            | ({k: v for k, v in by_day[d.date].items() if k not in ("runs", "cost_usd", "by_trigger")} | {"by_trigger": dict(by_day[d.date]["by_trigger"])} if d.date in by_day else {})
            for d in overview_days
        ],
        # Runs from before this was recorded have no account.
        "accounts": [
            {"label": accounts[key].label if key in accounts else "unknown", "provider": accounts[key].provider if key in accounts else None, "sign_in": accounts[key].auth_mode if key in accounts else None, **item}
            for key, item in by_account.items()
        ],
    }
