"""Fires due schedules by queueing a run for the schedule's agent."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Agent, RunTrigger, utcnow
from ..services import schedules as schedule_service
from .run_manager import RunManager

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runs: RunManager, interval: float):
        self._sm = sessionmaker
        self._runs = runs
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Scheduler tick failed")
            await asyncio.sleep(self._interval)

    async def tick(self) -> int:
        fired = 0
        now = utcnow()
        async with self._sm() as s:
            for schedule in await schedule_service.due(s, now):
                run_id = None
                agent = await s.get(Agent, schedule.agent_id)
                if agent is not None and agent.enabled:
                    text = f'Scheduled task "{schedule.name}" (schedule {schedule.id}):\n\n{schedule.prompt}'
                    run = await self._runs.enqueue(s, agent, RunTrigger.SCHEDULE, text, schedule_id=schedule.id)
                    run_id = run.id
                    fired += 1
                await schedule_service.mark_fired(s, schedule, run_id, now)
        return fired
