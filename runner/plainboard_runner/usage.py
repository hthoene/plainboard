"""Disk and memory use of this machine, for the app's overview (no model involved).

Folder sizes are slow to measure (hundreds of GB of clones and caches): they are measured in the
background with ``du``, when the last measurement is older than ``MEASURE_EVERY_SECONDS`` or when
asked, and kept in a file in the permanent home, so a restart does not measure again. Volumes and
memory are cheap and read on every request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from plainboard_protocol import CpuBucket, FolderUsage, MachineUsage, ProcessMemory, VolumeUsage

from . import config, disk
from .trains import trains_root

log = logging.getLogger(__name__)

MEASURE_EVERY_SECONDS = 6 * 3600
MEASURE_TIMEOUT_SECONDS = 3 * 3600  # what du has not reached by then keeps its last size
CHECK_EVERY_SECONDS = 600

_folders: list[FolderUsage] = []
_measured_at: float | None = None
_loaded = False
_task: asyncio.Task[None] | None = None


def _cache_file() -> Path:
    return Path.home() / ".plainboard" / "usage.json"


def _entries(folder: Path, dirs_only: bool = False) -> list[Path]:
    try:
        with os.scandir(folder) as it:
            return sorted(Path(e.path) for e in it if not e.is_symlink() and (e.is_dir(follow_symlinks=False) or not dirs_only))
    except OSError:
        return []


def targets() -> list[tuple[Path, str]]:
    """What is measured: each agent's folder (two levels below the workspaces root), the parts of the
    shared home, and the trains' clones, which are in the home but belong to a project."""
    root = config.WORKSPACES_ROOT
    found = [(agent, "workspace") for project in _entries(root, dirs_only=True) for agent in _entries(project, dirs_only=True)]
    home, trains = Path.home(), trains_root()
    own = home / ".plainboard"
    for child in _entries(home):
        if child != own and not root.is_relative_to(child):
            found.append((child, "shared"))
    found += [(child, "shared") for child in _entries(own) if child != trains]
    found += [(child, "train") for child in _entries(trains, dirs_only=True)]
    return found


async def du(paths: list[str], timeout: float = MEASURE_TIMEOUT_SECONDS) -> dict[str, int]:
    """Bytes on disk of each path, on its own file system, at the lowest priority (disk.py). What is done by the timeout counts."""
    found = await asyncio.to_thread(disk.sizes, paths, timeout)
    return {p: n for p, (n, _) in found.items()}


def _load() -> None:
    global _folders, _measured_at, _loaded
    _loaded = True
    try:
        data = json.loads(_cache_file().read_text())
        _folders = [FolderUsage.model_validate(f) for f in data["folders"]]
        _measured_at = float(data["measured_at"])
    except (OSError, ValueError, KeyError, TypeError):
        pass


async def measure() -> None:
    global _folders, _measured_at
    started = time.time()
    found = targets()
    sizes = await du([str(path) for path, _ in found])
    before = {f.path: f.bytes for f in _folders}
    folders = []
    for path, kind in found:
        size = sizes.get(str(path), before.get(str(path)))
        if size is not None:
            folders.append(FolderUsage(path=str(path), kind=kind, bytes=size))  # type: ignore[arg-type]
    _folders, _measured_at = folders, time.time()
    log.info("Measured %d folders in %.0fs", len(folders), _measured_at - started)
    try:
        _cache_file().parent.mkdir(parents=True, exist_ok=True)
        _cache_file().write_text(json.dumps({"measured_at": _measured_at, "folders": [f.model_dump() for f in folders]}))
    except OSError as exc:
        log.warning("Folder sizes not kept: %s", exc)


def measuring() -> bool:
    return _task is not None and not _task.done()


def measure_soon(force: bool = False) -> None:
    """Measure the folders in the background, unless it runs already or the last time is recent enough."""
    global _task
    if not _loaded:
        _load()
    if measuring() or (not force and _measured_at is not None and time.time() - _measured_at < MEASURE_EVERY_SECONDS):
        return
    _task = asyncio.create_task(measure())


async def keep_measured() -> None:
    while True:
        measure_soon()
        await asyncio.sleep(CHECK_EVERY_SECONDS)


def volumes() -> list[VolumeUsage]:
    """The file systems of the workspaces, the shared home and the uploads; paths on the same one share a row."""
    found: dict[int, VolumeUsage] = {}
    for path in (config.WORKSPACES_ROOT, Path.home(), config.UPLOADS_DIR):
        try:
            st = os.statvfs(path)
            fsid = st.f_fsid or os.stat(path).st_dev
        except OSError:
            continue
        if fsid in found:
            found[fsid].paths.append(str(path))
            continue
        size = st.f_frsize
        found[fsid] = VolumeUsage(paths=[str(path)], fsid=fsid, total=st.f_blocks * size, used=(st.f_blocks - st.f_bfree) * size, free=st.f_bavail * size)
    return list(found.values())


