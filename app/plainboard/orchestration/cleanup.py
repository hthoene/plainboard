"""Watches disk and memory without a model (see services/storage.py and services/cleanup.py).

Disk: it reads how full the agents' volume is (statvfs in the runner, no folder is measured for it),
every few minutes when it is high or grows fast, else once an hour, and keeps the samples in memory
for the trend. At the target, or when the forecast crosses it soon, the manager of the project that
causes it gets a cleanup run of its own (orchestration/cleanup_run.py: a small model decides on the
project's candidates, the manager hears the result in his updates without a turn). Nothing of a project
goes otherwise, except at the emergency level: then what is made again when needed, and the manager
hears exactly what. Above the target the big shared tool caches are trimmed,
oldest entries first, and the managers of the projects that use them hear it. Memory: servers nobody opened for long are stopped.

The target is a ceiling, not a budget: a regular cleanup keeps the disk small whatever its level (``_regular``). Daily: old
job logs, cache files unused for a month, temp leftovers untouched for days, the big shared tool caches trimmed, and of each
project what is safe and untouched for a while (build outputs of idle agents, extra work folders without own work, removed
agents' folders). After an assignment ended: that agent's extra work folders without own work. Never what is unsure, busy
or kept; each manager hears what of his project went (and what is unsure) without a turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from plainboard_protocol import CandidatesSpec, CleanedItem, CleanupResult, CleanupSpec, DeleteSpec, MachineUsage, TrimSpec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import get_config
from ..models import Agent, Assignment, Job, JobStatus, Project, Repository, utcnow
from ..services import cleanup as cleanup_service
from ..services import flow_log
from ..services import jobs as job_service
from ..services import board as board_service
from ..services import preview as preview_service
from ..services import retention
from ..services import scratchpad as scratchpad_service
from ..services import settings as settings_service
from ..services import storage
from ..services import tool_details
from . import inbox
from .cleanup_run import CleanupRuns, Order, need
from .run_manager import RunManager
from .runner_client import RunnerError

log = logging.getLogger(__name__)



def _on(runner_id: int | None):
    return Project.runner_id.is_(None) if runner_id is None else Project.runner_id == runner_id


DETAILS_EVERY = timedelta(days=1)  # tool messages past their limit are trimmed this often
MEASURE_EVERY = 3600  # folder sizes are measured again this often while the disk is high (else every 6 h)


class CleanupWatcher:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runs: RunManager, interval: float):
        self._sm = sessionmaker
        self._runs = runs
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        # Per runner (None: the built-in one), each watched for itself: its disk, its growth, its cleanups.
        self._measures: dict[int | None, list[tuple[float, dict[str, int]]]] = {}  # the last two folder measurements: who grows
        self._emergency_at: dict[int | None, datetime] = {}
        self._trim_at: dict[int | None, datetime] = {}
        self._details_at: datetime | None = None
        self.cleanups = CleanupRuns(runs)
        self._regulars: dict[int | None, asyncio.Task[None]] = {}  # the regular cleanup per runner, in the background
        self._ended_since: dict[int | None, datetime] = {}  # assignments ended after this were cleaned up after
        self._named: dict[int, set[str]] = {}  # project -> unsure paths its manager heard of from the daily cleanup

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="cleanup")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)  # its database connection is back before the engine goes
        for task in self._regulars.values():
            task.cancel()
        await asyncio.gather(*self._regulars.values(), return_exceptions=True)
        await self.cleanups.stop()

    async def wait_regular(self) -> None:
        """Until the regular cleanups started are over (tests)."""
        await asyncio.gather(*self._regulars.values(), return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            wait = self._interval
            try:
                wait = await self.tick()
            except Exception:
                log.exception("Cleanup tick failed")
            await asyncio.sleep(wait)

    async def tick(self, now: datetime | None = None) -> float:
        """One look at every connected runner a project uses; the seconds until the next."""
        now = now or utcnow()
        async with self._sm() as s:
            settings = await settings_service.get_all(s)
            await scratchpad_service.purge(s, now)  # archived scratchpads past their retention
        if self._details_at is None or now - self._details_at >= DETAILS_EVERY:
            self._details_at = now
            await self._tool_details(settings, now)
            await self._retention(now)
        hub = self._runs.runners
        waits = [self._interval]
        for runner_id in hub.in_use():
            if hub.connected(runner_id):
                waits.append(await self._look(runner_id, hub.client(runner_id), settings, now))
        return min(waits)

    async def _tool_details(self, settings: dict, now: datetime) -> None:
        """Tool details past their limit become a preview; each project's manager hears it once, not woken."""
        async with self._sm() as s:
            found = await tool_details.trim(s, now)
            kept = tool_details.days(settings)
            for project_id, (count, size) in found.items():
                manager = await s.scalar(select(Agent).where(Agent.project_id == project_id, Agent.is_manager))
                if manager is not None:
                    await inbox.push(s, manager.id, tool_details.notice(count, size, kept))
        if found:
            log.info("Trimmed tool details of %d messages (%d bytes)", sum(n for n, _ in found.values()), sum(b for _, b in found.values()))

    async def _retention(self, now: datetime) -> None:
        """Files of done tickets past their project's retention go (services/retention.py), also the copies on
        external runners; each project's manager hears it in one line, not woken."""
        try:
            async with self._sm() as s:
                found = await retention.purge(s, now)
                for project_id, due in found.items():
                    manager = await s.scalar(select(Agent).where(Agent.project_id == project_id, Agent.is_manager))
                    if manager is not None:
                        project = await s.get(Project, project_id)
                        days, default = due[0].attachment.purged_after_days, project.attachment_retention_days is None
                        await inbox.push(s, manager.id, retention.notice(len(due), sum(d.attachment.size for d in due), days, default))
                    for task_id in {d.attachment.task_id for d in due}:
                        await board_service.publish_task_id(s, task_id)
        except Exception:
            log.exception("Attachment retention failed")
        hub = self._runs.runners
        for runner_id in hub.in_use():
            if runner_id is None or not hub.connected(runner_id):
                continue  # the built-in runner reads the app's uploads volume; one away gets it at the next run
            async with self._sm() as s:
                names = await retention.purged_names(s, list(await s.scalars(select(Project.id).where(_on(runner_id)))))
            if names:
                try:
                    await hub.client(runner_id).delete_uploads(names)
                except RunnerError as exc:
                    log.warning("Deleted attachments not removed on runner %s: %s", runner_id, exc)

    async def _look(self, runner_id: int | None, client: Any, settings: dict, now: datetime) -> float:
        try:
            machine = await client.usage()
        except RunnerError:
            return self._interval
        volume = cleanup_service.workspace_volume(machine, get_config().workspaces_root)
        forecast = None
        if volume is not None and volume.total > 0:
            forecast = storage.trend(runner_id).add(now, volume.total, volume.free)
            await self._disk(runner_id, client, forecast, machine, settings, now)
        if settings["cleanup"] and cleanup_service.memory_short(machine, settings):
            await self._memory(runner_id, client, now)
        if settings["cleanup"] and volume is not None:
            await self._regular(runner_id, client, settings, now)
        return storage.next_check(forecast, settings, self._interval)

    def _keep_measure(self, runner_id: int | None, machine: MachineUsage) -> None:
        kept = self._measures.get(runner_id, [])
        if machine.measured_at is not None and (not kept or kept[-1][0] != machine.measured_at):
            self._measures[runner_id] = [*kept, (machine.measured_at, {f.path: f.bytes for f in machine.folders})][-2:]

    async def _disk(self, runner_id: int | None, client: Any, f: storage.Forecast, machine: MachineUsage, settings: dict, now: datetime) -> None:
        self._keep_measure(runner_id, machine)
        target = storage.limit(settings, "storage_notify_percent")
        if f.percent >= target - storage.WATCH_BELOW and machine.measured_at is not None and now.timestamp() - machine.measured_at > MEASURE_EVERY:
            with contextlib.suppress(RunnerError):
                await client.usage(measure=True)  # fresh folder sizes for the next notice: who grows
        emergency = storage.limit(settings, "storage_emergency_percent")
        at = self._emergency_at.get(runner_id)
        if settings["cleanup"] and f.percent >= emergency and (at is None or now - at >= storage.EMERGENCY_AGAIN):
            self._emergency_at[runner_id] = now
            await self._emergency(runner_id, client, f, machine, settings, now)
            return
        at = self._trim_at.get(runner_id)
        if settings["cleanup"] and f.percent >= target and (at is None or now - at >= storage.TRIM_AGAIN):
            self._trim_at[runner_id] = now
            if freed := await self._trim(runner_id, client, f, machine, settings, now):
                f = storage.Forecast(f.total, max(0, f.used - freed), f.rate)
        last = storage.last_notice(settings["storage_notices"], runner_id)
        if storage.closes(f, settings, last):
            async with self._sm() as s:
                await storage.close_last(s, runner_id)
            self.cleanups.forget()
            return
        reason = storage.notice_due(f, settings, last, now)
        if reason is not None:
            first = not last or bool(last.get("closed"))
            await self._notify(runner_id, client, reason, f, machine, settings, now, others=first)

    async def _notify(self, runner_id: int | None, client: Any, reason: str, f: storage.Forecast, machine: MachineUsage, settings: dict, now: datetime, others: bool, before: str = "") -> None:
        async with self._sm() as s:
            try:
                found = await storage.listing(s, client, machine, runner_id=runner_id)
            except RunnerError as exc:
                log.warning("Cleanup candidates not listed: %s", exc)
                return
            main, why = storage.cause(found, self._measures.get(runner_id, []))
            if main is None:
                return
            told = [main.name]
            text = storage.notice_text(reason, f, settings, found, main, why)
            await self._clean(s, main, f"{before}\n\n{text}" if before else text, found, f, settings, first=others or bool(before), before=before)
            if others:
                for project in found.projects:
                    size = storage.freeable(found, project.id)
                    if project.id != main.id and size >= storage.OTHERS_FROM:
                        await self._clean(s, project, storage.others_text(f, settings, main, size), found, f, settings, first=True)
                        told.append(project.name)
            await storage.remember(s, "storage_notices", storage.notice_record(reason if not before else "emergency", f, now, told, settings, runner_id))

    async def _clean(self, s: AsyncSession, project: Project, text: str, found: storage.Listing, f: storage.Forecast, settings: dict, first: bool, before: str = "") -> None:
        """A cleanup run of its own decides on the project's candidates; the manager hears the result without a turn.
        One at a time per project, and none when nothing new can go: then the manager hears it once, also without a turn."""
        manager = await s.scalar(select(Agent).where(Agent.project_id == project.id, Agent.is_manager))
        if manager is None or self.cleanups.running(project.id):
            return
        items = self.cleanups.offer(found, project.id)
        unsure = [i for i in found.visible(project.id) if i.candidate.safety == "unsure" and i.candidate.bytes >= storage.SMALL]
        high = f.percent >= storage.limit(settings, "storage_notify_percent")
        if items:
            head = f"Disk space: {storage.forecast_text(f, settings)}."
            self.cleanups.start(Order(project.id, text, items, head, need(f, settings), found.busy, unsure, high))
        elif first:
            lines = [f"{before}\n" if before else "", f"Disk space: {storage.forecast_text(f, settings)}. Nothing of your project can go now by the safety rules: no cleanup run."]
            lines += storage.grouped(found.visible(project.id), project.id, found.busy, ("unsure", "keep"))
            await inbox.push(s, manager.id, "\n".join(x for x in lines if x))
            if high and unsure:  # only the manager can judge them, and the disk is above the target
                await self._runs.request_wake(s, manager.id)

    # --- the regular cleanup: the disk is kept small all the time, the target is only the ceiling -----------------

    async def _regular(self, runner_id: int | None, client: Any, settings: dict, now: datetime) -> None:
        """Daily, and after assignments ended, what is made again when needed goes, in the background (one at a time per runner)."""
        task = self._regulars.get(runner_id)
        if task is not None and not task.done():
            return
        key = "built-in" if runner_id is None else str(runner_id)
        last = (settings.get("storage_routine_at") or {}).get(key)
        try:
            daily = last is None or now - datetime.fromisoformat(last) >= storage.ROUTINE_EVERY
        except (TypeError, ValueError):
            daily = True
        since = self._ended_since.setdefault(runner_id, now)  # from the watcher's start: none before
        async with self._sm() as s:
            ended = set(await s.scalars(select(Assignment.agent_id).join(Project, Project.id == Assignment.project_id).where(Assignment.finished_at > since, _on(runner_id))))
            if daily:
                done = dict((await settings_service.get(s, "storage_routine_at")) or {})
                await settings_service.update(s, {"storage_routine_at": {**done, key: now.isoformat()}}, managed=True)
        self._ended_since[runner_id] = now
        if daily or ended:
            self._regulars[runner_id] = asyncio.create_task(self._regular_run(runner_id, client, daily, ended, now), name=f"cleanup-regular-{key}")

    async def _regular_run(self, runner_id: int | None, client: Any, daily: bool, ended: set[int], now: datetime) -> None:
        try:
            if ended:
                await self._after(runner_id, client, ended, now)
            if daily:
                await self._daily(runner_id, client, now)
        except asyncio.CancelledError:
            raise
        except RunnerError as exc:
            log.warning("Regular cleanup on runner %s failed: %s", runner_id, exc)
        except Exception:
            log.exception("Regular cleanup on runner %s failed", runner_id)

    async def _after(self, runner_id: int | None, client: Any, agent_ids: set[int], now: datetime) -> None:
        """Agents whose assignment ended and that are idle now: their extra work folders without own work go (worktrees,
        more clones); the manager hears it without a turn. Only their folders are looked at, not the shared home."""
        root = get_config().workspaces_root.rstrip("/")
        async with self._sm() as s:
            projects = list(await s.scalars(select(Project).where(_on(runner_id)).order_by(Project.id)))
            busy = await cleanup_service.busy_agents(s)
            agents = [a for a in await s.scalars(select(Agent).where(Agent.id.in_(agent_ids))) if a.id not in busy]
            clones: dict[int, list[str]] = defaultdict(list)
            for pid, name in (await s.execute(select(Repository.project_id, Repository.name))).all():
                clones[pid].append(name)
            await s.commit()
        folders = cleanup_service.folders(projects, agents, busy, {}, [], root, {p.id: clones[p.id] for p in projects})
        if not folders:
            return
        found = await client.cleanup_candidates(CandidatesSpec(folders=folders, home=False))
        mine = [f.path.rstrip("/") + "/" for f in folders]
        paths = [c.path for c in found if c.what == "workdir" and c.safety == "safe" and any(c.path.startswith(m) for m in mine)]
        if not paths:
            return
        result = await client.cleanup_delete(DeleteSpec(paths=paths, folders=folders))
        async with self._sm() as s:
            for agent in agents:
                project = next((p for p in projects if p.id == agent.project_id), None)
                if project is None:
                    continue
                prefix = f"{root}/{project.slug}/{agent.slug}/"
                went = [i for i in result.items if i.path.startswith(prefix)]
                if not went:
                    continue
                freed = sum(i.bytes for i in went)
                await flow_log.record(s, project.id, "storage.cleaned", agent_id=agent.id, level="after", freed=freed, items=[i.model_dump() for i in went[:40]])
                await storage.remember(s, "storage_cleanups", cleanup_service.record("after", now, 0, 0, CleanupResult(freed=freed, items=went), by=agent.name, project=project.name))
                if (manager := await s.scalar(select(Agent).where(Agent.project_id == project.id, Agent.is_manager))) is not None:
                    await inbox.push(s, manager.id, storage.after_text(agent.name, went))
        log.info("Cleaned up after assignments: %d bytes", result.freed)

    async def _daily(self, runner_id: int | None, client: Any, now: datetime) -> None:
        """Whatever the disk: old job logs, cache files unused for a month and temp leftovers; the big shared tool caches
        trimmed; of each project what is safe and untouched (``storage.routine_items``). Each manager hears what of his
        project went and what is unsure (never deleted here), without a turn; the storage view keeps the whole."""
        root = get_config().workspaces_root.rstrip("/")
        machine = await client.usage()
        volume = cleanup_service.workspace_volume(machine, root)
        total, free = (volume.total, volume.free) if volume else (0, 0)
        whole = CleanupResult()
        base = await client.clean_up(CleanupSpec(level="soft", need=0, tmp_days=storage.TMP_DAYS))
        if base.skipped != ["a cleanup runs already"]:
            whole.items += base.items
            whole.freed += base.freed
        async with self._sm() as s:
            found = await storage.listing(s, client, machine, runner_id=runner_id)
        trimmed = await client.trim_caches(TrimSpec(need=storage.ROUTINE_TRIM, large=storage.TRIM_LARGE, keep=storage.TRIM_KEEP, folders=found.folders))
        whole.items += trimmed.items
        whole.freed += trimmed.freed
        went: dict[int, list[CleanedItem]] = {}
        unix = cleanup_service._unix(now)
        for project in found.projects:
            if items := storage.routine_items(found, project.id, unix):
                result = await client.cleanup_delete(DeleteSpec(paths=[i.candidate.path for i in items], folders=found.folders))
                went[project.id] = result.items
                whole.items += result.items
                whole.freed += result.freed
                whole.skipped += result.skipped
        async with self._sm() as s:
            for project in found.projects:
                mine = went.get(project.id, []) + [i for i in base.items if i.what == "tmp" and project.slug in i.path.rsplit("/", 1)[-1]]
                uses = [i for i in trimmed.items if any((p := cleanup_service.project_of(u, root, found.projects)) is not None and p.id == project.id for u in i.users)]
                named = self._named.setdefault(project.id, set())
                unsure = [i for i in storage.unsure_items(found, project.id) if i.candidate.path not in named]
                named.update(i.candidate.path for i in unsure)
                if not (mine or uses or unsure):
                    continue
                if mine or uses:
                    await flow_log.record(s, project.id, "storage.cleaned", level="routine", freed=sum(i.bytes for i in mine + uses), items=[i.model_dump() for i in (mine + uses)[:40]])
                if (manager := await s.scalar(select(Agent).where(Agent.project_id == project.id, Agent.is_manager))) is not None:
                    await inbox.push(s, manager.id, storage.routine_text(mine, uses, unsure, project.id, found.busy))
            if whole.freed:
                await storage.remember(s, "storage_cleanups", cleanup_service.record("routine", now, total, free, whole))
        log.info("Regular cleanup on runner %s freed %d bytes", runner_id, whole.freed)

    async def _emergency(self, runner_id: int | None, client: Any, f: storage.Forecast, machine: MachineUsage, settings: dict, now: datetime) -> None:
        """Nearly full: delete what is made again when needed (idle agents' folders only), and say exactly what."""
        root = get_config().workspaces_root
        target = storage.limit(settings, "storage_notify_percent")
        async with self._sm() as s:
            projects = list(await s.scalars(select(Project).where(_on(runner_id)).order_by(Project.id)))  # on this runner
            agents = list(await s.scalars(select(Agent)))
            folders = cleanup_service.folders(projects, agents, await cleanup_service.busy_agents(s), await cleanup_service.last_turns(s), machine.folders, root)
        free = f.total - f.used
        spec = CleanupSpec(level="soft", need=cleanup_service.need(f.total, free, target), folders=[x for x in folders if not x.busy])
        try:
            result = await client.clean_up(spec)
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
            await self._notify(runner_id, client, "over", after, machine, settings, now, others=True, before=text)
        elif main is not None:
            async with self._sm() as s:
                if (manager := await s.scalar(select(Agent).where(Agent.project_id == main.id, Agent.is_manager))) is not None:
                    await inbox.push(s, manager.id, text)  # done already: nothing for him to do, no turn
                await storage.remember(s, "storage_notices", storage.notice_record("emergency", after, now, [main.name], settings, runner_id))

    async def _trim(self, runner_id: int | None, client: Any, f: storage.Forecast, machine: MachineUsage, settings: dict, now: datetime) -> int:
        """Above the target: shrink the big shared tool caches (oldest entries first) to a little below it; the managers of
        the projects whose clones use those tools hear what went. The bytes freed."""
        root = get_config().workspaces_root
        target = storage.limit(settings, "storage_notify_percent")
        async with self._sm() as s:
            projects = list(await s.scalars(select(Project).where(_on(runner_id)).order_by(Project.id)))  # on this runner
            agents = list(await s.scalars(select(Agent)))
            folders = cleanup_service.folders(projects, agents, await cleanup_service.busy_agents(s), await cleanup_service.last_turns(s), machine.folders, root)
        free = f.total - f.used
        spec = TrimSpec(need=cleanup_service.need(f.total, free, target - storage.CLOSE_BELOW), large=storage.TRIM_LARGE, keep=storage.TRIM_KEEP, folders=folders)
        try:
            result = await client.trim_caches(spec)
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

    async def _memory(self, runner_id: int | None, client: Any, now: datetime) -> None:
        """Stop servers whose agent is idle and that nobody opened for ``SERVER_IDLE``. Only servers that
        answer on a port: a long job without one may be work someone waits for."""
        async with self._sm() as s:
            busy = await cleanup_service.busy_agents(s, jobs=False)
            here = set(await s.scalars(select(Project.id).where(_on(runner_id))))  # the other runners' memory is other machines'
            running = [j for j in await s.scalars(select(Job).where(Job.status == JobStatus.RUNNING, Job.assignment_id.is_(None))) if j.agent_id not in busy and j.project_id in here]
            if not running:
                return
            try:
                ports = await client.job_ports([j.runner_id for j in running])
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
