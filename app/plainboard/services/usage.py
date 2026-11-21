"""Disk and memory PlainBoard uses: per volume, per project and agent, and what all agents share.

The runner measures its side (``runner/plainboard_runner/usage.py``: folder sizes in the background,
rarely); this puts its numbers together with the app's own volumes and the projects' uploads.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from dataclasses import dataclass, field
from pathlib import Path

from plainboard_protocol import MachineUsage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..models import Agent, Attachment, Project, Runner
from . import cleanup as cleanup_service
from . import settings as settings_service
from ..schemas import AgentUsageOut, FolderOut, MachineOut, ProjectUsageOut, RunnerUsageOut, UsageOut, VolumeOut

LOW_FREE_SHARE = 0.1  # a volume is short of room below a tenth free ...
LOW_FREE_BYTES = 10 * 2**30  # ... or below 10 GB
TRAIN = re.compile(r"^p(\d+)-r\d+$")  # landings.train_key


@dataclass
class Volume:
    kind: str
    fsid: int
    total: int
    used: int
    free: int


@dataclass
class Machine:
    """The app's side: its own volumes and the size of its data folder."""

    volumes: list[Volume] = field(default_factory=list)
    data: int | None = None


def low(total: int, free: int) -> bool:
    return total > 0 and (free < total * LOW_FREE_SHARE or free < LOW_FREE_BYTES)


def _volume(kind: str, path: Path) -> Volume | None:
    try:
        st = os.statvfs(path)
        fsid = st.f_fsid or os.stat(path).st_dev
    except OSError:
        return None
    return Volume(kind, fsid, st.f_blocks * st.f_frsize, (st.f_blocks - st.f_bfree) * st.f_frsize, st.f_bavail * st.f_frsize)


