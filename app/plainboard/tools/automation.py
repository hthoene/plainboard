"""Schedules: run a prompt for an agent once at a given time or repeatedly (cron)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..schemas import ScheduleCreate, ScheduleUpdate
from ..services import agents as agent_service
from ..services import schedules as schedule_service
from .registry import ToolContext, tool


class CreateScheduleArgs(BaseModel):
    name: str
    prompt: str = Field(description="What the agent should do when the schedule fires")
    agent: str | None = Field(default=None, description="Target agent (default: yourself)")
    cron: str | None = Field(default=None, description="Recurring: 5-field cron, e.g. '0 9 * * 1-5' (weekdays 09:00)")
    run_at: datetime | None = Field(default=None, description="One-off: local date-time, e.g. '2026-11-02T14:30'")
    timezone: str | None = Field(default=None, description="IANA zone; default from settings")


class UpdateScheduleArgs(BaseModel):
    schedule_id: int
    name: str | None = None
    prompt: str | None = None
    agent: str | None = None
    cron: str | None = None
    run_at: datetime | None = None
    timezone: str | None = None
    enabled: bool | None = None


class ScheduleRef(BaseModel):
    schedule_id: int


def _kind(cron: str | None, run_at: datetime | None) -> Literal["once", "cron"]:
    return "cron" if cron else "once"


@tool("list_schedules", "List schedules with their next run time.")
async def list_schedules(ctx: ToolContext, _args) -> str:
    rows = await schedule_service.list_for_project(ctx.session, ctx.project.id)
    names = {a.id: a.slug for a in await agent_service.list_for_project(ctx.session, ctx.project.id)}
    lines = []
    for s in rows:
        when = s.cron if s.kind == "cron" else schedule_service.to_local(s.run_at, s.timezone)
        state = "" if s.enabled else " (disabled)"
        lines.append(f"{s.id}: {s.name} → {names.get(s.agent_id, s.agent_id)} | {s.kind} {when} | next {schedule_service.to_local(s.next_run_at, s.timezone)}{state}")
    return "\n".join(lines) or "No schedules."


@tool("create_schedule", "Schedule a prompt for an agent: one-off (run_at) or recurring (cron).", CreateScheduleArgs)
async def create_schedule(ctx: ToolContext, args: CreateScheduleArgs) -> str:
    agent = await agent_service.resolve(ctx.session, ctx.project.id, args.agent) if args.agent else ctx.agent
    schedule = await schedule_service.create(
        ctx.session,
        ctx.project.id,
        ScheduleCreate(name=args.name, agent_id=agent.id, prompt=args.prompt, kind=_kind(args.cron, args.run_at), run_at=args.run_at, cron=args.cron, timezone=args.timezone),
        by_agent_id=ctx.agent.id,
    )
    return f"Schedule {schedule.id} created; next run {schedule_service.to_local(schedule.next_run_at, schedule.timezone)}."


@tool("update_schedule", "Change, enable or disable a schedule.", UpdateScheduleArgs)
async def update_schedule(ctx: ToolContext, args: UpdateScheduleArgs) -> str:
    schedule = await schedule_service.get(ctx.session, args.schedule_id, ctx.project.id)
    fields = args.model_dump(exclude_unset=True, exclude={"schedule_id", "agent"})
    if args.agent:
        fields["agent_id"] = (await agent_service.resolve(ctx.session, ctx.project.id, args.agent)).id
    if args.cron:
        fields["kind"] = "cron"
    elif args.run_at:
        fields["kind"] = "once"
    schedule = await schedule_service.update(ctx.session, schedule, ScheduleUpdate(**fields))
    return f"Schedule {schedule.id} updated; next run {schedule_service.to_local(schedule.next_run_at, schedule.timezone)}."


@tool("delete_schedule", "Delete a schedule.", ScheduleRef)
async def delete_schedule(ctx: ToolContext, args: ScheduleRef) -> str:
    schedule = await schedule_service.get(ctx.session, args.schedule_id, ctx.project.id)
    await schedule_service.delete(ctx.session, schedule)
    return f"Deleted schedule {args.schedule_id}."
