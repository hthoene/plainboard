"""Removes the branches that merging left behind.

PlainBoard deletes a branch when it lands it. What it did not land itself stays on the host: a
branch that was merged by hand or with a pull request, one that got stuck and reached the base
branch another way, one from before PlainBoard landed anything. A repository can have those removed
(``repository.tidy_branches``), once a day, without any model: only branches whose commits are all on
the base branch already, so nothing but a name goes. Branches with work of their own are a decision
and stay.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from plainboard_protocol import TidySpec
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Landing, LandingStatus, Project, Repository, Train, utcnow
from ..services import flow_log
from ..services import landings as landing_service
from ..services import projects as project_service
from ..services import secrets as secret_service
from ..services import settings as settings_service
from .run_manager import RunManager
from .runner_client import RunnerError

log = logging.getLogger(__name__)

EVERY = timedelta(hours=24)
# A branch whose last commit is younger than this stays: one that was just started from the base
# branch has no commit of its own yet, and looks merged.
MIN_AGE_HOURS = 24
# With PlainBoard, or with the manager: these are never removed, merged or not.
KEPT = (LandingStatus.WAITING, LandingStatus.TRAIN, LandingStatus.BACK, LandingStatus.STUCK)


class BranchTidy:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runs: RunManager, interval: float):
        self._sm = sessionmaker
        self._runs = runs
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="branch-tidy")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Branch tidy tick failed")
            await asyncio.sleep(self._interval)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        if self._runs.paused:
            return
        async with self._sm() as s:
            due = or_(Repository.tidied_at.is_(None), Repository.tidied_at <= now - EVERY)
            for repo in list(await s.scalars(select(Repository).where(Repository.tidy_branches, due).order_by(Repository.id))):
                try:
                    await self._tidy(s, repo, now)
                except Exception:
                    log.exception("Tidying the branches of repository %s failed", repo.id)
                    await s.rollback()

    async def _tidy(self, s: AsyncSession, repo: Repository, now: datetime) -> None:
        project = await s.get(Project, repo.project_id)
        if project is None or project.paused_at is not None or not self._runs.runners.ready(project.id):
            return  # an external runner that is away: tried again when it is back
        env = secret_service.env(await secret_service.effective(s, project.id))
        credentials = settings_service.credentials(await settings_service.get_all(s), project_service.github_token(project), env)
        keep = set(await s.scalars(select(Landing.branch).where(Landing.repository_id == repo.id, Landing.status.in_(KEPT))))
        keep |= set(await s.scalars(select(Train.branch).where(Train.repository_id == repo.id, Train.status.in_(landing_service.RUNNING_TRAINS))))
        # The same repository under another base branch, in this or another project.
        keep |= set(await s.scalars(select(Repository.default_branch).where(Repository.url == repo.url, Repository.default_branch.is_not(None))))
        spec = TidySpec(
            credentials=credentials,
            key=landing_service.train_key(repo),
            remote=landing_service.remote(repo),
            base=repo.default_branch or None,
            keep=sorted(name for name in keep if name),
            github=landing_service.github_slug(repo),
            min_age_hours=MIN_AGE_HOURS,
        )
        repo.tidied_at = now  # also after a failure: tried again tomorrow, not with every look
        await s.commit()
        try:
            result = await self._runs.runners.of(project.id).tidy_branches(spec)
        except RunnerError as exc:
            await flow_log.record(s, project.id, "branches.tidy_failed", repository=repo.name, what=str(exc)[:300])
            return
        if result.deleted:
            await flow_log.record(s, project.id, "branches.tidied", repository=repo.name, deleted=len(result.deleted), branches=result.deleted[:40])
