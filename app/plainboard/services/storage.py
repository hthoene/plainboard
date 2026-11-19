"""Disk space under the manager's control: trend and forecast, when the manager hears it, what may go.

The watcher (``orchestration/cleanup.py``) reads how full the agents' volume is (statvfs through the
runner, cheap): every few minutes when it is high or grows fast, else hourly. From the recent samples,
kept in memory, it forecasts when the target (``storage_notify_percent``) is crossed. The manager of
the project that causes it hears it at the target, or earlier when the forecast crosses it within
``SOON_HOURS``; again only when it gets clearly worse, or as a reminder while above. Nothing is deleted
without him (``tools/storage.py``: candidates with sizes and safety, delete chosen ones), except near
full (``storage_emergency_percent``): then what can be made again goes by itself, and he hears exactly what.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from plainboard_protocol import CandidatesSpec, CleanupCandidate, CleanupResult, DeleteSpec, MachineUsage, VolumeUsage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..models import Agent, Project, Repository, utcnow
from ..schemas import StorageNoticeOut, StorageOut
from . import cleanup as cleanup_service
from . import flow_log
from . import settings as settings_service
from .timing import duration
from .util import format_bytes

WINDOW = timedelta(hours=3)  # samples the growth rate is taken from
MIN_SPAN = timedelta(minutes=10)
FAST_SECONDS = 300  # how often it looks when high or growing fast
WATCH_BELOW = 10.0  # points below the target from which it looks that often ...
WATCH_HOURS = 6.0  # ... or when the target is forecast within this
SOON_HOURS = 2.0  # the manager hears it this long before the target is crossed
WORSE_POINTS = 5.0  # clearly worse: again at once
REMIND_GROWING = timedelta(hours=1)  # above the target and still growing: a reminder
REMIND_STILL = timedelta(hours=6)  # above the target, not growing
CLOSE_BELOW = 3.0  # points below the target at which it is over, and the next time is a new notice
EMERGENCY_AGAIN = timedelta(hours=1)
KEPT = 10
SMALL = 100 * 2**20  # candidates below this are left out of lists
SHOWN = 8
OTHERS_FROM = 5 * 2**30  # another project's manager hears it if that much of theirs could go


@dataclass
class Forecast:
    total: int
    used: int  # what is not free for the agents
    rate: float | None = None  # bytes per hour; None: not enough samples yet

    @property
    def percent(self) -> float:
        return self.used * 100 / self.total if self.total else 0.0

    def hours_to(self, percent: float) -> float | None:
        """Until that share is used: 0 if it is already, None if it does not grow."""
        target = self.total * percent / 100
        if self.used >= target:
            return 0.0
        if not self.rate or self.rate <= 0:
            return None
        return (target - self.used) / self.rate


@dataclass
class Trend:
    """Recent samples of the volume, in memory: nothing is written for them."""

    samples: list[tuple[datetime, int, int]] = field(default_factory=list)  # at, used, total

    def add(self, at: datetime, total: int, free: int) -> Forecast:
        used = total - free
        if self.samples and (self.samples[-1][2] != total or used < self.samples[-1][1] - total // 100):
            self.samples = []  # cleaned up (or another volume): a new series
        self.samples = [s for s in self.samples if at - s[0] <= WINDOW] + [(at, used, total)]
        return self.forecast()  # type: ignore[return-value]

    def forecast(self) -> Forecast | None:
        if not self.samples:
            return None
        _, used, total = self.samples[-1]
        return Forecast(total, used, self._rate())

    def _rate(self) -> float | None:
        if len(self.samples) < 2 or self.samples[-1][0] - self.samples[0][0] < MIN_SPAN:
            return None
        t0 = self.samples[0][0]
        xs = [(at - t0).total_seconds() / 3600 for at, _, _ in self.samples]
        ys = [used for _, used, _ in self.samples]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sum((x - mx) ** 2 for x in xs)


TREND = Trend()


def limit(settings: dict[str, Any], key: str) -> float:
    try:
        return min(max(float(settings[key]), 1.0), 100.0)
    except (TypeError, ValueError, KeyError):
        return float(settings_service.SETTINGS[key].default)


def next_check(f: Forecast | None, settings: dict[str, Any], slow: float) -> float:
    """Seconds to the next look: often when high or when the target comes near, else ``slow``."""
    if f is None:
        return slow
    target = limit(settings, "storage_notify_percent")
    soon = f.hours_to(target)
    if f.percent >= target - WATCH_BELOW or (soon is not None and soon <= WATCH_HOURS):
        return min(FAST_SECONDS, slow)
    return slow


def notice_due(f: Forecast, settings: dict[str, Any], last: dict[str, Any] | None, now: datetime) -> str | None:
    """"over" (at the target or above) or "soon" (the forecast crosses it within SOON_HOURS) when the
    manager is to hear it now; None if not, or if he heard it already and it is not clearly worse."""
    target = limit(settings, "storage_notify_percent")
    soon = f.hours_to(target)
    if f.percent >= target:
        reason = "over"
    elif soon is not None and soon <= SOON_HOURS:
        reason = "soon"
    else:
        return None
    if not last:
        return reason
    age = now - datetime.fromisoformat(last["at"])
    if last.get("closed"):
        return reason if age >= REMIND_GROWING else None
    if reason == "over" and last.get("reason") == "soon":
        return reason
    if f.percent >= last["percent"] + WORSE_POINTS:
        return reason
    full = f.hours_to(100)
    if full is not None and full <= SOON_HOURS and not (last.get("hours_full") is not None and last["hours_full"] <= SOON_HOURS):
        return reason
    if reason == "over" and (age >= REMIND_STILL or (age >= REMIND_GROWING and f.percent > last["percent"])):
        return reason
    return None


def closes(f: Forecast, settings: dict[str, Any], last: dict[str, Any] | None) -> bool:
    """The last notice is over: well below the target again, and no crossing soon."""
    if not last or last.get("closed"):
        return False
    target = limit(settings, "storage_notify_percent")
    soon = f.hours_to(target)
    return f.percent < target - CLOSE_BELOW and (soon is None or soon > SOON_HOURS)


def _hours(h: float | None) -> str:
    return duration(h * 3600) if h is not None else ""


def forecast_text(f: Forecast, settings: dict[str, Any]) -> str:
    target = limit(settings, "storage_notify_percent")
    out = f"{f.percent:.0f} % used of {format_bytes(f.total)} on the agents' volume (target: at most {target:g} %)"
    if f.rate is None:
        return out + ", growth not known yet"
    if f.rate < f.total / 1000:  # under 0.1 % an hour
        return out + ", not growing"
    etas = [f"{label} in about {_hours(h)}" for p, label in ((target, f"{target:g} %"), (95, "95 %"), (100, "full")) if (h := f.hours_to(p))]
    return out + f", growing {format_bytes(int(f.rate))}/h" + (": " + ", ".join(etas) if etas else "")


def when_text(reason: str, f: Forecast, settings: dict[str, Any]) -> str:
    full = f.hours_to(100)
    if reason == "soon":
        return f"Clean up within {_hours(f.hours_to(limit(settings, 'storage_notify_percent')))}, before it crosses the target."
    if full is not None and full <= SOON_HOURS:
        return f"Clean up now: it is full in about {_hours(full)}, and then every member's work fails."
    return "Clean up now."


# --- candidates --------------------------------------------------------------------------------


@dataclass
class Item:
    candidate: CleanupCandidate
    project_id: int | None  # None: in the shared home and nobody's to tell
    agent: Agent | None = None  # whose folder it is in


@dataclass
class Listing:
    items: list[Item]
    projects: list[Project]
    agents: list[Agent]
    busy: set[int]
    machine: MachineUsage
    folders: list  # the CleanupFolders sent to the runner

    def visible(self, project_id: int) -> list[Item]:
        """What one project's manager may see and delete: its own, and the shared home's that is nobody's."""
        return [i for i in self.items if i.project_id in (project_id, None)]

    def attribution(self) -> dict[str, int]:
        """Folder (as the runner measures it) → project."""
        root = get_config().workspaces_root.rstrip("/")
        out = {f.path: p.id for f in self.machine.folders if (p := cleanup_service.project_of(f.path, root, self.projects))}
        from .usage import _train_project

        for f in self.machine.folders:
            if f.kind == "train" and (pid := _train_project(f.path, root)) is not None:
                out[f.path] = pid
        out.update({i.candidate.path: i.project_id for i in self.items if i.candidate.what == "home" and i.project_id is not None})
        return out


