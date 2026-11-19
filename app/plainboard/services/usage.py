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
from ..models import Agent, Attachment, Project
from ..orchestration.runner_client import RunnerClient, RunnerError
from . import cleanup as cleanup_service
from . import settings as settings_service
from ..schemas import AgentUsageOut, FolderOut, ProjectUsageOut, UsageOut, VolumeOut

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
        out_projects.append(
            ProjectUsageOut(
                id=project.id,
                name=project.name,
                agents=rows,
                uploads=uploads.get(project.id, 0),
                other_disk=sum(f.bytes for f in other) + sum(f.bytes for f in trains),
                other_memory=other_memory,
            )
        )

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
    )


def _naive_utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC).replace(tzinfo=None)


async def overview(session: AsyncSession, runner: RunnerClient, measure: bool = False) -> UsageOut:
    projects = list((await session.scalars(select(Project).order_by(Project.name))).all())
    agents = list((await session.scalars(select(Agent).order_by(Agent.is_manager.desc(), Agent.name))).all())
    settings = await settings_service.get_all(session)
    uploads = {pid: int(size or 0) for pid, size in (await session.execute(select(Attachment.project_id, func.sum(Attachment.size)).group_by(Attachment.project_id))).all()}
    await session.commit()  # no transaction stays open while the runner is asked
    try:
        machine: MachineUsage | None = await runner.usage(measure)
    except RunnerError:
        machine = None
    own = await asyncio.to_thread(own_machine)
    out = summarize(projects, agents, uploads, machine, own)
    out.cleanup, out.servers_stopped = cleanup_service.last(settings["cleanup_last"]), cleanup_service.last(settings["cleanup_servers_last"])
    return out
