"""What the runner may clean, and the records of what was cleaned.

Disk space is under the manager's control (services/storage.py): here is decided which agent folders
are busy (their agent has a turn, an open assignment or a running job: listed, deleted only when the
manager insists) and which belong to no agent any more. Only in an emergency the watcher has the runner
clean up by itself, and then only folders of idle agents (``CleanupSpec`` level soft). Memory: below
``cleanup_memory_free_percent`` available, servers nobody opened for ``SERVER_IDLE`` are stopped.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from plainboard_protocol import CleanupFolder, CleanupResult, FolderUsage, MachineUsage, VolumeUsage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ACTIVE_RUN_STATUSES, Agent, Assignment, AssignmentStatus, Job, JobStatus, Project, Run
from ..schemas import CleanedOut, CleanupOut

SERVER_IDLE = timedelta(hours=12)
ITEMS_KEPT = 12


def percent(settings: dict[str, Any], key: str) -> float:
    try:
        return min(max(float(settings[key]), 0.0), 90.0)
    except (TypeError, ValueError, KeyError):
        return 0.0


def workspace_volume(machine: MachineUsage, root: str) -> VolumeUsage | None:
    root = root.rstrip("/")
    return next((v for v in machine.volumes if any(p.rstrip("/") == root for p in v.paths)), None)


def need(total: int, free: int, percent_used: float) -> int:
    """Bytes to free until no more than ``percent_used`` is used."""
    return max(0, int((total - free) - total * percent_used / 100))


def memory_short(machine: MachineUsage, settings: dict[str, Any]) -> bool:
    total = machine.memory_total
    return total > 0 and (total - machine.memory_used) * 100 / total < percent(settings, "cleanup_memory_free_percent")


async def busy_agents(session: AsyncSession, jobs: bool = True) -> set[int]:
    """Agents whose folder must not be touched: a turn queued or running, an open assignment, a running job."""
    found: set[int] = set()
    found |= set(await session.scalars(select(Run.agent_id).where(Run.status.in_(ACTIVE_RUN_STATUSES))))
    found |= set(await session.scalars(select(Assignment.agent_id).where(Assignment.status.in_((AssignmentStatus.QUEUED, AssignmentStatus.RUNNING)))))
    if jobs:
        found |= set(await session.scalars(select(Job.agent_id).where(Job.status == JobStatus.RUNNING)))
    return found


async def last_turns(session: AsyncSession) -> dict[int, datetime]:
    rows = await session.execute(select(Run.agent_id, func.max(Run.finished_at)).group_by(Run.agent_id))
    return {agent_id: at for agent_id, at in rows.all() if at is not None}


def folders(projects: list[Project], agents: list[Agent], busy: set[int], turns: dict[int, datetime], measured: list[FolderUsage], root: str) -> list[CleanupFolder]:
    """The agent folders the runner may look at, busy ones marked. Projects with a folder of their own
    (the user's) never; a folder below a project's that no agent has any more is an orphan (deleted, or renamed)."""
    root = root.rstrip("/")
    out: list[CleanupFolder] = []
    by_slug = {p.slug: p for p in projects if not p.workspace_path}
    mine: dict[str, Agent] = {}
    for agent in agents:
        project = next((p for p in by_slug.values() if p.id == agent.project_id), None)
        if project is not None:
            mine[f"{root}/{project.slug}/{agent.slug}"] = agent
    for path, agent in mine.items():
        since = turns.get(agent.id) or agent.created_at
        out.append(CleanupFolder(path=path, idle_since=_unix(since) if since else 0, busy=agent.id in busy))
    for f in measured:
        if f.kind != "workspace" or f.path in mine:
            continue
        parts = Path(f.path).relative_to(root).parts if Path(f.path).is_relative_to(root) else ()
        if len(parts) == 2 and parts[0] in by_slug:
            out.append(CleanupFolder(path=f.path, orphan=True))
    return out


def _unix(at: datetime) -> float:
    return (at - datetime(1970, 1, 1)).total_seconds()


def project_of(path: str, root: str, projects: list[Project]) -> Project | None:
    root = root.rstrip("/")
    if not path.startswith(root + "/"):
        return None
    slug = path[len(root) + 1 :].split("/", 1)[0]
    return next((p for p in projects if p.slug == slug and not p.workspace_path), None)


def record(lvl: str, now: datetime, total: int, free: int, result: CleanupResult, by: str = "", project: str = "") -> dict[str, Any]:
    """What the storage view keeps of a cleanup (``storage_cleanups``)."""
    items = sorted(result.items, key=lambda i: -i.bytes)[:ITEMS_KEPT]
    return {
        "at": now.isoformat(),
        "level": lvl,
        "by": by,
        "project": project,
        "freed": result.freed,
        "free_before": free,
        "total": total,
        "items": [i.model_dump() for i in items],
        "kept": result.skipped[:ITEMS_KEPT],
        "told": True,
    }


def stopped_record(now: datetime, jobs: list[Job]) -> dict[str, Any]:
    return {
        "at": now.isoformat(),
        "level": "memory",
        "freed": 0,
        "items": [{"path": job.command[:200], "what": "server", "bytes": 0} for job in jobs[:ITEMS_KEPT]],
        "kept": [],
        "told": False,
    }


def last(value: Any) -> CleanupOut | None:
    if not isinstance(value, dict):
        return None
    try:
        return CleanupOut(
            at=datetime.fromisoformat(value["at"]),
            level=value["level"],
            freed=value.get("freed", 0),
            free_before=value.get("free_before", 0),
            total=value.get("total", 0),
            items=[CleanedOut.model_validate(i) for i in value.get("items", [])],
            kept=value.get("kept", []),
            told=value.get("told", False),
            by=value.get("by", ""),
            project=value.get("project", ""),
        )
    except (KeyError, ValueError, TypeError):
        return None