def _repo_key(url: str) -> str:
    """The same repository whatever the address: host/owner/name."""
    url = re.sub(r"^[a-z+]+://", "", url.strip().lower())
    url = re.sub(r"^[^@/]+@", "", url).replace(":", "/", 1)
    return url.removesuffix("/").removesuffix(".git")


def _owner(c: CleanupCandidate, projects: list[Project], repos: dict[str, int]) -> int | None:
    found = {repos[k] for r in c.remotes if (k := _repo_key(r)) in repos}
    if len(found) == 1:
        return found.pop()
    name = Path(c.path).name.lower()
    by_name = [p.id for p in projects if len(p.slug) >= 3 and p.slug.lower() in name]
    return by_name[0] if len(by_name) == 1 else None


async def listing(session: AsyncSession, runner, machine: MachineUsage | None = None) -> Listing:
    """Every candidate, with its project and agent. The runner measures sizes (kept for some minutes)."""
    root = get_config().workspaces_root.rstrip("/")
    projects = list(await session.scalars(select(Project).order_by(Project.id)))
    agents = list(await session.scalars(select(Agent)))
    repos = {_repo_key(r.url): r.project_id for r in await session.scalars(select(Repository))}
    busy = await cleanup_service.busy_agents(session)
    turns = await cleanup_service.last_turns(session)
    await session.commit()  # no transaction stays open while the runner works
    machine = machine or await runner.usage()
    folders = cleanup_service.folders(projects, agents, busy, turns, machine.folders, root)
    found = await runner.cleanup_candidates(CandidatesSpec(folders=folders))
    by_folder = {f"{root}/{p.slug}/{a.slug}": a for a in agents for p in projects if p.id == a.project_id and not p.workspace_path}
    items = []
    for c in found:
        if c.what in ("build", "folder"):
            project = cleanup_service.project_of(c.path, root, projects)
            agent = next((a for path, a in by_folder.items() if c.path == path or c.path.startswith(path + "/")), None)
            items.append(Item(c, project.id if project else None, agent))
        else:
            items.append(Item(c, _owner(c, projects, repos)))
    items.sort(key=lambda i: -i.candidate.bytes)
    return Listing(items, projects, agents, busy, machine, folders)


