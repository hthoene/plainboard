"""Background jobs: slow commands that keep running after the agent's turn has ended."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import JobStatus
from ..services import jobs as job_service
from ..services.util import truncate
from .registry import ToolContext, tool


class StartJobArgs(BaseModel):
    command: str = Field(min_length=1, description="Shell command; runs in your workspace")


class StopJobArgs(BaseModel):
    id: int


@tool(
    "start_job",
    "Run a slow shell command (build, install, test suite) outside your turn, so you stay available. Then finish your turn, without waiting for it (no sleep, tail or Monitor): the result arrives as an update when it ends.",
    StartJobArgs,
)
async def start_job(ctx: ToolContext, args: StartJobArgs) -> str:
    job = await ctx.runs.start_job(ctx.session, ctx.project, ctx.agent, args.command)
    return f"Job #{job.id} started. Log: {job.log_path}. Don't wait for it: end your turn when nothing else is left, its result wakes you."


@tool("list_jobs", "Your running and recent background jobs.")
async def list_jobs(ctx: ToolContext, _args) -> str:
    jobs = await job_service.recent_for_agent(ctx.session, ctx.agent.id)
    lines = []
    for job in jobs:
        code = f", exit {job.exit_code}" if job.exit_code is not None else ""
        lines.append(f"#{job.id} {job.status}{code}: {truncate(job.command, 100)} · log: {job.log_path}")
    return "\n".join(lines) or "No jobs."


@tool("stop_job", "Stop one of your running background jobs.", StopJobArgs)
async def stop_job(ctx: ToolContext, args: StopJobArgs) -> str:
    job = await job_service.get_for_agent(ctx.session, ctx.agent.id, args.id)
    if job.status != JobStatus.RUNNING or not await ctx.runs.stop_job(ctx.session, job):
        return f"Job #{job.id} has already ended."
    return f"Job #{job.id} stopped."
