"""PlainBoard's own checks of the flow: what slows the team down, found in the database.

No model looks for these. Each check is a query with a threshold; a finding says what was measured
and what usually helps. The manager reads them (the flow_check tool, and once a day in the
tidy-up), the user sees them in the statistics. They are the patterns that were found by hand in
the data of a real project: a base branch that stays red, the same check failing run after run,
repairs waiting behind other work, branches that got stuck and were forgotten, notes that are full.

A mechanism of PlainBoard that can go wrong in a way that costs time gets its check here: what a
person had to dig out of the database once should be found by itself the next time.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    Agent,
    AgentNote,
    Assignment,
    AssignmentStatus,
    Conversation,
    Decision,
    DecisionStatus,
    Job,
    Landing,
    LandingStatus,
    Message,
    Repository,
    Run,
    Schedule,
    Task,
    TaskStatus,
    Train,
    TrainStatus,
    utcnow,
)
from . import own_notes
from .timing import duration
from .util import truncate

QUICK_CHECK_MARK = "# PlainBoard quick check: "  # first line of the job that runs a repository's quick check
SPLIT_AFTER = 10  # assignments on one ticket (the same number as board.SPLIT_AFTER)


@dataclass
class Finding:
    key: str
    weight: int  # what it costs: findings are shown heaviest first
    title: str
    detail: str
    action: str


def _age(since: datetime, now: datetime) -> str:
    return duration((now - since).total_seconds())


def _listed(items: list[str], limit: int = 5) -> str:
    return ", ".join(items[:limit]) + (f" and {len(items) - limit} more" if len(items) > limit else "")


async def findings(session: AsyncSession, project_id: int, hours: int = 24, now: datetime | None = None) -> list[Finding]:
    now = now or utcnow()
    since = now - timedelta(hours=hours)
    agents = {a.id: a for a in await session.scalars(select(Agent).where(Agent.project_id == project_id))}
    repos = list(await session.scalars(select(Repository).where(Repository.project_id == project_id).order_by(Repository.id)))
    found: list[Finding] = []
    for check in (_red_base, _failing_checks, _slow_quick_check, _repairs, _waiting_repairs, _stuck, _long_assignments, _queue, _big_tickets):
        found += await check(session, project_id, repos, agents, since, now)
    for check in (_tool_errors, _full_notes, _schedules_off, _cost, _waiting_for_user, _job_waiting):
        found += await check(session, project_id, agents, since, now)
    return sorted(found, key=lambda f: -f.weight)


def text(found: list[Finding], hours: int, limit: int = 8) -> str:
    """The findings for an agent: short, heaviest first."""
    if not found:
        return f"PlainBoard's checks found nothing in the last {hours} h that slows the team down."
    lines = [f"PlainBoard's checks of the last {hours} h, heaviest first:"]
    for number, finding in enumerate(found[:limit], 1):
        lines += [f"{number}. {finding.title}", f"   {finding.detail}", f"   → {finding.action}"]
    if len(found) > limit:
        lines.append(f"(+{len(found) - limit} lighter ones)")
    return "\n".join(lines)


# --- landing --------------------------------------------------------------------------------


async def _red_base(session, project_id, repos, agents, since, now) -> list[Finding]:
    out = []
    for repo in repos:
        lately = select(Train).where(Train.repository_id == repo.id, Train.direct, Train.status.in_((TrainStatus.LANDED, TrainStatus.FAILED))).order_by(Train.id.desc()).limit(20)
        streak = []
        for train in await session.scalars(lately):
            if train.status != TrainStatus.FAILED:
                break
            streak.append(train)
        # Red for two landings in a row, or one red base branch that nothing has made green for an hour.
        if not streak or (len(streak) < 2 and (now - (streak[0].finished_at or streak[0].created_at)).total_seconds() < 3600):
            continue
        names = Counter(name for t in streak for name in t.failed_checks.split("\n") if name)
        repairs = await session.scalar(
            select(func.count()).select_from(Assignment).where(Assignment.train_id.in_([t.id for t in streak]), Assignment.status != AssignmentStatus.CANCELLED)
        )
        out.append(
            Finding(
                "red_base",
                90,
                f"{streak[-1].base or 'The base branch'} of {repo.name} has failed its checks for {len(streak)} landings in a row" if len(streak) > 1 else f"{streak[0].base or 'The base branch'} of {repo.name} fails its checks",
                f"Red for {_age(streak[-1].finished_at or streak[-1].created_at, now)}. Failing: {_listed([f'{n} ({c}x)' for n, c in names.most_common()], 6)}. Repairs so far: {repairs or 0}.",
                "Work that starts now builds on a broken base. Delegate the repair with what the failed logs say, or change the check if the check is what is wrong.",
            )
        )
    return out


FAILING_TOLD_SECONDS = 12 * 3600  # how often at most the manager is told by itself that a repository's check runs keep failing


async def failing_checks(session: AsyncSession, repo: Repository, since: datetime) -> Finding | None:
    """Most of a repository's check runs since then failed (and there were a few)."""
    runs = (await session.execute(select(Train.status, Train.failed_checks).where(Train.repository_id == repo.id, Train.size > 0, Train.finished_at >= since, Train.status.in_((TrainStatus.LANDED, TrainStatus.FAILED))))).all()
    failed = [names for status, names in runs if status == TrainStatus.FAILED]
    if len(runs) < 4 or len(failed) / len(runs) < 0.4:
        return None
    names = Counter(name for names_ in failed for name in names_.split("\n") if name)
    often = [f"{name} ({count}x)" for name, count in names.most_common(4) if count >= 2]
    quick = f"Its quick check: `{truncate(repo.quick_check_command.strip(), 160)}`." if repo.quick_check_command.strip() else "It has no quick check."
    return Finding(
        "failing_checks",
        80,
        f"{len(failed)} of {len(runs)} check runs of {repo.name} failed",
        (f"Most often: {', '.join(often)}. " if often else "") + quick,
        "What the quick check at hand-over catches never costs a check run: put the fast part of what keeps failing into it "
        "(update_repository quick_check_command: a compile check, lint, the tests of what changed).",
    )


