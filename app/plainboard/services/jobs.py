"""Background jobs: shell commands that run in the runner independently of an agent's turn."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Job, JobStatus, utcnow
from . import agents as agent_service
from .errors import NotFound
from .util import truncate

RECENT_JOBS = 10


async def add(session: AsyncSession, project_id: int, agent_id: int, runner_id: str, command: str, log_path: str) -> Job:
    job = Job(project_id=project_id, agent_id=agent_id, runner_id=runner_id, command=command, log_path=log_path)
    session.add(job)
    await session.commit()
    await agent_service.publish_by_id(session, agent_id)
    return job


async def running(session: AsyncSession) -> list[Job]:
    return list((await session.scalars(select(Job).where(Job.status == JobStatus.RUNNING).order_by(Job.id))).all())


async def recent_for_agent(session: AsyncSession, agent_id: int) -> list[Job]:
    q = select(Job).where(Job.agent_id == agent_id).order_by(Job.id.desc()).limit(RECENT_JOBS)
    return list((await session.scalars(q)).all())


async def get_for_agent(session: AsyncSession, agent_id: int, job_id: int) -> Job:
    job = await session.get(Job, job_id)
    if job is None or job.agent_id != agent_id:
        raise NotFound(f"You have no job #{job_id}")
    return job


async def finish(session: AsyncSession, job: Job, status: JobStatus, exit_code: int | None = None) -> None:
    job.status, job.exit_code, job.finished_at = status, exit_code, utcnow()
    await session.commit()
    await agent_service.publish_by_id(session, job.agent_id)


def label(job: Job) -> str:
    return f"Job #{job.id} ({truncate(job.command, 200)})"
