"""Notices the end of background jobs by polling the runner.

Polling needs no callback or credentials, and it survives a restart on either side: finished jobs
wait in the runner until the app asks, and a job the runner no longer knows was lost with it.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import JobStatus
from ..services import jobs as job_service
from .run_manager import RunManager
from .runner_client import RunnerError

log = logging.getLogger(__name__)


class JobWatcher:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runs: RunManager, interval: float):
        self._sm = sessionmaker
        self._runs = runs
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="job-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Job watcher tick failed")
            await asyncio.sleep(self._interval)

    async def tick(self) -> None:
        try:
            await self._runs.check_turns()  # turns the runner lost: the same kind of look
            await self._runs.check_runners()  # external runners that stay away
        except Exception:
            log.exception("Turn check failed")
        async with self._sm() as s:
            # Rows first: each one was accepted by the runner, so the answer below must include it.
            jobs = await job_service.running(s)
            if not jobs:
                return
            runners = self._runs.runners
            by_runner: dict[int | None, list] = {}
            for job in jobs:
                by_runner.setdefault(runners.runner_id(job.project_id), []).append(job)
            states, asked = {}, []
            for runner_id, group in by_runner.items():
                try:
                    states |= await runners.client(runner_id).job_status([job.runner_id for job in group])
                    asked += group
                except RunnerError as exc:  # e.g. rebuilding, an external runner away: decide when it is back
                    log.info("Skipping job check: %s", exc)
            for job in asked:
                await s.refresh(job)
                state = states.get(job.runner_id)
                if job.status != JobStatus.RUNNING or (state and state.running):
                    continue
                await self._runs.job_ended(s, job, state)