async def _failing_checks(session, project_id, repos, agents, since, now) -> list[Finding]:
    return [finding for repo in repos if (finding := await failing_checks(session, repo, since))]


async def _slow_quick_check(session, project_id, repos, agents, since, now) -> list[Finding]:
    rows = (await session.execute(select(Job.command, Job.created_at, Job.finished_at).where(Job.project_id == project_id, Job.created_at >= since, Job.finished_at.is_not(None), Job.command.like(QUICK_CHECK_MARK + "%")))).all()
    took: dict[str, list[float]] = {}
    for command, created_at, finished_at in rows:
        took.setdefault(command.splitlines()[0].removeprefix(QUICK_CHECK_MARK).strip(), []).append((finished_at - created_at).total_seconds())
    return [
        Finding(
            "slow_quick_check",
            50,
            f"The quick check of {name} is not quick: {duration(median(times))} at the median",
            f"{len(times)} runs, the longest {duration(max(times))}. Every hand-over waits for it.",
            "Keep what answers within a minute or two and leave the rest to the check run (update_repository quick_check_command).",
        )
        for name, times in took.items()
        if len(times) >= 3 and median(times) >= 120
    ]


async def _repairs(session, project_id, repos, agents, since, now) -> list[Finding]:
    done = (await session.execute(select(Assignment.title, Assignment.landing_id, Assignment.train_id).where(Assignment.project_id == project_id, Assignment.status == AssignmentStatus.DONE, Assignment.finished_at >= since))).all()
    repairs = [title for title, landing_id, train_id in done if landing_id is not None or train_id is not None]
    if len(repairs) < 5 or len(repairs) / len(done) < 0.3:
        return []
    conflicts = sum(title.startswith("Resolve the conflicts") for title in repairs)
    return [
        Finding(
            "repairs",
            70,
            f"{len(repairs)} of {len(done)} finished assignments were repairs for landing",
            f"{conflicts} for conflicts, {len(repairs) - conflicts} for failed checks.",
            "Conflicts come from branches that wait or are built on each other for long: order smaller pieces and have each handed over when it is done. "
            "For failed checks, see the quick check.",
        )
    ]


async def _waiting_repairs(session, project_id, repos, agents, since, now) -> list[Finding]:
    waiting = list(
        await session.scalars(
            select(Assignment)
            .where(Assignment.project_id == project_id, Assignment.status == AssignmentStatus.QUEUED, Assignment.landing_id.is_not(None) | Assignment.train_id.is_not(None), Assignment.created_at < now - timedelta(minutes=30))
            .order_by(Assignment.id)
        )
    )
    if not waiting:
        return []
    listed = [f"#{a.id} ({agents[a.agent_id].name if a.agent_id in agents else '?'}, {_age(a.created_at, now)})" for a in waiting]
    return [
        Finding(
            "waiting_repairs",
            75,
            f"{len(waiting)} repair{'s' if len(waiting) != 1 else ''} for landing wait{'s' if len(waiting) == 1 else ''} behind other work",
            f"{_listed(listed)}. Nothing of those branches lands meanwhile, and they fall further behind the base branch.",
            "Have the member do them before it goes on (message_agent), or cancel one and delegate it to a member who is free.",
        )
    ]


