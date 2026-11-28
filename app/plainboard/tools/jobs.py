"""Background jobs: slow commands that keep running after the agent's turn has ended."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import JobStatus, utcnow
from ..services import agents as agent_service
from ..services import jobs as job_service
from ..services import timing as timing_service
from ..services.errors import WaitEnded
from ..services.util import truncate
from .registry import ToolContext, ToolError, tool


class StartJobArgs(BaseModel):
    command: str = Field(min_length=1, description="Shell command; runs in your workspace")
    wait_minutes: int = Field(default=0, ge=0, le=10, description="Wait this long for its result in this turn; 0 = don't wait")
    server: bool = Field(default=False, description="true for something that keeps running until stopped (dev server, preview)")
    repeat: bool = Field(default=False, description="Only to run a slow command again that this assignment already ran three times")


class WaitJobArgs(BaseModel):
    id: int
    minutes: int = Field(default=5, ge=1, le=10)


class StopJobArgs(BaseModel):
    id: int


async def _wait(ctx: ToolContext, job, minutes: int) -> str:
    await ctx.session.commit()  # don't hold a DB transaction while waiting
    try:
        result = await ctx.runs.wait_job(job.id, minutes * 60, ctx.run.run_id)
    except WaitEnded as exc:
        return f"Stopped waiting for job #{job.id}: {exc}. The job keeps running and its result reaches you as an update."
    if result:
        return result
    await ctx.session.refresh(job)
    if job.status != JobStatus.RUNNING:
        return f"Job #{job.id} has ended ({job.status}, exit code {job.exit_code}). Log: {job.log_path}"
    return f"Job #{job.id} is still running after {minutes} min. Log: {job.log_path}. Go on with what does not depend on it, or end your turn: you continue when it ends."


@tool(
    "start_job",
    "Run a slow shell command (build, install, test suite) outside your turn. With wait_minutes you get its result in this turn if it ends in time; otherwise it arrives as an update when it ends, and your assignment stays open until then.",
    StartJobArgs,
)
async def start_job(ctx: ToolContext, args: StartJobArgs) -> str:
    # A server never ends by itself, so no assignment waits for it.
    assignment_id = None if args.server else ctx.run.assignment_id
    if assignment_id and not args.repeat:
        # Change, slow run, change, slow run: the loop that eats the hours. It gets a bound, like the rounds of a pull request.
        count, spent = await timing_service.runs_in_assignment(ctx.session, assignment_id, args.command)
        if count >= timing_service.REPEATED_RUNS and spent >= timing_service.LOOP_SECONDS:
            raise ToolError(
                f"This assignment has run this command {count} times already ({timing_service.duration(spent)} together). Another run without a new idea is a loop: "
                "finish with a report of what keeps failing. If you changed something the earlier runs did not cover, start it with repeat=true."
            )
    # What this run costs, said before the wait: the agent has no clock and decides here whether to start it.
    cost = "" if args.server else await timing_service.job_note(ctx.session, ctx.agent.id, assignment_id, args.command)
    job = await ctx.runs.start_job(ctx.session, ctx.project, ctx.agent, args.command, assignment_id)
    if args.wait_minutes:
        return await _wait(ctx, job, args.wait_minutes) + (f"\n{cost}" if cost else "")
    if args.server:
        return f"Job #{job.id} started and keeps running until someone stops it (stop_job). Log: {job.log_path}."
    after = "Your assignment stays open until it ends: end your turn and you continue then" if assignment_id else "Its result wakes you after your turn"
    return f"Job #{job.id} started. Log: {job.log_path}. {after}; to have it in this turn, use wait_job." + (f" {cost}" if cost else "")


@tool("wait_job", "Wait in this turn for one of your running jobs and get its result.", WaitJobArgs)
async def wait_job(ctx: ToolContext, args: WaitJobArgs) -> str:
    job = await job_service.get_for_agent(ctx.session, ctx.agent.id, args.id)
    if job.status != JobStatus.RUNNING:
        return f"Job #{job.id} has ended ({job.status}, exit code {job.exit_code}). Log: {job.log_path}"
    return await _wait(ctx, job, args.minutes)


def _age(job) -> str:
    hours = (utcnow() - job.created_at).total_seconds() / 3600
    return f"{hours / 24:.0f} d" if hours >= 48 else f"{hours:.0f} h" if hours >= 1 else "<1 h"


@tool("list_jobs", "Your running and recent background jobs. Manager: also the team's running ones.")
async def list_jobs(ctx: ToolContext, _args) -> str:
    jobs = await job_service.recent_for_agent(ctx.session, ctx.agent.id)
    lines = []
    for job in jobs:
        code = f", exit {job.exit_code}" if job.exit_code is not None else ""
        lines.append(f"#{job.id} {job.status}{code}: {truncate(job.command, 100)} · log: {job.log_path}")
    names = {a.id: a.name for a in await agent_service.list_for_project(ctx.session, ctx.project.id)} if ctx.agent.is_manager else {}
    if ctx.agent.is_manager:
        team = [j for j in await job_service.running_for_project(ctx.session, ctx.project.id) if j.agent_id != ctx.agent.id]
        if team:
            lines.append("Running in the team:")
            lines += [f"#{j.id} {names.get(j.agent_id, '?')}, since {_age(j)}: {truncate(j.command, 100)}" for j in team]
    # Detached processes and containers of earlier turns (#187): they stop when their agent is paused or stopped.
    left = await ctx.runs.leftovers(ctx.session, list(names) or [ctx.agent.id], stop=False)
    if left:
        lines.append("Left running outside jobs by earlier turns (stopped when paused or stopped):")
        lines += [f"{names[agent_id] + ': ' if names else ''}{item}" for agent_id, items in left.items() for item in items]
    return "\n".join(lines) or "No jobs."


@tool("stop_job", "Stop one of your running background jobs. Manager: any job of the team.", StopJobArgs)
async def stop_job(ctx: ToolContext, args: StopJobArgs) -> str:
    if ctx.agent.is_manager:
        job = await job_service.get_for_project(ctx.session, ctx.project.id, args.id)
    else:
        job = await job_service.get_for_agent(ctx.session, ctx.agent.id, args.id)
    if job.status != JobStatus.RUNNING or not await ctx.runs.stop_job(ctx.session, job):
        return f"Job #{job.id} has already ended."
    return f"Job #{job.id} stopped."
