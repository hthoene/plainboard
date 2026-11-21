"""Watches disk and memory without a model (see services/storage.py and services/cleanup.py).

Disk: it reads how full the agents' volume is (statvfs in the runner, no folder is measured for it),
every few minutes when it is high or grows fast, else once an hour, and keeps the samples in memory
for the trend. At the target, or when the forecast crosses it soon, the manager of the project that
causes it gets a notice with the biggest items and what is safe to delete; he deletes with his tools.
Nothing of a project goes by itself, except at the emergency level: then what is made again when
needed, and the manager hears exactly what. Above the target the big shared tool caches are trimmed,
oldest entries first, and the managers of the projects that use them hear it. Memory: servers nobody opened for long are stopped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from datetime import datetime

from plainboard_protocol import CleanedItem, CleanupResult, CleanupSpec, MachineUsage, TrimSpec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import get_config
from ..models import Agent, Job, JobStatus, Project, utcnow
from ..services import cleanup as cleanup_service
from ..services import flow_log
from ..services import jobs as job_service
from ..services import preview as preview_service
from ..services import settings as settings_service
from ..services import storage
from . import inbox
from .run_manager import RunManager
from .runner_client import RunnerError

log = logging.getLogger(__name__)

MEASURE_EVERY = 3600  # folder sizes are measured again this often while the disk is high (else every 6 h)


class CleanupWatcher:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runs: RunManager, interval: float):
        self._sm = sessionmaker
        self._runs = runs
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._measures: list[tuple[float, dict[str, int]]] = []  # the last two folder measurements: who grows
        self._emergency_at: datetime | None = None
        self._trim_at: datetime | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="cleanup")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            wait = self._interval
            try:
                wait = await self.tick()
            except Exception:
                log.exception("Cleanup tick failed")
            await asyncio.sleep(wait)

    async def tick(self, now: datetime | None = None) -> float:
        """One look; the seconds until the next."""
        now = now or utcnow()
        async with self._sm() as s:
            settings = await settings_service.get_all(s)
        try:
            machine = await self._runs.runner.usage()
        except RunnerError:
            return self._interval
        volume = cleanup_service.workspace_volume(machine, get_config().workspaces_root)
        forecast = None
        if volume is not None and volume.total > 0:
            forecast = storage.TREND.add(now, volume.total, volume.free)
            await self._disk(forecast, machine, settings, now)
        if settings["cleanup"] and cleanup_service.memory_short(machine, settings):
            await self._memory(now)
        return storage.next_check(forecast, settings, self._interval)

    def _keep_measure(self, machine: MachineUsage) -> None:
        if machine.measured_at is not None and (not self._measures or self._measures[-1][0] != machine.measured_at):
            self._measures = [*self._measures, (machine.measured_at, {f.path: f.bytes for f in machine.folders})][-2:]

    async def _disk(self, f: storage.Forecast, machine: MachineUsage, settings: dict, now: datetime) -> None:
        self._keep_measure(machine)
        target = storage.limit(settings, "storage_notify_percent")
        if f.percent >= target - storage.WATCH_BELOW and machine.measured_at is not None and now.timestamp() - machine.measured_at > MEASURE_EVERY:
            with contextlib.suppress(RunnerError):
                await self._runs.runner.usage(measure=True)  # fresh folder sizes for the next notice: who grows
        emergency = storage.limit(settings, "storage_emergency_percent")
        if settings["cleanup"] and f.percent >= emergency and (self._emergency_at is None or now - self._emergency_at >= storage.EMERGENCY_AGAIN):
            self._emergency_at = now
            await self._emergency(f, machine, settings, now)
            return
        if settings["cleanup"] and f.percent >= target and (self._trim_at is None or now - self._trim_at >= storage.TRIM_AGAIN):
            self._trim_at = now
            if freed := await self._trim(f, machine, settings, now):
                f = storage.Forecast(f.total, max(0, f.used - freed), f.rate)
        notices = settings["storage_notices"] or []
        last = notices[0] if notices else None
        if storage.closes(f, settings, last):
            async with self._sm() as s:
                await storage.close_last(s)
            return
        reason = storage.notice_due(f, settings, last, now)
        if reason is not None:
            first = not last or bool(last.get("closed"))
            await self._notify(reason, f, machine, settings, now, others=first)

    async def _notify(self, reason: str, f: storage.Forecast, machine: MachineUsage, settings: dict, now: datetime, others: bool, before: str = "") -> None:
        async with self._sm() as s:
            try:
                found = await storage.listing(s, self._runs.runner, machine)
            except RunnerError as exc:
                log.warning("Cleanup candidates not listed: %s", exc)
                return
            main, why = storage.cause(found, self._measures)
            if main is None:
                return
            told = [main.name]
            text = storage.notice_text(reason, f, settings, found, main, why)
            await self._tell(s, main, f"{before}\n\n{text}" if before else text)
            if others:
                for project in found.projects:
                    size = storage.freeable(found, project.id)
                    if project.id != main.id and size >= storage.OTHERS_FROM:
                        await self._tell(s, project, storage.others_text(f, settings, main, size))
                        told.append(project.name)
            await storage.remember(s, "storage_notices", storage.notice_record(reason if not before else "emergency", f, now, told, settings))

    async def _tell(self, s: AsyncSession, project: Project, text: str) -> None:
        """Disk space can't wait: the manager gets a turn even where he is not woken for updates."""
        manager = await s.scalar(select(Agent).where(Agent.project_id == project.id, Agent.is_manager))
        if manager is not None:
            await inbox.push(s, manager.id, text)
            await self._runs.request_wake(s, manager.id)

    async def _emergency(self, f: storage.Forecast, machine: MachineUsage, settings: dict, now: datetime) -> None:
        """Nearly full: delete what is made again when needed (idle agents' folders only), and say exactly what."""
        root = get_config().workspaces_root
        target = storage.limit(settings, "storage_notify_percent")
        async with self._sm() as s:
            projects = list(await s.scalars(select(Project).where(Project.runner_id.is_(None)).order_by(Project.id)))  # on this runner
            agents = list(await s.scalars(select(Agent)))
            folders = cleanup_service.folders(projects, agents, await cleanup_service.busy_agents(s), await cleanup_service.last_turns(s), machine.folders, root)
        free = f.total - f.used
        spec = CleanupSpec(level="soft", need=cleanup_service.need(f.total, free, target), folders=[x for x in folders if not x.busy])
        try:
            result = await self._runs.runner.clean_up(spec)
        except RunnerError as exc:
            log.warning("Emergency cleanup failed: %s", exc)
            return
        if result.skipped == ["a cleanup runs already"]:
            return
        log.warning("Emergency cleanup at %.0f %% freed %d bytes", f.percent, result.freed)
        after = storage.Forecast(f.total, max(0, f.used - result.freed), f.rate)
        sizes: dict[int, int] = defaultdict(int)
        for x in machine.folders:
            if (project := cleanup_service.project_of(x.path, root, projects)) is not None:
                sizes[project.id] += x.bytes
        main = max(projects, key=lambda p: (sizes.get(p.id, 0), -p.id), default=None)
        async with self._sm() as s:
            await self._log(s, result, projects, main, f, root)
            await storage.remember(s, "storage_cleanups", cleanup_service.record("emergency", now, f.total, free, result, project=main.name if main else ""))
        text = storage.emergency_text(result, f.percent, settings, after.percent)
        if after.percent >= target:
            await self._notify("over", after, machine, settings, now, others=True, before=text)
        elif main is not None:
            async with self._sm() as s:
                await self._tell(s, main, text)
                await storage.remember(s, "storage_notices", storage.notice_record("emergency", after, now, [main.name], settings))

    async def _trim(self, f: storage.Forecast, machine: MachineUsage, settings: dict, now: datetime) -> int:
        """Above the target: shrink the big shared tool caches (oldest entries first) to a little below it; the managers of
        the projects whose clones use those tools hear what went. The bytes freed."""
        root = get_config().workspaces_root
        target = storage.limit(settings, "storage_notify_percent")
        async with self._sm() as s:
            projects = list(await s.scalars(select(Project).where(Project.runner_id.is_(None)).order_by(Project.id)))  # on this runner
            agents = list(await s.scalars(select(Agent)))
            folders = cleanup_service.folders(projects, agents, await cleanup_service.busy_agents(s), await cleanup_service.last_turns(s), machine.folders, root)
        free = f.total - f.used
        spec = TrimSpec(need=cleanup_service.need(f.total, free, target - storage.CLOSE_BELOW), large=storage.TRIM_LARGE, keep=storage.TRIM_KEEP, folders=folders)
        try:
            result = await self._runs.runner.trim_caches(spec)
        except RunnerError as exc:
            log.warning("Shared caches not trimmed: %s", exc)
            return 0
        if not result.items:
            return 0
        by_project: dict[int, list[CleanedItem]] = defaultdict(list)
        for item in result.items:
            for pid in {p.id for u in item.users if (p := cleanup_service.project_of(u, root, projects)) is not None}:
                by_project[pid].append(item)
        async with self._sm() as s:
            await storage.remember(s, "storage_cleanups", cleanup_service.record("trim", now, f.total, free, result))
            for project in projects:
                if found := by_project.get(project.id):
                    await flow_log.record(s, project.id, "storage.cleaned", level="trim", freed=sum(i.bytes for i in found), items=[i.model_dump() for i in found])
                    manager = await s.scalar(select(Agent).where(Agent.project_id == project.id, Agent.is_manager))
                    if manager is not None:
                        await inbox.push(s, manager.id, storage.trim_text(found, f.percent, settings))
        log.info("Trimmed shared caches at %.0f %%: %d bytes", f.percent, result.freed)
        return result.freed

    async def _log(self, s: AsyncSession, result: CleanupResult, projects: list[Project], main: Project | None, f: storage.Forecast, root: str) -> None:
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
                level="emergency",
                freed=sum(i.bytes for i in found),
                freed_total=result.freed,
                free_before=f.total - f.used,
                total=f.total,
                items=[i.model_dump() for i in sorted(found, key=lambda i: -i.bytes)[:40]],
            )

    async def _memory(self, now: datetime) -> None:
        """Stop servers whose agent is idle and that nobody opened for ``SERVER_IDLE``. Only servers that
        answer on a port: a long job without one may be work someone waits for."""
        async with self._sm() as s:
            busy = await cleanup_service.busy_agents(s, jobs=False)
            elsewhere = set(await s.scalars(select(Project.id).where(Project.runner_id.is_not(None))))  # their memory is another machine's
            running = [j for j in await s.scalars(select(Job).where(Job.status == JobStatus.RUNNING, Job.assignment_id.is_(None))) if j.agent_id not in busy and j.project_id not in elsewhere]
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