async def _stuck(session, project_id, repos, agents, since, now) -> list[Finding]:
    stuck = list(await session.scalars(select(Landing).where(Landing.project_id == project_id, Landing.status == LandingStatus.STUCK).order_by(Landing.updated_at.desc())))
    if not stuck:
        return []
    return [
        Finding(
            "stuck",
            60,
            f"{len(stuck)} branch{'es are' if len(stuck) != 1 else ' is'} stuck: PlainBoard has stopped for {'them' if len(stuck) != 1 else 'it'}",
            f"{_listed([f'{b.branch} ({_age(b.updated_at, now)})' for b in stuck])}. They do not land by themselves: finished work may be lying there.",
            "For each one: hand it over again when it is fixed (merge_when_green), or take it off the list if its work landed another way or is dropped (forget_branch).",
        )
    ]


# --- how work is cut ------------------------------------------------------------------------


async def _long_assignments(session, project_id, repos, agents, since, now) -> list[Finding]:
    long = list(
        await session.scalars(
            select(Assignment)
            .where(Assignment.project_id == project_id, Assignment.status == AssignmentStatus.RUNNING, Assignment.landing_id.is_(None), Assignment.train_id.is_(None), Assignment.created_at < now - timedelta(hours=3))
            .order_by(Assignment.id)
        )
    )
    if not long:
        return []
    listed = [f'#{a.id} ({agents[a.agent_id].name if a.agent_id in agents else "?"}, {_age(a.created_at, now)}): "{truncate(a.title, 50)}"' for a in long]
    return [
        Finding(
            "long_assignments",
            55,
            f"{len(long)} assignment{'s have' if len(long) != 1 else ' has'} been open for more than 3 h",
            f"{_listed(listed, 3)}.",
            "Work this long lands late, conflicts with what landed meanwhile and keeps its member from everything else: order what remains in smaller pieces, each handed over when it is done.",
        )
    ]


async def _queue(session, project_id, repos, agents, since, now) -> list[Finding]:
    waits = [
        (started - created).total_seconds()
        for created, started in await session.execute(select(Run.created_at, Run.started_at).where(Run.project_id == project_id, Run.trigger == "assignment", Run.created_at >= since, Run.started_at.is_not(None)))
    ]
    if len(waits) < 5 or median(waits) < 600:
        return []
    return [
        Finding(
            "queue",
            50,
            f"Assignments waited {duration(median(waits))} at the median before they started",
            f"{len(waits)} assignments, {sum(w >= 1800 for w in waits)} of them waited more than 30 min.",
            "A member does one thing at a time: give independent work to members that are free, or raise the limit (set_parallel_agents) if all slots are in use.",
        )
    ]


async def _big_tickets(session, project_id, repos, agents, since, now) -> list[Finding]:
    counted = (
        select(Task.number, func.count(Assignment.id))
        .join(Assignment, Assignment.task_id == Task.id)
        .where(Task.project_id == project_id, Task.status != TaskStatus.DONE, Task.archived_at.is_(None), Assignment.landing_id.is_(None), Assignment.train_id.is_(None))
        .group_by(Task.id)
        .having(func.count(Assignment.id) >= SPLIT_AFTER)
        .order_by(func.count(Assignment.id).desc())
    )
    big = (await session.execute(counted)).all()
    if not big:
        return []
    return [
        Finding(
            "big_tickets",
            45,
            f"{len(big)} open ticket{'s have' if len(big) != 1 else ' has'} {SPLIT_AFTER} or more assignments",
            f"{_listed([f'#{number} ({count})' for number, count in big])}. Nobody can follow such a ticket any more.",
            "Split what remains into smaller tickets (create_tasks), each with one result, and finish these.",
        )
    ]


# --- friction -------------------------------------------------------------------------------


async def _tool_errors(session, project_id, agents, since, now) -> list[Finding]:
    from ..tools.registry import all_tools  # the tools import the services

    names = {tool.name for tool in all_tools()}
    rows = (
        await session.execute(
            select(Message.content, Message.meta["result"].as_string())
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.agent_id.in_(list(agents)), Message.kind == "tool", Message.created_at >= since, Message.meta["is_error"].as_boolean().is_(True))
        )
    ).all()
    refused: dict[str, Counter] = {}
    for name, result in rows:
        name = name.removeprefix("mcp__plainboard__")
        if name in names and not str(result or "").startswith("Not handed over: the quick check"):  # that is the quick check doing its work
            refused.setdefault(name, Counter())[truncate(str(result or "").split("\n")[0], 110)] += 1
    often = sorted(((name, reasons) for name, reasons in refused.items() if sum(reasons.values()) >= 5), key=lambda item: -sum(item[1].values()))
    if not often:
        return []
    listed = "; ".join(f'{name} {sum(reasons.values())}x (mostly: "{reasons.most_common(1)[0][0]}")' for name, reasons in often[:3])
    return [
        Finding(
            "tool_errors",
            40,
            f"{sum(sum(r.values()) for _n, r in often)} calls of PlainBoard's tools were refused",
            f"{listed}.",
            "Each refused call is a step for nothing. Remove what it says: full notes (edit_member_note), a tool that is used wrongly (say how in the member's instructions).",
        )
    ]


