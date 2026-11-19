"""When disk or memory run short: what may be cleaned, and what the last cleanup did.

The watcher (``orchestration/cleanup.py``) looks rarely and cheaply (statvfs, through the runner's
``/usage``). Below ``cleanup_soft_free_percent`` free on the workspaces' volume the runner removes what
costs nothing to rebuild, below ``cleanup_hard_free_percent`` also installed dependencies, until
``SOFT_MARGIN`` points above the soft threshold are free (runner ``cleanup.py`` lists what goes).
Here is decided which agent folders it may touch at all: those of agents with no turn, no open
assignment and no running job, of projects without a folder of their own, plus folders no agent has any
more. Memory: below ``cleanup_memory_free_percent`` available, servers nobody opened for
``SERVER_IDLE`` are stopped. The manager hears only what needs a person: still short after a hard cleanup.
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
from .util import format_bytes

SOFT_MARGIN = 5  # percentage points above the soft threshold a cleanup aims for, so it does not run again at once
AGAIN_AFTER = timedelta(hours=6)  # at the same level, unless the last one freed what was needed
TELL_EVERY = timedelta(hours=24)
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


def level(total: int, free: int, settings: dict[str, Any]) -> str | None:
    if total <= 0:
        return None
    share = free * 100 / total
    if share < percent(settings, "cleanup_hard_free_percent"):
        return "hard"
    if share < percent(settings, "cleanup_soft_free_percent"):
        return "soft"
    return None


def need(total: int, free: int, settings: dict[str, Any]) -> int:
    """Bytes to free to be ``SOFT_MARGIN`` points above the soft threshold."""
    goal = total * min(percent(settings, "cleanup_soft_free_percent") + SOFT_MARGIN, 95) / 100
    return max(0, int(goal - free))


def memory_short(machine: MachineUsage, settings: dict[str, Any]) -> bool:
    total = machine.memory_total
    return total > 0 and (total - machine.memory_used) * 100 / total < percent(settings, "cleanup_memory_free_percent")


def due(last: dict[str, Any] | None, lvl: str, now: datetime) -> bool:
    """Not every hour again for nothing: the same level waits ``AGAIN_AFTER`` after a cleanup that did not free what was needed."""
    if not last:
        return True
    at = datetime.fromisoformat(last["at"])
    worse = lvl == "hard" and last.get("level") == "soft"
    return worse or now - at >= AGAIN_AFTER or last.get("freed", 0) >= last.get("need", 0)


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
    """The agent folders the runner may clean. Projects with a folder of their own (the user's) never;
    a folder below a project's that no agent has any more is an orphan (deleted, or renamed)."""
    root = root.rstrip("/")
    out: list[CleanupFolder] = []
    by_slug = {p.slug: p for p in projects if not p.workspace_path}
    mine: dict[str, Agent] = {}
    for agent in agents:
        project = next((p for p in by_slug.values() if p.id == agent.project_id), None)
        if project is not None:
            mine[f"{root}/{project.slug}/{agent.slug}"] = agent
    for path, agent in mine.items():
        if agent.id not in busy:
            since = turns.get(agent.id) or agent.created_at
            out.append(CleanupFolder(path=path, idle_since=_unix(since) if since else 0))
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


def record(lvl: str, now: datetime, total: int, free: int, wanted: int, result: CleanupResult, told: bool) -> dict[str, Any]:
    """What the setting ``cleanup_last`` keeps for the storage view."""
    items = sorted(result.items, key=lambda i: -i.bytes)[:ITEMS_KEPT]
    return {
        "at": now.isoformat(),
        "level": lvl,
        "freed": result.freed,
        "need": wanted,
        "free_before": free,
        "total": total,
        "items": [i.model_dump() for i in items],
        "kept": result.skipped[:ITEMS_KEPT],
        "told": told,
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
        )
    except (KeyError, ValueError, TypeError):
        return None


def text(total: int, free: int, result: CleanupResult, biggest: list[tuple[str, int]]) -> str:
    """For the manager: the volume is still short after a hard cleanup. A decision for the user."""
    lines = [
        f"Disk space is still short on the agents' volume after PlainBoard's automatic cleanup: about {format_bytes(free + result.freed)} "
        f"free of {format_bytes(total)}. It removed {format_bytes(result.freed)} that can be rebuilt (build outputs and dependencies of idle agents, "
        "old cache files, old job logs, folders of removed agents without unpushed work). What else goes is the user's decision: "
        "tell them (e.g. remove projects or agents that are no longer needed, clean a folder by hand, or a bigger volume). Don't delete anything yourself."
    ]
    if biggest:
        lines.append("Biggest folders: " + ", ".join(f"{path} {format_bytes(size)}" for path, size in biggest))
    if result.skipped:
        lines.append("Left alone: " + "; ".join(result.skipped[:6]))
    return "\n".join(lines)
