"""Asks now and then whether a newer PlainBoard exists than the one that is running.

No model and nothing about the user's projects: the runner asks GitHub how many commits the upstream
branch is ahead of the commit this image was built from, or for a release, how many newer releases
(tags vX.Y.Z) exist (services/version.py). The UI shows the
answer as a quiet notice. Whatever goes wrong (offline, a private fork, a build without git data)
means no notice, never an error for the user.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import get_config
from ..models import utcnow
from ..services import settings as settings_service
from ..services import version as version_service
from .runner_client import RunnerClient, RunnerError

log = logging.getLogger(__name__)

START_DELAY_SECONDS = 30.0  # the runner comes up with the app


class UpdateWatcher:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runner: RunnerClient, interval: float):
        self._sm = sessionmaker
        self._runner = runner
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="update-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)  # its database connection is back before the engine goes

    async def _loop(self) -> None:
        await asyncio.sleep(min(START_DELAY_SECONDS, self._interval))
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Update watcher tick failed")
            await asyncio.sleep(self._interval)

    async def tick(self) -> None:
        commit = version_service.running_commit()
        if not commit:
            return  # this build does not know what it is: nothing to compare
        async with self._sm() as s:
            values = await settings_service.get_all(s)
        if not values["update_check"]:
            version_service.forget()
            return
        config = get_config()
        try:
            state = await self._runner.upstream(settings_service.credentials(values), config.update_repository, config.update_branch, commit, version_service.running_version())
        except RunnerError as exc:  # offline, rate limited: what was known stays, the next round asks again
            log.info("Could not ask for newer versions: %s", str(exc)[:200])
            return
        version_service.record(state.behind if state else None, state.url if state else "", utcnow(), state.latest if state else "")
