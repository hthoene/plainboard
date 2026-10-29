"""One-off and recurring (cron) schedules that send a prompt to an agent."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Agent, Schedule, utcnow
from ..schemas import ScheduleCreate, ScheduleOut, ScheduleUpdate
from . import settings as settings_service
from .errors import NotFound, ServiceError


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
        created_by_agent_id=by_agent_id,
    )
    schedule.run_at = to_utc(data.run_at, tz) if data.run_at else None
    _validate(schedule)
    schedule.next_run_at = compute_next_run(schedule)
    session.add(schedule)
    await session.commit()
    bus.publish(project_id, "schedule.upserted", ScheduleOut.model_validate(schedule))
    return schedule


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


async def mark_fired(session: AsyncSession, schedule: Schedule, run_id: int | None, now: datetime) -> None:
    schedule.last_run_at = now
    schedule.last_run_id = run_id
    if schedule.kind == "once":
        schedule.enabled = False
    schedule.next_run_at = compute_next_run(schedule, after=now)
    await session.commit()
    bus.publish(schedule.project_id, "schedule.upserted", ScheduleOut.model_validate(schedule))