async def _full_notes(session, project_id, agents, since, now) -> list[Finding]:
    used = dict((await session.execute(select(AgentNote.agent_id, func.sum(func.length(AgentNote.text))).where(AgentNote.agent_id.in_(list(agents))).group_by(AgentNote.agent_id))).all())
    full = [f"{agents[agent_id].name} ({chars} of {own_notes.BUDGET_CHARS})" for agent_id, chars in used.items() if chars >= 0.9 * own_notes.BUDGET_CHARS]
    if not full:
        return []
    return [
        Finding(
            "full_notes",
            35,
            f"The own notes of {len(full)} agent{'s are' if len(full) != 1 else ' is'} full",
            f"{_listed(full)} characters. Saving a note fails then, and every one of them is read with every turn.",
            "Delete what no longer applies (member_notes, edit_member_note; your own: forget): the state of work that is finished, things the tickets say.",
        )
    ]


async def _schedules_off(session, project_id, agents, since, now) -> list[Finding]:
    off = list(await session.scalars(select(Schedule.name).where(Schedule.project_id == project_id, Schedule.kind == "cron", Schedule.enabled.is_(False))))
    if not off:
        return []
    return [
        Finding(
            "schedules_off",
            30,
            f"{len(off)} schedule{'s are' if len(off) != 1 else ' is'} switched off",
            f"{_listed([repr(name) for name in off])}.",
            "Switch on again what should run (update_schedule), delete what has done its job.",
        )
    ]


async def _cost(session, project_id, agents, since, now) -> list[Finding]:
    rows = (await session.execute(select(Run.agent_id, func.sum(Run.cost_usd), func.avg(Run.context_tokens)).where(Run.project_id == project_id, Run.created_at >= since, Run.num_turns > 0).group_by(Run.agent_id))).all()
    total = sum(cost or 0 for _a, cost, _c in rows)
    if total < 20 or len(rows) < 2:
        return []
    agent_id, cost, context = max(rows, key=lambda row: row[1] or 0)
    if cost / total < 0.6 or agent_id not in agents:
        return []
    agent = agents[agent_id]
    return [
        Finding(
            "cost",
            45,
            f"{agent.name} made {round(100 * cost / total)} % of the cost (${cost:.0f} of ${total:.0f})",
            f"Model {agent.model}, effort {agent.effort or 'default'}, {round((context or 0) / 1000)}k tokens of context per turn on average.",
            "Every turn re-reads its context: order its work in smaller pieces, and check whether all of it needs that model and effort (update_agent, or effort per assignment).",
        )
    ]


async def _waiting_for_user(session, project_id, agents, since, now) -> list[Finding]:
    old = list(await session.scalars(select(Decision).where(Decision.project_id == project_id, Decision.status == DecisionStatus.OPEN, Decision.created_at < now - timedelta(hours=12)).order_by(Decision.id)))
    if not old:
        return []
    return [
        Finding(
            "waiting_for_user",
            40,
            f"{len(old)} decision{'s have' if len(old) != 1 else ' has'} waited for the user for more than 12 h",
            f"The oldest for {_age(old[0].created_at, now)}: \"{truncate(old[0].question, 80)}\".",
            "Name them in your next message to the user. What is yours to decide, decide and withdraw the question.",
        )
    ]


async def _job_waiting(session, project_id, agents, since, now) -> list[Finding]:
    members = [a.id for a in agents.values() if not a.is_manager]
    rows = (await session.execute(select(Run.started_at, Run.finished_at, Run.job_wait_seconds).where(Run.agent_id.in_(members), Run.created_at >= since, Run.finished_at.is_not(None), Run.started_at.is_not(None)))).all()
    in_turns = sum((finished - started).total_seconds() for started, finished, _w in rows)
    waiting = sum(wait or 0 for _s, _f, wait in rows)
    if in_turns < 3600 or waiting / in_turns < 0.5:
        return []
    return [
        Finding(
            "job_waiting",
            35,
            f"Members spent {duration(waiting)} of {duration(in_turns)} in their turns waiting for jobs",
            "A waiting turn takes no slot, but its member does nothing else meanwhile.",
            "time_report names the slowest commands: make the worst one faster or needed less often (a narrower check, a warm build folder).",
        )
    ]
