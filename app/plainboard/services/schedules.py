"""One-off and recurring (cron) schedules that send a prompt to an agent."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Agent, FlowEvent, Run, Schedule, TaskTransition, utcnow
from ..schemas import ScheduleCreate, ScheduleOut, ScheduleUpdate
from . import flow_log
from . import landings as landing_service
from . import settings as settings_service
from .errors import NotFound, ServiceError


SKIPPED = "schedule.skipped"  # in the work log: a run left out because nothing had happened

# Every project starts with this schedule for its manager. It is an ordinary schedule: the user
# (or the manager) can change its prompt or interval, pause it or delete it.
TIDY_NAME = "Tidy up"
TIDY_CRON = "0 4 * * *"  # once a day: every run re-reads the manager's context
TIDY_PROMPT = """\
Routine tidy-up, so that nothing piles up that nobody needs any more. Work carefully: change only what
is clearly outdated or redundant. When in doubt, leave it as it is. Nothing here is urgent.

Look through:
- Project notes: merge duplicates, correct outdated ones, delete obsolete ones, unpin what not everyone
  must always know. Your own notes: forget what no longer applies. Own notes of team members
  (member_notes): only where they look long or old, delete what clearly no longer applies, never notes
  from the user.
- Schedules: delete those that have done their job or repeat another one (not this one).
- Tickets: move finished ones to done, also those in review that the user does not have to look at, and
  archive old done ones. A ticket that looks no longer relevant stays; mention it in your reply instead
  of deleting it.
- Task lists of the team: delete tasks that are done elsewhere or no longer make sense.
- Background jobs of the team (list_jobs): check for each preview or dev server whether it is still
  needed. Stop it with stop_job when its work is finished or it has been running for more than a day
  without anyone using it, and say so in your reply. Leave builds, installs and tests alone.
- Team: do not remove or change agents; if one seems no longer needed, mention it in your reply.
- Waiting time (time_report): if one command or the checks of pull requests make up most of the
  team's waiting and no ticket for it exists, create one to make that faster or needed less often.
- What slows the team down (flow_check): PlainBoard's own checks, with numbers. Do what is yours to
  do at once (notes, schedules, branches to forget or hand over again, a quick check to extend) and
  create a ticket for what needs work, unless one exists. Name the heaviest finding in your reply.

Never delete anything the user wrote or may still need. Do not start new work, do not delegate and do
not message teammates for this. Reply in one or two lines: what you changed, or "Nothing to tidy."."""


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ServiceError(f"Unknown timezone '{name}'") from None


def to_utc(value: datetime, tz_name: str) -> datetime:
    """Naive datetimes are interpreted in the schedule's timezone."""
    aware = value.replace(tzinfo=_zone(tz_name)) if value.tzinfo is None else value
    return aware.astimezone(UTC).replace(tzinfo=None)


def to_local(value: datetime | None, tz_name: str) -> str:
    if value is None:
        return "-"
    return value.replace(tzinfo=UTC).astimezone(_zone(tz_name)).strftime("%Y-%m-%d %H:%M %Z")


def compute_next_run(schedule: Schedule, after: datetime | None = None) -> datetime | None:
    if not schedule.enabled:
        return None
    if schedule.kind == "once":
        return schedule.run_at if schedule.last_run_at is None else None
    after = after or utcnow()
    tz = _zone(schedule.timezone)
    start = after.replace(tzinfo=UTC).astimezone(tz)
    nxt: datetime = croniter(schedule.cron, start).get_next(datetime)
    return nxt.astimezone(UTC).replace(tzinfo=None)


def _validate(schedule: Schedule) -> None:
    _zone(schedule.timezone)
    if schedule.kind == "cron":
        if not schedule.cron or not croniter.is_valid(schedule.cron):
            raise ServiceError(f"Invalid cron expression '{schedule.cron}' (expected e.g. '0 9 * * 1-5')")
    elif schedule.kind == "once":
        if schedule.run_at is None:
            raise ServiceError("run_at is required for one-off schedules")
    else:
        raise ServiceError("kind must be 'once' or 'cron'")


async def _check_agent(session: AsyncSession, project_id: int, agent_id: int) -> None:
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.project_id != project_id:
        raise ServiceError("Agent not found in this project")


