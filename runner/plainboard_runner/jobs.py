"""Commands that outlive an agent's turn: the app starts them here and polls for their end.

State is in memory on purpose: a runner restart kills the processes too, and the app reports a
job this runner does not know as lost.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from plainboard_protocol import JobSpec, JobState, WorkspaceSpec

from .config import JOBS_ROOT
from .workspace import resolve_workspace, tool_env

log = logging.getLogger(__name__)

TAIL_CHARS = 1000
STOP_GRACE_SECONDS = 10
RETENTION_SECONDS = 7 * 24 * 3600


@dataclass
class Job:
    proc: asyncio.subprocess.Process
    log_path: Path
    finished_at: float | None = None


_jobs: dict[str, Job] = {}
_tasks: set[asyncio.Task[None]] = set()


def _spawn_task(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def prune() -> int:
    """Forget jobs and delete logs past the retention; the bytes that freed."""
    cutoff = time.time() - RETENTION_SECONDS
    for job_id, job in list(_jobs.items()):
        if job.finished_at and job.finished_at < cutoff:
            del _jobs[job_id]
    active = {job.log_path for job in _jobs.values()}
    freed = 0
    for file in JOBS_ROOT.glob("*.log"):
        try:
            st = file.stat()
            if file not in active and st.st_mtime < cutoff:
                file.unlink(missing_ok=True)
                freed += st.st_blocks * 512 or st.st_size  # blocks can lag behind on some file systems
        except OSError:
            pass
    return freed


async def _watch(job: Job) -> None:
    await job.proc.wait()
    job.finished_at = time.time()


async def start(spec: JobSpec, cwd: Path | None = None) -> JobState:
    """``cwd``: a folder of the runner's own outside the workspaces (a train's clone)."""
    if spec.id in _jobs:
        raise ValueError(f"Job {spec.id} already exists")
    cwd = cwd or resolve_workspace(WorkspaceSpec(path=spec.workspace))
    cwd.mkdir(parents=True, exist_ok=True)
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    prune()
    log_path = JOBS_ROOT / f"{spec.id}.log"
    with log_path.open("wb") as out:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            spec.command,
            cwd=str(cwd),
            env={**os.environ, **tool_env(spec.credentials), **spec.env, "PLAINBOARD_JOB": spec.id},  # what it leaves running is found by these (leftovers.py)
            stdin=asyncio.subprocess.DEVNULL,
            stdout=out,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # its own process group, so stop() reaches its children too
        )
    job = Job(proc=proc, log_path=log_path)
    _jobs[spec.id] = job
    _spawn_task(_watch(job))
    log.info("Job %s started (pid %s)", spec.id, proc.pid)
    return _state(job)


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(max(0, path.stat().st_size - TAIL_CHARS * 4))
            return fh.read().decode(errors="replace").strip()[-TAIL_CHARS:]
    except OSError:
        return ""


def _state(job: Job) -> JobState:
    running = job.finished_at is None
    return JobState(running=running, exit_code=job.proc.returncode, log_path=str(job.log_path), tail="" if running else _tail(job.log_path))


def status(ids: list[str]) -> dict[str, JobState]:
    return {job_id: _state(_jobs[job_id]) for job_id in ids if job_id in _jobs}


def is_running(job_id: str) -> bool:
    job = _jobs.get(job_id)
    return job is not None and job.finished_at is None


def running_ids() -> list[str]:
    return [job_id for job_id in _jobs if is_running(job_id)]


def sessions(ids: list[str]) -> dict[int, str]:
    """Session id -> job id of those that still run: a job's shell leads a session of its own (see ``start``)."""
    return {_jobs[job_id].proc.pid: job_id for job_id in ids if is_running(job_id)}


def _signal_group(job: Job, sig: signal.Signals) -> None:
    try:
        os.killpg(job.proc.pid, sig)
    except ProcessLookupError:
        pass


async def _stop(job: Job) -> None:
    _signal_group(job, signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.shield(job.proc.wait()), STOP_GRACE_SECONDS)
    except TimeoutError:
        pass
    _signal_group(job, signal.SIGKILL)  # also whatever kept running after the shell ended


def stop(job_id: str) -> bool:
    job = _jobs.get(job_id)
    if job is None or job.finished_at is not None:
        return False
    _spawn_task(_stop(job))
    return True
