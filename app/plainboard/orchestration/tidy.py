"""Removes the branches that merging left behind.

PlainBoard deletes a branch when it lands it. What it did not land itself stays on the host: a
branch that was merged by hand or with a pull request (also squashed or rebased), one that landed
inside another branch, one from before PlainBoard landed anything. These are removed
(``repository.tidy_branches``) every hour and after each landing, without any model: only branches
whose content is proven to be on the base branch already (the runner's ``trains.merged_how``), so
nothing but a name goes. Never one that PlainBoard has in flight, one of a running train, one with
a young commit, a protected one or one of an open pull request. The manager reads, without a turn
of its own, what was removed and how to restore it. Branches with work of their own are a decision:
they stay, and one whose last commit is old is named to the manager once.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from plainboard_protocol import TidyBranch, TidySpec
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Landing, LandingStatus, Project, Repository, Train, utcnow
from ..services import flow_log
from ..services import landings as landing_service
from ..services import agents as agent_service
from ..services import permissions
from . import inbox
from .run_manager import RunManager
from .runner_client import RunnerError

log = logging.getLogger(__name__)

EVERY = timedelta(hours=1)
# A branch whose last commit is younger than this stays: one a member just pushed is never touched.
MIN_AGE_HOURS = 1
# A branch with work of its own whose last commit is older than this is named to the manager, once.
STALE_DAYS = 14
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
            await asyncio.gather(self._task, return_exceptions=True)  # its database connection is back before the engine goes

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
        if project is None or project.paused_at is not None or project.limit_paused_at is not None or not self._runs.runners.ready(project.id):
            return  # an external runner that is away: tried again when it is back
        credentials = await permissions.credentials(s, project)
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
            stale_days=STALE_DAYS,
        )
        repo.tidied_at = now  # also after a failure: tried again in an hour, not with every look
        await s.commit()
        try:
            result = await self._runs.runners.of(project.id).tidy_branches(spec)
        except RunnerError as exc:
            await flow_log.record(s, project.id, "branches.tidy_failed", repository=repo.name, what=str(exc)[:300])
            return
        manager = await agent_service.get_manager(s, project.id)
        base = spec.base or "the base branch"
        removed = result.removed or [TidyBranch(name=name, sha="") for name in result.deleted]  # a runner of an older version
        if removed:
            await flow_log.record(s, project.id, "branches.tidied", repository=repo.name, deleted=len(removed), branches={b.name: b.sha for b in removed[:40]})
            listing = "; ".join(f"{b.name} at {b.sha or '?'}" + (f" ({b.how})" if b.how else "") for b in removed)
            await inbox.push(
                s, manager.id,
                f"Removed {len(removed)} branch{'es' if len(removed) != 1 else ''} from {repo.name} whose content is all on {base}: {listing}. "
                "Nothing else is needed. To restore one: git push origin <commit>:refs/heads/<name>.",
            )
        listed = set(repo.tidy_listed or [])
        new = [b for b in result.unmerged if b.name not in listed]
        if new:
            listing = "; ".join(f"{b.name} at {b.sha[:12]} (last commit {b.days} days ago)" for b in new)
            await inbox.push(
                s, manager.id,
                f"Branches in {repo.name} with work that is not on {base}, untouched for over {STALE_DAYS} days: {listing}. "
                "PlainBoard does not delete them. Decide whether one is still wanted (land it, ask the user) or can go.",
            )
        repo.tidy_listed = sorted(b.name for b in result.unmerged)  # named once; again only after it changed and got old again
        await s.commit()
