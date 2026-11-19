"""Frees room on the workspaces' volume with what can be made again, without any model.

The app decides when (``app/plainboard/orchestration/cleanup.py``) and which agent folders are safe:
their agent runs no turn and has no open assignment. Here, once more right before a folder is touched,
nothing may work in it (no process has its working directory there). What goes, in this order, until
``need`` bytes are free:

1. job logs past their retention;
2. files in the package caches of the shared home that were not used for a month (hard: a week);
3. folders no agent has any more, entirely, but only if everything in them is a git clone without
   changes, stash or unpushed commits (else only their build outputs, as below);
4. build outputs in the folders of idle agents, the longest idle first: only folders git ignores
   (never anything tracked or merely untracked), or outside a clone with the marker the tool leaves
   (``CACHEDIR.TAG`` of cargo, ``pyvenv.cfg``). Soft: build output and tool caches; hard: also
   installed dependencies (``node_modules``, virtualenvs) and ``dist``/``build``.

Never: uploads, the database, secrets, the runner's own clones, anything of a project with a folder
of its own (the app does not send those), git history. ``git gc`` is not run on purpose: git runs
``gc --auto`` itself, and a forced one rewrites the packs, which is a lot of writing for little room.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from plainboard_protocol import CleanedItem, CleanupFolder, CleanupResult, CleanupSpec

from . import config, jobs
from .usage import folder_of

log = logging.getLogger(__name__)

SOFT_BUILD = frozenset({"target", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next", ".nuxt", ".turbo", ".parcel-cache", ".svelte-kit"})
HARD_BUILD = SOFT_BUILD | {"node_modules", ".venv", "venv", "dist", "build", ".tox", ".gradle"}
# Outside a clone git can't say what is generated: only folders the tool marked as its own.
MARKERS = {"target": "CACHEDIR.TAG", ".venv": "pyvenv.cfg", "venv": "pyvenv.cfg"}
UNMARKED_OK = frozenset({"node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
# An agent that finished its last turn later than this keeps its build outputs (it may go on soon).
MIN_IDLE_SECONDS = {"soft": 24 * 3600, "hard": 3600}
CACHES = (".cargo/registry/cache", ".npm/_cacache", ".cache/pip", ".cache/yarn")
CACHE_UNUSED_DAYS = {"soft": 30, "hard": 7}
SEARCH_DEPTH = 3  # clones are searched this deep below an agent's folder
GIT_TIMEOUT = 300


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        timeout=GIT_TIMEOUT,
        check=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},  # status writes no index
    )
    return out.stdout.decode(errors="replace")


def _size(paths: list[Path]) -> dict[Path, int]:
    """Bytes on disk of each path, at the lowest priority."""
    if not paths:
        return {}
    cmd = ["du", "-skx", "--", *map(str, paths)]
    if shutil.which("ionice"):
        cmd = ["ionice", "-c3", *cmd]
    if shutil.which("nice"):
        cmd = ["nice", "-n19", *cmd]
    out = subprocess.run(cmd, capture_output=True, timeout=3600).stdout.decode(errors="replace")
    sizes: dict[Path, int] = {}
    for line in out.splitlines():
        kb, _, path = line.partition("\t")
        if kb.isdigit():
            sizes[Path(path)] = int(kb) * 1024
    return sizes


def busy_folders(proc: Path = Path("/proc")) -> set[str]:
    """The agent folders some process works in right now."""
    found: set[str] = set()
    try:
        entries = [e for e in proc.iterdir() if e.name.isdigit()]
    except OSError:
        return found
    for entry in entries:
        try:
            folder = folder_of(os.readlink(entry / "cwd"))
        except OSError:
            continue
        if folder:
            found.add(folder)
    return found


def _is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def repos(folder: Path, depth: int = SEARCH_DEPTH) -> list[Path]:
    """Clones (and worktrees) in a folder, not below one another."""
    if _is_repo(folder):
        return [folder]
    if depth == 0:
        return []
    found = []
    try:
        children = sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return []
    for child in children:
        if child.name not in HARD_BUILD:
            found += repos(child, depth - 1)
    return found


def _marked(path: Path) -> bool:
    marker = MARKERS.get(path.name)
    return (path / marker).exists() if marker else path.name in UNMARKED_OK


def build_outputs(folder: Path, level: str) -> list[Path]:
    """What can be built again in an agent's folder: in a clone what git ignores, elsewhere what its tool marked."""
    names = HARD_BUILD if level == "hard" else SOFT_BUILD
    found: list[Path] = []
    clones = repos(folder)
    for repo in clones:
        try:
            listed = _git(repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory")
        except (subprocess.SubprocessError, OSError):
            continue
        for rel in listed.split("\0"):
            if rel.endswith("/") and Path(rel).name in names:
                path = repo / rel.rstrip("/")
                if path.is_dir() and not path.is_symlink():
                    found.append(path)

    def outside(path: Path, depth: int) -> None:
        if depth < 0 or path in clones:
            return
        try:
            children = sorted(p for p in path.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            return
        for child in children:
            if child in clones:
                continue
            if child.name in names and _marked(child):
                found.append(child)
            elif not child.name.startswith("."):
                outside(child, depth - 1)

    outside(folder, SEARCH_DEPTH)
    return found


def unpushed(folder: Path) -> str | None:
    """Why a folder must stay: work in a clone that is nowhere else, or anything that is not a clone. None: it may go."""
    try:
        entries = sorted(folder.iterdir())
    except OSError as exc:
        return str(exc)
    for entry in entries:
        if entry.is_symlink():
            continue  # the link goes, never what it points at
        if not entry.is_dir():
            return f"{entry.name} is not in a clone"
        if not _is_repo(entry):
            if entry.name in HARD_BUILD and _marked(entry):
                continue
            return f"{entry.name} is not a clone"
        try:
            if _git(entry, "status", "--porcelain").strip():
                return f"{entry.name} has changes"
            if _git(entry, "stash", "list").strip():
                return f"{entry.name} has a stash"
            if _git(entry, "rev-list", "-n1", "HEAD", "--branches", "--not", "--remotes").strip():
                return f"{entry.name} has unpushed commits"
        except (subprocess.SubprocessError, OSError) as exc:
            return f"{entry.name}: git could not tell ({type(exc).__name__})"
    return None


def _remove(paths: list[Path], what: str, result: CleanupResult) -> None:
    sizes = _size(paths)
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)
        left = _size([path]).get(path, 0) if path.exists() else 0
        freed = max(0, sizes.get(path, 0) - left)
        result.items.append(CleanedItem(path=str(path), what=what, bytes=freed))  # type: ignore[arg-type]
        result.freed += freed


def trim_cache(root: Path, older_than: float) -> int:
    """Delete the files of a cache not used since then; the tools fetch them again when needed."""
    freed = 0
    for dirpath, dirnames, files in os.walk(root, topdown=False):
        for name in files:
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
                if max(st.st_atime, st.st_mtime) < older_than:
                    os.unlink(path)
                    freed += st.st_blocks * 512 or st.st_size  # blocks can lag behind on some file systems
            except OSError:
                pass
        if dirpath != str(root):
            try:
                os.rmdir(dirpath)  # only if empty now
            except OSError:
                pass
    return freed


def _valid(entry: CleanupFolder) -> Path | None:
    """An agent's folder: exactly two levels below the workspaces root, no link."""
    root = config.WORKSPACES_ROOT.resolve()
    path = Path(entry.path)
    if path.is_symlink() or not path.is_dir():
        return None
    path = path.resolve()
    if not path.is_relative_to(root) or len(path.relative_to(root).parts) != 2:
        return None
    return path


def run(spec: CleanupSpec, now: float | None = None) -> CleanupResult:
    now = now or time.time()
    result = CleanupResult()

    logs = jobs.prune()
    if logs:
        result.items.append(CleanedItem(path=str(config.JOBS_ROOT), what="logs", bytes=logs))
        result.freed += logs

    older_than = now - CACHE_UNUSED_DAYS[spec.level] * 86400
    for rel in CACHES:
        root = Path.home() / rel
        if root.is_dir() and not root.is_symlink():
            freed = trim_cache(root, older_than)
            if freed:
                result.items.append(CleanedItem(path=str(root), what="cache", bytes=freed))
                result.freed += freed

    min_idle = MIN_IDLE_SECONDS[spec.level]
    for entry in sorted(spec.folders, key=lambda f: (not f.orphan, f.idle_since)):
        if result.freed >= spec.need:
            break
        folder = _valid(entry)
        if folder is None:
            result.skipped.append(f"{entry.path}: not an agent's folder")
            continue
        if not entry.orphan and now - entry.idle_since < min_idle:
            continue
        if str(folder) in busy_folders():
            result.skipped.append(f"{folder}: in use")
            continue
        if entry.orphan:
            reason = unpushed(folder)
            if reason is None:
                _remove([folder], "folder", result)
                continue
            result.skipped.append(f"{folder}: kept, {reason}")
        _remove(build_outputs(folder, spec.level), "build", result)
    log.info("Cleaned up %d bytes (%s, %d items)", result.freed, spec.level, len(result.items))
    return result
