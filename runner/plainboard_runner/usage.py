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
import shutil
import time
from pathlib import Path

from plainboard_protocol import FolderUsage, MachineUsage, ProcessMemory, VolumeUsage

from . import config
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
    """Bytes on disk of each path, on its own file system, at the lowest priority. What is done by the timeout counts."""
    if not paths:
        return {}
    cmd = ["du", "-skx", "--", *paths]
    if shutil.which("ionice"):
        cmd = ["ionice", "-c3", *cmd]
    if shutil.which("nice"):
        cmd = ["nice", "-n19", *cmd]
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    sizes: dict[str, int] = {}

    async def read() -> None:
        assert proc.stdout
        async for raw in proc.stdout:
            kb, _, path = raw.decode(errors="replace").rstrip("\n").partition("\t")
            if kb.isdigit():
                sizes[path] = int(kb) * 1024
        await proc.wait()

    try:
        await asyncio.wait_for(read(), timeout)
    except TimeoutError:
        log.warning("Measuring the folders took longer than %ss; stopped", timeout)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return sizes


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

    by_folder: dict[str | None, ProcessMemory] = {}
    for pid, size in sizes.items():
        key = owner(pid)
        row = by_folder.setdefault(key, ProcessMemory(folder=key, bytes=0, processes=0))
        row.bytes += size
        row.processes += 1
    return total, used, sorted(by_folder.values(), key=lambda m: -m.bytes)


async def current() -> MachineUsage:
    measure_soon()
    vols, (total, used, mem) = await asyncio.gather(asyncio.to_thread(volumes), asyncio.to_thread(memory))
    return MachineUsage(volumes=vols, folders=_folders, measured_at=_measured_at, measuring=measuring(), memory_total=total, memory_used=used, memory=mem)