async def create(session: AsyncSession, project_id: int, data: ScheduleCreate, *, by_agent_id: int | None = None) -> Schedule:
    await _check_agent(session, project_id, data.agent_id)
    tz = data.timezone or await settings_service.get(session, "timezone")
    schedule = Schedule(
        project_id=project_id,
        agent_id=data.agent_id,
        name=data.name,
        prompt=data.prompt,
        kind=data.kind,
        cron=data.cron,
        timezone=tz,
        enabled=data.enabled,
        skip_quiet=data.skip_quiet,
        created_by_agent_id=by_agent_id,
    )
    schedule.run_at = to_utc(data.run_at, tz) if data.run_at else None
    _validate(schedule)
    schedule.next_run_at = compute_next_run(schedule)
    session.add(schedule)
    await session.commit()
    bus.publish(project_id, "schedule.upserted", ScheduleOut.model_validate(schedule))
    return schedule


async def create_tidy(session: AsyncSession, manager: Agent) -> Schedule:
    """The default tidy-up schedule of a new project's manager."""
    data = ScheduleCreate(name=TIDY_NAME, agent_id=manager.id, prompt=TIDY_PROMPT, kind="cron", cron=TIDY_CRON)
    return await create(session, manager.project_id, data)


async def update(session: AsyncSession, schedule: Schedule, data: ScheduleUpdate) -> Schedule:
    fields = data.model_dump(exclude_unset=True)
    if "agent_id" in fields:
        await _check_agent(session, schedule.project_id, fields["agent_id"])
    run_at = fields.pop("run_at", None)
    for key, value in fields.items():
        setattr(schedule, key, value)
    if run_at is not None:
        schedule.run_at = to_utc(run_at, schedule.timezone)
        schedule.last_run_at = None  # re-arm a one-off schedule
    _validate(schedule)
    schedule.next_run_at = compute_next_run(schedule)
    await session.commit()
    bus.publish(schedule.project_id, "schedule.upserted", ScheduleOut.model_validate(schedule))
    return schedule


async def delete(session: AsyncSession, schedule: Schedule) -> None:
    project_id, schedule_id = schedule.project_id, schedule.id
    await session.delete(schedule)
    await session.commit()
    bus.publish(project_id, "schedule.deleted", {"id": schedule_id})


async def get(session: AsyncSession, schedule_id: int, project_id: int | None = None) -> Schedule:
    schedule = await session.get(Schedule, schedule_id)
    if schedule is None or (project_id is not None and schedule.project_id != project_id):
        raise NotFound(f"Schedule {schedule_id} not found")
    return schedule


async def list_for_project(session: AsyncSession, project_id: int) -> list[Schedule]:
    return list((await session.scalars(select(Schedule).where(Schedule.project_id == project_id).order_by(Schedule.id))).all())


async def due(session: AsyncSession, now: datetime) -> list[Schedule]:
    q = select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_run_at.is_not(None), Schedule.next_run_at <= now)
    return list((await session.scalars(q)).all())


async def quiet(session: AsyncSession, schedule: Schedule) -> bool:
    """Whether nothing happened in the schedule's project since its last run: no turn but its own, no
    ticket moved, nothing landed or noted in the work log, and no base branch is red."""
    since = schedule.last_run_at
    if since is None:
        return False
    if await landing_service.red_repositories(session, schedule.project_id):
        return False  # a red base branch is never a quiet project: it has to be repaired
    turn = select(Run.id).where(Run.project_id == schedule.project_id, Run.created_at > since, or_(Run.schedule_id.is_(None), Run.schedule_id != schedule.id))
    moved = select(TaskTransition.id).where(TaskTransition.project_id == schedule.project_id, TaskTransition.at > since)
    noted = select(FlowEvent.id).where(FlowEvent.project_id == schedule.project_id, FlowEvent.at > since, FlowEvent.kind != SKIPPED)
    for something in (turn, moved, noted):
        if await session.scalar(something.limit(1)) is not None:
            return False
    return True


async def mark_skipped(session: AsyncSession, schedule: Schedule, now: datetime) -> None:
    """Its time passed without a turn. The last run stays the last one that happened."""
    if schedule.kind == "once":
        schedule.enabled = False
    schedule.next_run_at = compute_next_run(schedule, after=now)
    await session.commit()
    await flow_log.record(session, schedule.project_id, SKIPPED, agent_id=schedule.agent_id, schedule=schedule.name)
    bus.publish(schedule.project_id, "schedule.upserted", ScheduleOut.model_validate(schedule))


async def mark_fired(session: AsyncSession, schedule: Schedule, run_id: int | None, now: datetime) -> None:
    schedule.last_run_at = now
    schedule.last_run_id = run_id
    if schedule.kind == "once":
        schedule.enabled = False
    schedule.next_run_at = compute_next_run(schedule, after=now)
    await session.commit()
    bus.publish(schedule.project_id, "schedule.upserted", ScheduleOut.model_validate(schedule))