def _meminfo(proc: Path) -> tuple[int, int]:
    values: dict[str, int] = {}
    try:
        for line in (proc / "meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            values[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return 0, 0
    total = values.get("MemTotal", 0)
    return total, total - values.get("MemAvailable", values.get("MemFree", 0))


def _process_memory(pid_dir: Path) -> int:
    """Proportional set size (shared pages split between their users, so sums stay true); RSS where that can't be read."""
    try:
        for line in (pid_dir / "smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return int((pid_dir / "statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def folder_of(cwd: str) -> str | None:
    """The folder a working directory belongs to: an agent's (two levels below the workspaces root) or a train's clone."""
    path = Path(cwd.removesuffix(" (deleted)"))
    for root, depth in ((config.WORKSPACES_ROOT, 2), (trains_root(), 1)):
        root = root.resolve()
        if path.is_relative_to(root) and path != root:
            return str(root.joinpath(*path.relative_to(root).parts[:depth]))
    return None


def _owners(parents: dict[int, int], folders: dict[int, str | None]) -> dict[int, str | None]:
    """The folder each process counts for: its own, or that of its nearest parent that has one."""
    owners: dict[int, str | None] = {}

    def owner(pid: int) -> str | None:
        chain = []
        while pid in parents and pid not in owners and folders.get(pid) is None:
            chain.append(pid)
            pid = parents[pid]
        found = owners.get(pid, folders.get(pid))
        for p in chain:
            owners[p] = found
        return found

    for pid in parents:
        owners[pid] = owner(pid)
    return owners


def memory(proc: Path = Path("/proc")) -> tuple[int, int, list[ProcessMemory]]:
    """Total and used memory of the machine, and the memory of the processes here by folder they work in.
    A process that works elsewhere (``cd /tmp``) counts for the folder of its nearest parent that has one."""
    total, used = _meminfo(proc)
    parents: dict[int, int] = {}
    folders: dict[int, str | None] = {}
    sizes: dict[int, int] = {}
    for entry in _entries(proc, dirs_only=True):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text()
            parents[pid] = int(stat[stat.rindex(")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        try:
            folders[pid] = folder_of(os.readlink(entry / "cwd"))
        except OSError:
            folders[pid] = None
        sizes[pid] = _process_memory(entry)

    owners = _owners(parents, folders)

    by_folder: dict[str | None, ProcessMemory] = {}
    for pid, size in sizes.items():
        key = owners[pid]
        row = by_folder.setdefault(key, ProcessMemory(folder=key, bytes=0, processes=0))
        row.bytes += size
        row.processes += 1
    return total, used, sorted(by_folder.values(), key=lambda m: -m.bytes)


async def current() -> MachineUsage:
    measure_soon()
    vols, (total, used, mem) = await asyncio.gather(asyncio.to_thread(volumes), asyncio.to_thread(memory))
    cpu = cpu_usage()
    return MachineUsage(volumes=vols, folders=_folders, measured_at=_measured_at, measuring=measuring(), memory_total=total, memory_used=used, memory=mem, **cpu)


# --- CPU: sampled from /proc, kept as a rolling history -----------------------------------------

CPU_SAMPLE_EVERY = 30  # seconds
CPU_STEP = 300  # seconds per slot of the history
CPU_KEEP = 24 * 3600
CPU_SAVE_EVERY = 900  # the history is written to the home this rarely


def _cpu_file() -> Path:
    return Path.home() / ".plainboard" / "cpu.json"


def _stat_times(stat: str) -> tuple[int, int, int, int] | None:
    """(parent, own CPU ticks, CPU ticks of waited-for children, start time) from /proc/<pid>/stat."""
    try:
        f = stat[stat.rindex(")") + 2 :].split()
        return int(f[1]), int(f[11]) + int(f[12]), int(f[13]) + int(f[14]), int(f[19])
    except (ValueError, IndexError):
        return None


class CpuSampler:
    """CPU time per folder between two samples.

    Each process counts for the folder of ``_owners``. What a process used is the growth of its utime+stime.
    A process that ended between two samples is not there to read: its time (and that of its own ended
    children) is in the ``cutime``/``cstime`` of the parent that waited for it, which grew. That growth,
    less what was counted for such children while they lived, is counted for the parent's folder.
    """

    def __init__(self) -> None:
        # (pid, start time) -> (parent, own ticks, children ticks, folder); what has been counted for a process is own + children
        self.seen: dict[tuple[int, int], tuple[int, int, int, str | None]] = {}
        self.tick = float(os.sysconf("SC_CLK_TCK"))

    def sample(self, proc: Path = Path("/proc")) -> dict[str | None, float]:
        """CPU seconds per folder since the last call; empty on the first."""
        first = not self.seen
        parents: dict[int, int] = {}
        folders: dict[int, str | None] = {}
        now: dict[int, tuple[int, int, int, int]] = {}
        for entry in _entries(proc, dirs_only=True):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                times = _stat_times((entry / "stat").read_text())
            except OSError:
                continue
            if times is None:
                continue
            parents[pid] = times[0]
            now[pid] = times
            try:
                folders[pid] = folder_of(os.readlink(entry / "cwd"))
            except OSError:
                folders[pid] = None
        owners = _owners(parents, folders)

        ticks: dict[str | None, float] = {}
        alive = {(pid, t[3]) for pid, t in now.items()}
        vanished: dict[int, list[tuple[int, int]]] = {}  # parent pid -> its children that are gone
        for key, row in self.seen.items():
            if key not in alive:
                vanished.setdefault(row[0], []).append(key)

        def counted(key: tuple[int, int], depth: int = 0) -> int:
            """What was counted for a gone process and for the gone ones below it (their time is in its cutime/cstime)."""
            _, own, kids, _ = self.seen[key]
            return own + kids + (sum(counted(c, depth + 1) for c in vanished.get(key[0], []) if c != key) if depth < 20 else 0)

        new_seen: dict[tuple[int, int], tuple[int, int, int, str | None]] = {}
        for pid, (ppid, own, kids, start) in now.items():
            key = (pid, start)
            folder = owners[pid]
            before = self.seen.get(key)
            if before is not None:
                grown = own - before[1]
                lost = max(0.0, (kids - before[2]) - sum(counted(c) for c in vanished.get(pid, [])))
                ticks[folder] = ticks.get(folder, 0.0) + max(0, grown) + lost
            elif not first:
                ticks[folder] = ticks.get(folder, 0.0) + own + kids
            new_seen[key] = (ppid, own, kids, folder)
        self.seen = new_seen
        return {} if first else {folder: t / self.tick for folder, t in ticks.items() if t > 0}



_buckets: dict[int, CpuBucket] = {}
_sampler = CpuSampler()
_cpu_now: dict[str, float] = {}
_cpu_loaded = False
_cpu_saved = 0.0


def _load_average(proc: Path = Path("/proc")) -> list[float]:
    try:
        return [float(x) for x in (proc / "loadavg").read_text().split()[:3]]
    except (OSError, ValueError):
        return []


def record(seconds: dict[str | None, float], elapsed: float, load: float, at: float) -> None:
    """Add one sample to the slot it falls in; slots older than 24 hours are dropped."""
    index = int(at // CPU_STEP)
    bucket = _buckets.setdefault(index, CpuBucket(start=index * CPU_STEP))
    for folder, used in seconds.items():
        bucket.seconds[folder or ""] = bucket.seconds.get(folder or "", 0.0) + used
    # the load is the mean over the sampled time of the slot
    bucket.load = (bucket.load * bucket.span + load * elapsed) / (bucket.span + elapsed) if bucket.span + elapsed else load
    bucket.span += elapsed
    for old in [i for i in _buckets if i * CPU_STEP < at - CPU_KEEP]:
        del _buckets[old]


def _load_cpu() -> None:
    global _cpu_loaded
    _cpu_loaded = True
    try:
        for raw in json.loads(_cpu_file().read_text())["buckets"]:
            bucket = CpuBucket.model_validate(raw)
            _buckets.setdefault(int(bucket.start // CPU_STEP), bucket)
    except (OSError, ValueError, KeyError, TypeError):
        pass


def _save_cpu(force: bool = False) -> None:
    global _cpu_saved
    if not force and time.time() - _cpu_saved < CPU_SAVE_EVERY:
        return
    _cpu_saved = time.time()
    try:
        _cpu_file().parent.mkdir(parents=True, exist_ok=True)
        _cpu_file().write_text(json.dumps({"buckets": [b.model_dump() for b in _buckets.values()]}))
    except OSError as exc:
        log.warning("CPU history not kept: %s", exc)


def sample_cpu(now: float | None = None, proc: Path = Path("/proc")) -> None:
    global _cpu_now, _last_sample
    at = now if now is not None else time.time()
    seconds = _sampler.sample(proc)
    elapsed = at - _last_sample if _last_sample else 0.0
    _last_sample = at
    if elapsed <= 0:
        return
    load = _load_average(proc)
    _cpu_now = {(folder or ""): used / elapsed for folder, used in seconds.items()}
    record(seconds, elapsed, load[0] if load else 0.0, at)


_last_sample = 0.0


def cpu_usage() -> dict:
    if not _cpu_loaded:
        _load_cpu()
    return {
        "cpu_cores": os.cpu_count() or 1,
        "load_average": _load_average(),
        "cpu_now": dict(_cpu_now),
        "cpu_step": CPU_STEP,
        "cpu_history": [_buckets[i] for i in sorted(_buckets)],
    }


async def keep_sampling() -> None:
    """Sample the CPU time every half minute; cheap (one pass over /proc). The history is kept in the home every 15 minutes."""
    if not _cpu_loaded:
        _load_cpu()
    while True:
        try:
            await asyncio.to_thread(sample_cpu)
            await asyncio.to_thread(_save_cpu)
        except Exception:
            log.exception("Sampling the CPU failed")
        await asyncio.sleep(CPU_SAMPLE_EVERY)