def _size(folder: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_blocks * 512
            except OSError:
                pass
    return total


def own_machine() -> Machine:
    cfg = get_config()
    volumes = [v for v in (_volume("data", cfg.data_dir), _volume("uploads", cfg.uploads_dir)) if v]
    return Machine(volumes=volumes, data=_size(cfg.data_dir) if cfg.data_dir.is_dir() else None)


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _train_project(path: str, root: str) -> int | None:
    """The project of a train's clone (outside the workspaces)."""
    match = None if _within(path, root) else TRAIN.match(Path(path).name)
    return int(match[1]) if match else None


CPU_WINDOWS_HOURS = (1, 6, 24)


class _Cpu:
    """The runner's CPU history by owner: what is asked is a series per owner (cores per slot) and its averages."""

    def __init__(self, machine: MachineUsage) -> None:
        self.step = machine.cpu_step or 300
        buckets = {int(b.start // self.step): b for b in machine.cpu_history}
        self.slots = 24 * 3600 // self.step
        last = max(buckets, default=None)
        self.first = None if last is None else last - self.slots + 1
        self.buckets = [] if self.first is None else [buckets.get(self.first + i) for i in range(self.slots)]
        self.now = machine.cpu_now

    def series(self, owner) -> tuple[list[float], list[float]]:
        """Cores per slot, and the averages over the windows. ``owner``: folder -> bool."""
        seconds = [sum(v for f, v in b.seconds.items() if owner(f)) if b else 0.0 for b in self.buckets]
        spans = [b.span if b else 0.0 for b in self.buckets]
        per_slot = [round(sec / span, 3) if span else 0.0 for sec, span in zip(seconds, spans)]
        avg = []
        for hours in CPU_WINDOWS_HOURS:
            n = hours * 3600 // self.step
            span = sum(spans[-n:])
            avg.append(round(sum(seconds[-n:]) / span, 3) if span else 0.0)
        return per_slot, avg

    def cores(self, owner) -> float:
        return round(sum(v for f, v in self.now.items() if owner(f)), 3)


def summarize(projects: list[Project], agents: list[Agent], uploads: dict[int, int], machine: MachineUsage | None, own: Machine) -> UsageOut:
    """``machine``: what the runner measured; None if it could not be asked."""
    cfg = get_config()
    root = cfg.workspaces_root.rstrip("/")
    runner = machine or MachineUsage()

    volumes: dict[int, tuple[list[str], Volume]] = {}
    runner_volumes = []
    for v in runner.volumes:
        kinds = ["workspaces" if p.rstrip("/") == root else "uploads" if p.rstrip("/") == cfg.runner_uploads_dir.rstrip("/") else "home" for p in v.paths]
        runner_volumes += [Volume(kind, v.fsid, v.total, v.used, v.free) for kind in kinds]
    for v in [*own.volumes, *runner_volumes]:
        kinds, _ = volumes.setdefault(v.fsid, ([], v))
        if v.kind not in kinds:
            kinds.append(v.kind)

    workspaces = [f for f in runner.folders if f.kind == "workspace"]
    used: set[str] = set()  # folders counted for an agent or a project
    out_projects = []
    agent_cpu: list[tuple[AgentUsageOut, str]] = []
    project_cpu: list[tuple[ProjectUsageOut, Project, str, set[str]]] = []
    for project in projects:
        own_folder = f"{root}/{project.workspace_path.strip('/')}" if project.workspace_path else None
        rows = []
        for agent in (a for a in agents if a.project_id == project.id):
            row = AgentUsageOut(id=agent.id, name=agent.name)
            if own_folder is None:
                folder = f"{root}/{project.slug}/{agent.slug}"
                found = [f for f in workspaces if f.path == folder]
                row.disk = found[0].bytes if found else None
                used.update(f.path for f in found)
                agent_cpu.append((row, folder))
                for m in runner.memory:
                    if m.folder == folder:
                        row.memory, row.processes = m.bytes, m.processes
            rows.append(row)
        mine = own_folder or f"{root}/{project.slug}"
        other = [f for f in workspaces if _within(f.path, mine) and f.path not in used]
        trains = [f for f in runner.folders if f.kind == "train" and _train_project(f.path, root) == project.id]
        used.update(f.path for f in other)
        agent_folders = {f"{root}/{project.slug}/{a.slug}" for a in agents if a.project_id == project.id} if own_folder is None else set()
        other_memory = sum(m.bytes for m in runner.memory if m.folder and m.folder not in agent_folders and (_within(m.folder, mine) or _train_project(m.folder, root) == project.id))
        out = (
            ProjectUsageOut(
                id=project.id,
                name=project.name,
                agents=rows,
                uploads=uploads.get(project.id, 0),
                other_disk=sum(f.bytes for f in other) + sum(f.bytes for f in trains),
                other_memory=other_memory,
            )
        )
        out_projects.append(out)
        project_cpu.append((out, project, mine, agent_folders))

    cpu = _Cpu(runner)
    for row, folder in agent_cpu:
        row.cpu_history, row.cpu_avg = cpu.series(lambda f, folder=folder: f == folder)
        row.cpu = cpu.cores(lambda f, folder=folder: f == folder)
    for out, project, mine, agent_folders in project_cpu:

        def other(folder: str, project=project, mine=mine, agent_folders=agent_folders) -> bool:
            return bool(folder) and folder not in agent_folders and (_within(folder, mine) or _train_project(folder, root) == project.id)

        out.other_cpu_history, out.other_cpu_avg = cpu.series(other)
        out.other_cpu = cpu.cores(other)

    def belongs(folder: str) -> bool:
        return bool(folder) and any(_within(folder, mine) or _train_project(folder, root) == project.id for _, project, mine, _ in project_cpu)

    cpu_other_history, cpu_other_avg = cpu.series(lambda f: not belongs(f))
    load_history = [round(b.load, 2) if b else 0.0 for b in cpu.buckets]

    attributed = sum(a.memory for p in out_projects for a in p.agents) + sum(p.other_memory for p in out_projects)
    shared = sorted((FolderOut(name=Path(f.path).name, bytes=f.bytes) for f in runner.folders if f.kind == "shared"), key=lambda f: -f.bytes)
    return UsageOut(
        runner_ok=machine is not None,
        measured_at=runner.measured_at and _naive_utc(runner.measured_at),
        measuring=runner.measuring,
        volumes=[VolumeOut(kinds=kinds, total=v.total, used=v.used, free=v.free, low=low(v.total, v.free)) for kinds, v in volumes.values()],
        projects=out_projects,
        shared=shared,
        other_disk=sum(f.bytes for f in workspaces if f.path not in used),
        data=own.data,
        memory_total=runner.memory_total,
        memory_used=runner.memory_used,
        memory_other=max(0, sum(m.bytes for m in runner.memory) - attributed),
        cpu_cores=runner.cpu_cores,
        cpu_step=cpu.step,
        cpu_start=None if cpu.first is None else _naive_utc(cpu.first * cpu.step),
        cpu_load=runner.load_average,
        cpu_load_history=load_history,
        cpu_other=cpu.cores(lambda f: not belongs(f)),
        cpu_other_avg=cpu_other_avg,
        cpu_other_history=cpu_other_history,
    )


def _naive_utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC).replace(tzinfo=None)


_LAST: dict[int, tuple[MachineUsage, datetime]] = {}  # what each external runner measured last, for while it is away


async def _ask(client, measure: bool) -> MachineUsage | None:
    try:
        return await asyncio.wait_for(client.usage(measure), 30)
    except Exception:
        return None


async def overview(session: AsyncSession, runs, measure: bool = False) -> UsageOut:
    """``runs``: the app's ``RunManager``; every runner a project uses is asked, at once."""
    hub = runs.runners
    projects = list((await session.scalars(select(Project).order_by(Project.name))).all())
    agents = list((await session.scalars(select(Agent).order_by(Agent.is_manager.desc(), Agent.name))).all())
    settings = await settings_service.get_all(session)
    uploads = {pid: int(size or 0) for pid, size in (await session.execute(select(Attachment.project_id, func.sum(Attachment.size)).group_by(Attachment.project_id))).all()}
    names = dict((await session.execute(select(Runner.id, Runner.name))).all())
    seen = dict((await session.execute(select(Runner.id, Runner.last_seen_at))).all())
    await session.commit()  # no transaction stays open while the runners are asked
    external = [rid for rid in hub.in_use() if rid is not None]
    asked = [rid for rid in external if hub.connected(rid)]
    answers = await asyncio.gather(_ask(hub.builtin, measure), *(_ask(hub.client(rid), measure) for rid in asked))
    machine, found = answers[0], dict(zip(asked, answers[1:]))
    own = await asyncio.to_thread(own_machine)

    def on(runner_id: int | None) -> tuple[list[Project], list[Agent]]:
        here = [p for p in projects if p.runner_id == runner_id]
        ids = {p.id for p in here}
        return here, [a for a in agents if a.project_id in ids]

    out = summarize(*on(None), uploads, machine, own)
    now = datetime.now(UTC).replace(tzinfo=None)
    for rid in external:
        here, here_agents = on(rid)
        if found.get(rid) is not None:
            _LAST[rid] = (found[rid], now)
        last = _LAST.get(rid)
        summary = summarize(here, here_agents, uploads, found.get(rid) or (last[0] if last else None), Machine())
        connected = rid in asked
        out.runners.append(
            RunnerUsageOut(
                **summary.model_dump(include=set(MachineOut.model_fields)),
                id=rid,
                name=names.get(rid, ""),
                connected=connected,
                last_seen_at=now if connected else seen.get(rid),
                known_at=now if found.get(rid) is not None else (last[1] if last else None),
            )
        )
        out.runners[-1].runner_ok = found.get(rid) is not None
    out.runners.sort(key=lambda r: r.name.lower())
    from . import storage

    volume = cleanup_service.workspace_volume(machine, get_config().workspaces_root) if machine else None
    out.storage = storage.overview(settings, volume)
    out.cleanups = [c for c in map(cleanup_service.last, settings["storage_cleanups"] or []) if c]
    out.servers_stopped = cleanup_service.last(settings["cleanup_servers_last"])
    return out