def item_line(item: Item, project_id: int, busy: set[int]) -> str:
    c = item.candidate
    what = {"build": "build output", "folder": "folder of a removed agent", "cache": "cache", "home": "folder in the shared home"}[c.what]
    if item.agent is not None and c.what == "build":
        what += f" of {item.agent.name}"
    if c.what == "home":
        what += ", yours (by its clones or name)" if item.project_id == project_id else ", owner unknown"
    safety = c.safety
    if safety == "busy" and item.agent is not None and item.agent.id in busy:
        safety = f"busy: {item.agent.name} works now"
    elif c.why:
        safety += f": {c.why}"
    return f"- {format_bytes(c.bytes)} {c.path}: {what}; {safety}"


def busy_names(listing: Listing, project_id: int) -> list[str]:
    return [a.name for a in listing.agents if a.project_id == project_id and a.id in listing.busy]


def tool_listing(listing: Listing, project: Project, f: Forecast | None, settings: dict[str, Any]) -> str:
    """For list_cleanup: the forecast, then what may go by safety, the biggest first."""
    lines = [forecast_text(f, settings) + "." if f else "Disk use not known yet."]
    if names := busy_names(listing, project.id):
        lines.append("Busy now: " + ", ".join(names) + ".")
    groups = {
        "safe": "Safe (made again when needed):",
        "busy": "Busy (a member works there: deleting with force=true makes its next build slower; or ask the member to clean up):",
        "unsure": "Unsure (may hold someone's work: ask who made it, or delete with force=true):",
        "keep": "Kept (unpushed work or tools; delete_cleanup won't):",
    }
    items = listing.visible(project.id)
    for safety, title in groups.items():
        found = [i for i in items if i.candidate.safety == safety and i.candidate.bytes >= SMALL]
        if found:
            lines.append(title)
            lines += [item_line(i, project.id, listing.busy) for i in found[:25]]
            if len(found) > 25:
                lines.append(f"- and {len(found) - 25} more, {format_bytes(sum(i.candidate.bytes for i in found[25:]))} together")
    small = [i for i in items if i.candidate.bytes < SMALL]
    if small:
        lines.append(f"{len(small)} smaller ones, {format_bytes(sum(i.candidate.bytes for i in small))} together, not shown.")
    if len(lines) <= 2:
        lines.append("Nothing to delete.")
    return "\n".join(lines)


