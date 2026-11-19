"""Cleans up when the workspaces' volume or the memory run short (see services/cleanup.py).

A watcher without a model: once an hour it reads how full the volume is (statvfs in the runner, no
folder is measured for it) and how much memory is available. Only when that is below a threshold does
anything happen: the runner removes what can be rebuilt, or servers nobody opened for long are
stopped. The manager gets a turn only when a person is needed: still short of room after a hard cleanup,
at most once a day.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime

from plainboard_protocol import CleanupResult, CleanupSpec, MachineUsage, VolumeUsage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import get_config
from ..models import Agent, Job, JobStatus, Project, utcnow
from ..services import cleanup as cleanup_service
from ..services import flow_log
from ..services import jobs as job_service
from ..services import preview as preview_service
from ..services import settings as settings_service
from . import inbox
from .run_manager import RunManager
from .runner_client import RunnerError

log = logging.getLogger(__name__)


class CleanupWatcher:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runs: RunManager, interval: float):
        self._sm = sessionmaker
        self._runs = runs
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="cleanup")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Cleanup tick failed")
            await asyncio.sleep(self._interval)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        async with self._sm() as s:
            settings = await settings_service.get_all(s)
        if not settings["cleanup"]:
            return
        try:
            machine = await self._runs.runner.usage()
        except RunnerError:
            return
        volume = cleanup_service.workspace_volume(machine, get_config().workspaces_root)
        if volume is not None:
            await self._disk(volume, machine, settings, now)
        if cleanup_service.memory_short(machine, settings):
            await self._memory(now)

    async def _disk(self, volume: VolumeUsage, machine: MachineUsage, settings: dict, now: datetime) -> None:
        level = cleanup_service.level(volume.total, volume.free, settings)
        if level is None or not cleanup_service.due(settings["cleanup_last"], level, now):
            return
        root = get_config().workspaces_root
        async with self._sm() as s:
            projects = list(await s.scalars(select(Project).order_by(Project.id)))
            agents = list(await s.scalars(select(Agent)))
            folders = cleanup_service.folders(projects, agents, await cleanup_service.busy_agents(s), await cleanup_service.last_turns(s), machine.folders, root)
        need = cleanup_service.need(volume.total, volume.free, settings)
        try:
            result = await self._runs.runner.clean_up(CleanupSpec(level=level, need=need, folders=folders))  # type: ignore[arg-type]
        except RunnerError as exc:
            log.warning("Cleanup failed: %s", exc)
            return
        if result.skipped == ["a cleanup runs already"]:
            return
        log.info("Cleanup (%s) freed %d bytes", level, result.freed)

        sizes: dict[int, int] = defaultdict(int)
        for f in machine.folders:
            if (project := cleanup_service.project_of(f.path, root, projects)) is not None:
                sizes[project.id] += f.bytes
        main = max(projects, key=lambda p: (sizes.get(p.id, 0), -p.id), default=None)
        async with self._sm() as s:
            told = False
            still_short = (volume.free + result.freed) * 100 < volume.total * cleanup_service.percent(settings, "cleanup_soft_free_percent")
            told_at = settings["cleanup_told_at"]
            if level == "hard" and still_short and main is not None and (not told_at or now - datetime.fromisoformat(told_at) >= cleanup_service.TELL_EVERY):
                manager = await s.scalar(select(Agent).where(Agent.project_id == main.id, Agent.is_manager))
                if manager is not None:
                    biggest = sorted(((f.path, f.bytes) for f in machine.folders), key=lambda f: -f[1])[:6]
                    await inbox.push(s, manager.id, cleanup_service.text(volume.total, volume.free, result, biggest))
                    await self._runs.request_wake(s, manager.id)
                    told = True
            await self._log(s, result, projects, main, level, volume, root)
            values = {"cleanup_last": cleanup_service.record(level, now, volume.total, volume.free, need, result, told)}
            if told:
                values["cleanup_told_at"] = now.isoformat()
            await settings_service.update(s, values, managed=True)

    async def _log(self, s: AsyncSession, result: CleanupResult, projects: list[Project], main: Project | None, level: str, volume: VolumeUsage, root: str) -> None:
        """One work-log event per project whose folders were cleaned; caches and logs with the biggest project."""
        items: dict[int, list] = defaultdict(list)
        for item in result.items:
            project = cleanup_service.project_of(item.path, root, projects) or main
            if project is not None:
                items[project.id].append(item)
        for project_id, found in items.items():
            await flow_log.record(
                s,
                project_id,
                "storage.cleaned",
                level=level,
                freed=sum(i.bytes for i in found),
                freed_total=result.freed,
                free_before=volume.free,
                total=volume.total,
                items=[i.model_dump() for i in sorted(found, key=lambda i: -i.bytes)[:40]],
            )

    async def _memory(self, now: datetime) -> None:
        """Stop servers whose agent is idle and that nobody opened for ``SERVER_IDLE``. Only servers that
        answer on a port: a long job without one may be work someone waits for."""
        async with self._sm() as s:
            busy = await cleanup_service.busy_agents(s, jobs=False)
            running = [j for j in await s.scalars(select(Job).where(Job.status == JobStatus.RUNNING, Job.assignment_id.is_(None))) if j.agent_id not in busy]
            if not running:
                return
            try:
                ports = await self._runs.runner.job_ports([j.runner_id for j in running])
            except RunnerError:
                return
            by_agent: dict[int, list[int]] = defaultdict(list)  # a backend behind a gateway is in use when the gateway is
            for job in running:
                by_agent[job.agent_id] += [p.port for p in ports.get(job.runner_id, [])]
            idle = cleanup_service.SERVER_IDLE.total_seconds()
            stopped: list[Job] = []
            for job in sorted(running, key=lambda j: j.created_at):
                if not ports.get(job.runner_id) or now - job.created_at < cleanup_service.SERVER_IDLE:
                    continue
                if not preview_service.unseen_for(idle, by_agent[job.agent_id], job.project_id):
                    continue
                if await self._runs.stop_job(s, job, settle=False):
                    stopped.append(job)
                    await inbox.push(s, job.agent_id, f"{job_service.label(job)} was stopped: memory was short and nobody had opened it for 12 hours. Start it again when it is needed.")
                    await flow_log.record(s, job.project_id, "storage.server_stopped", agent_id=job.agent_id, job=job.id, command=job.command[:200])
            if stopped:
                await settings_service.update(s, {"cleanup_servers_last": cleanup_service.stopped_record(now, stopped)}, managed=True)