def project_sizes(listing: Listing, measures: list[tuple[float, dict[str, int]]]) -> tuple[dict[int, int], dict[int, int], float | None]:
    """Per project: bytes in the last measurement, growth since the one before, and its age in hours."""
    owner = listing.attribution()
    size: dict[int, int] = defaultdict(int)
    growth: dict[int, int] = defaultdict(int)
    if not measures:
        return size, growth, None
    _, now = measures[-1]
    for path, n in now.items():
        if path in owner:
            size[owner[path]] += n
    hours = None
    if len(measures) > 1:
        at, before = measures[-2]
        hours = (measures[-1][0] - at) / 3600
        for path, n in now.items():
            if path in owner:
                growth[owner[path]] += n - before.get(path, 0)
    return size, growth, hours


def cause(listing: Listing, measures: list[tuple[float, dict[str, int]]]) -> tuple[Project | None, str]:
    """The project that causes it: the biggest grower since the last measurement, else the biggest user."""
    size, growth, hours = project_sizes(listing, measures)
    own = [p for p in listing.projects if not p.workspace_path] or listing.projects
    grower = max(own, key=lambda p: (growth.get(p.id, 0), -p.id), default=None)
    if grower is not None and growth.get(grower.id, 0) >= 2**30 and hours is not None:
        return grower, f"{grower.name} grows most: +{format_bytes(growth[grower.id])} in the {_hours(hours)} between the last two measurements."
    user = max(own, key=lambda p: (size.get(p.id, 0) + sum(i.candidate.bytes for i in listing.items if i.project_id == p.id and i.candidate.what == "home"), -p.id), default=None)
    if user is None:
        return None, ""
    return user, f"{user.name} uses the most room: {format_bytes(size.get(user.id, 0))} in its folders."


def notice_text(reason: str, f: Forecast, settings: dict[str, Any], listing: Listing, project: Project, why: str) -> str:
    """For the manager of the project that causes it: what, until when, the biggest items, how to go on."""
    lines = [f"Disk space: {forecast_text(f, settings)}. {when_text(reason, f, settings)}"]
    if why:
        lines.append(why)
    root = get_config().workspaces_root.rstrip("/")
    sizes = {x.path: x.bytes for x in listing.machine.folders}
    workspaces = sorted(((a.name, sizes.get(f"{root}/{project.slug}/{a.slug}", 0)) for a in listing.agents if a.project_id == project.id), key=lambda w: -w[1])
    if any(n for _, n in workspaces):
        lines.append("Workspaces: " + ", ".join(f"{name} {format_bytes(n)}" for name, n in workspaces[:SHOWN] if n) + ".")
    items = [i for i in listing.visible(project.id) if i.candidate.safety != "keep" and i.candidate.bytes >= SMALL]
    if items:
        lines.append("Biggest items that can go (list_cleanup shows all, delete_cleanup deletes):")
        lines += [item_line(i, project.id, listing.busy) for i in items[:SHOWN]]
    if names := busy_names(listing, project.id):
        lines.append("Busy now: " + ", ".join(names) + ".")
    lines.append(
        "Delete what is safe with delete_cleanup. Busy members' build outputs: delete anyway with force=true (their next build is slower) "
        "or tell the member to clean up. Folders in the shared home of unknown owner: tell your members to delete their own scratch and "
        "build folders there, or delete with force=true. If that is not enough, tell the user."
    )
    return "\n".join(lines)


def others_text(f: Forecast, settings: dict[str, Any], main: Project, freeable: int) -> str:
    return (
        f"Disk space: {forecast_text(f, settings)}; most of it comes from {main.name}. Your project holds {format_bytes(freeable)} that can go: "
        "list_cleanup shows it, delete_cleanup deletes what is safe."
    )


def freeable(listing: Listing, project_id: int) -> int:
    return sum(i.candidate.bytes for i in listing.items if i.project_id == project_id and i.candidate.safety in ("safe", "busy"))


# --- what was told and cleaned, for the storage view ----------------------------------------------


def notice_record(reason: str, f: Forecast, now: datetime, projects: list[str], settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": now.isoformat(),
        "reason": reason,
        "percent": round(f.percent, 1),
        "rate": f.rate,
        "hours_target": f.hours_to(limit(settings, "storage_notify_percent")),
        "hours_full": f.hours_to(100),
        "projects": projects,
    }


async def remember(session: AsyncSession, key: str, record: dict[str, Any]) -> None:
    """Newest first, the last ``KEPT``: a write only when something was told or deleted."""
    kept = (await settings_service.get(session, key)) or []
    await settings_service.update(session, {key: [record, *kept][:KEPT]}, managed=True)


async def close_last(session: AsyncSession) -> None:
    kept = (await settings_service.get(session, "storage_notices")) or []
    if kept:
        kept[0] = {**kept[0], "closed": True}
        await settings_service.update(session, {"storage_notices": kept}, managed=True)


def overview(settings: dict[str, Any], volume: VolumeUsage | None) -> StorageOut:
    f = TREND.forecast()
    if f is None and volume is not None and volume.total:
        f = Forecast(volume.total, volume.total - volume.free)
    notices = []
    for n in settings.get("storage_notices") or []:
        try:
            notices.append(StorageNoticeOut.model_validate({**n, "at": datetime.fromisoformat(n["at"])}))
        except (KeyError, ValueError, TypeError):
            continue
    target = limit(settings, "storage_notify_percent")
    return StorageOut(
        notify_percent=target,
        emergency_percent=limit(settings, "storage_emergency_percent"),
        percent=f.percent if f else None,
        rate=f.rate if f else None,
        hours_target=f.hours_to(target) if f else None,
        hours_95=f.hours_to(95) if f else None,
        hours_full=f.hours_to(100) if f else None,
        notices=notices,
    )


async def delete(session: AsyncSession, runner, project: Project, agent: Agent, paths: list[str], force: bool) -> tuple[CleanupResult, list[str]]:
    """Delete what one project's manager chose, through the runner; logged as a flow event.
    The second value: paths that are no candidate of this project."""
    found = await listing(session, runner)
    allowed = {i.candidate.path for i in found.visible(project.id)}
    wanted = [p.rstrip("/") for p in paths]
    refused = [p for p in wanted if p not in allowed]
    chosen = [p for p in wanted if p in allowed]
    result = await runner.cleanup_delete(DeleteSpec(paths=chosen, folders=found.folders, force=force)) if chosen else CleanupResult()
    volume = cleanup_service.workspace_volume(found.machine, get_config().workspaces_root)
    if result.items:
        await flow_log.record(
            session,
            project.id,
            "storage.cleaned",
            agent_id=agent.id,
            level="manager",
            force=force,
            freed=result.freed,
            items=[i.model_dump() for i in result.items[:40]],
            kept=result.skipped[:20],
        )
        await remember(session, "storage_cleanups", cleanup_service.record("manager", utcnow(), volume.total if volume else 0, volume.free if volume else 0, result, by=agent.name, project=project.name))
    return result, refused


def delete_text(result: CleanupResult, refused: list[str]) -> str:
    lines = []
    if result.items:
        lines.append(f"Deleted {format_bytes(result.freed)}: " + ", ".join(f"{i.path} {format_bytes(i.bytes)}" for i in result.items[:20]) + ".")
    if result.skipped:
        lines.append("Not deleted: " + "; ".join(result.skipped[:20]) + ".")
    if refused:
        lines.append("Not a cleanup candidate of this project (list_cleanup shows them): " + ", ".join(refused[:20]) + ".")
    return "\n".join(lines) or "Nothing deleted."


def emergency_text(result: CleanupResult, percent: float, settings: dict[str, Any], after: float) -> str:
    items = ", ".join(f"{i.path} ({i.what}) {format_bytes(i.bytes)}" for i in sorted(result.items, key=lambda i: -i.bytes)[:12])
    return (
        f"PlainBoard deleted by itself, because the disk was {percent:.0f} % used (emergency level: {limit(settings, 'storage_emergency_percent'):g} %): "
        f"{items or 'nothing could go'}; {format_bytes(result.freed)} together. Now about {after:.0f} % used. Only what is made again "
        "when needed went: old job logs, cache files unused for a month, folders of removed agents without unpushed work, build outputs of members idle for a day."
    )
