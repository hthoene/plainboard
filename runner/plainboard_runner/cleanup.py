"""Frees room on the workspaces' volume with what can be made again, without any model.

Two uses. The manager lists candidates (``candidates``: build outputs, removed agents' folders, caches
and folders in the shared home, each with its size and how safe it is to delete) and deletes chosen
ones (``delete``). And only in an emergency (the volume nearly full) the app has the runner clean up by
itself (``run``, level soft).

For ``run`` the app decides when (``app/plainboard/orchestration/cleanup.py``) and which agent folders are safe:
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
import re
import shutil
import subprocess
import time
from pathlib import Path

from plainboard_protocol import CandidatesSpec, CleanedItem, CleanupCandidate, CleanupFolder, CleanupResult, CleanupSpec, DeleteSpec

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


def _cwds(proc: Path = Path("/proc")) -> set[str]:
    """The working directories of the processes here."""
    found: set[str] = set()
    try:
        entries = [e for e in proc.iterdir() if e.name.isdigit()]
    except OSError:
        return found
    for entry in entries:
        try:
            found.add(os.readlink(entry / "cwd").removesuffix(" (deleted)"))
        except OSError:
            continue
    return found


def busy_folders(proc: Path = Path("/proc")) -> set[str]:
    """The agent folders some process works in right now."""
    return {folder for cwd in _cwds(proc) if (folder := folder_of(cwd))}


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


# --- candidates for the manager ---------------------------------------------------------------

NEEDED_CACHES = {"ms-playwright": "holds the installed browsers", "puppeteer": "holds the installed browsers"}
OTHER_CACHES = (".cargo/registry/cache", ".npm/_cacache")
SIZES_TTL = 900  # sizes of candidates are measured again after this, or after a deletion
_sizes: dict[str, int] = {}
_sized_at = 0.0
_RANK = {"safe": 0, "busy": 1, "unsure": 2, "keep": 3}


def _measure(paths: list[Path]) -> dict[str, int]:
    global _sizes, _sized_at
    if time.time() - _sized_at > SIZES_TTL:
        _sizes, _sized_at = {}, time.time()
    missing = [p for p in paths if str(p) not in _sizes]
    _sizes.update({str(p): n for p, n in _size(missing).items()})
    return _sizes


def forget_sizes() -> None:
    global _sized_at
    _sized_at = 0.0


def home_folders(home: Path | None = None) -> list[tuple[Path, str]]:
    """What of the shared home may be offered: folders not hidden at its top (the agents' own build and
    scratch folders), the parts of ``~/.cache`` and the package caches. Never other hidden folders
    (toolchains, sign-ins, keys), nor what holds the workspaces, the uploads or the job logs."""
    home = home or Path.home()
    keep = [p.resolve() for p in (config.WORKSPACES_ROOT, config.UPLOADS_DIR, config.JOBS_ROOT)]

    def dirs(folder: Path) -> list[Path]:
        try:
            return sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            return []

    found = [(p, "home") for p in dirs(home) if not p.name.startswith(".") and not any(k.is_relative_to(p.resolve()) for k in keep)]
    cache = home / ".cache"
    if cache.is_dir() and not cache.is_symlink():
        found += [(p, "cache") for p in dirs(cache)]
    found += [(home / rel, "cache") for rel in OTHER_CACHES if (home / rel).is_dir() and not (home / rel).is_symlink()]
    return found


def _on_path(folder: Path, home: Path) -> bool:
    """Tools are installed in it: PATH, or a shell profile of the home, names it."""
    text = os.environ.get("PATH", "").replace(":", "\n")
    for name in (".profile", ".bashrc", ".bash_profile", ".zshrc"):
        try:
            text += "\n" + (home / name).read_text(errors="replace")
        except OSError:
            pass
    prefix = "|".join(re.escape(p) for p in ("$HOME", "${HOME}", "~", str(home)))
    return re.search(rf"(?:{prefix})/{re.escape(folder.name)}(?=[/:\s\"']|$)", text, re.M) is not None


def _clone_state(repo: Path, name: str) -> tuple[str, str]:
    try:
        if _git(repo, "status", "--porcelain").strip():
            return "keep", f"{name} has changes"
        if _git(repo, "stash", "list").strip():
            return "keep", f"{name} has a stash"
        if _git(repo, "rev-list", "-n1", "HEAD", "--branches", "--not", "--remotes").strip():
            return "keep", f"{name} has unpushed commits"
    except (subprocess.SubprocessError, OSError) as exc:
        return "unsure", f"{name}: git could not tell ({type(exc).__name__})"
    return "safe", ""


def assess(folder: Path, top: Path | None = None, depth: int = SEARCH_DEPTH) -> tuple[str, str]:
    """How safe a folder outside the workspaces is to delete: safe if it holds only clones without own
    work and build outputs; keep if a clone in it has work that is nowhere else; else unsure."""
    top = top or folder
    name = folder.relative_to(top).as_posix() if folder != top else folder.name
    if _is_repo(folder):
        return _clone_state(folder, name)
    if (folder / "CACHEDIR.TAG").exists() or (folder.name in MARKERS and _marked(folder)):
        return "safe", ""
    try:
        entries = sorted(folder.iterdir())
    except OSError as exc:
        return "unsure", str(exc)
    worst = ("safe", "")
    for entry in entries:
        if entry.is_symlink():
            continue
        if not entry.is_dir():
            state = ("unsure", f"{entry.relative_to(top.parent).as_posix()} is not in a clone")
        elif entry.name in HARD_BUILD and _marked(entry):
            continue
        elif depth == 0:
            state = ("unsure", f"{entry.relative_to(top.parent).as_posix()} was not looked into")
        else:
            state = assess(entry, top, depth - 1)
        if _RANK[state[0]] > _RANK[worst[0]]:
            worst = state
        if worst[0] == "keep":
            break
    return worst


def _remotes(folder: Path) -> list[str]:
    found = []
    for repo in repos(folder):
        try:
            found.append(_git(repo, "config", "--get", "remote.origin.url").strip())
        except (subprocess.SubprocessError, OSError):
            continue
    return sorted({r for r in found if r})


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def candidates(spec: CandidatesSpec, only: set[str] | None = None, sizes: bool = True) -> list[CleanupCandidate]:
    """What the manager may delete, with its size and how safe that is. ``only``: just what these paths are in."""
    found: list[CleanupCandidate] = []
    cwds = _cwds()
    in_use = {folder_of(c) for c in cwds}

    def wanted(path: Path) -> bool:
        return only is None or any(_within(p, str(path)) or _within(str(path), p) for p in only)

    for entry in spec.folders:
        folder = _valid(entry)
        if folder is None or not wanted(folder):
            continue
        busy = entry.busy or str(folder) in in_use
        if entry.orphan:
            reason = unpushed(folder)
            if reason is None:
                found.append(CleanupCandidate(path=str(folder), what="folder", safety="busy" if busy else "safe", why="in use" if busy else ""))
                continue
            found.append(CleanupCandidate(path=str(folder), what="folder", safety="keep", why=reason))
        for path in build_outputs(folder, "hard"):
            found.append(CleanupCandidate(path=str(path), what="build", safety="busy" if busy else "safe"))

    home = Path.home()
    for path, what in home_folders(home):
        if not wanted(path):
            continue
        if what == "cache":
            why = NEEDED_CACHES.get(path.name, "")
            found.append(CleanupCandidate(path=str(path), what="cache", safety="unsure" if why else "safe", why=why))
            continue
        if _on_path(path, home):
            safety, why = "keep", "holds tools on PATH"
        elif any(_within(c, str(path)) for c in cwds):
            safety, why = "busy", "a process works in it"
        else:
            safety, why = assess(path)
        found.append(CleanupCandidate(path=str(path), what="home", safety=safety, why=why, remotes=_remotes(path)))  # type: ignore[arg-type]

    if sizes:
        measured = _measure([Path(c.path) for c in found])
        for c in found:
            c.bytes = measured.get(c.path, 0)
    return found


def delete(spec: DeleteSpec) -> CleanupResult:
    """Delete chosen candidates: each is looked at again first. Busy and unsure ones only with ``force``,
    never what holds unpushed work."""
    paths = [os.path.normpath(os.path.expanduser(p)) for p in spec.paths]
    found = {c.path: c for c in candidates(CandidatesSpec(folders=spec.folders), only=set(paths), sizes=False)}
    result = CleanupResult()
    for path in paths:
        c = found.get(path)
        if c is None:
            result.skipped.append(f"{path}: not a cleanup candidate")
        elif c.safety == "keep":
            result.skipped.append(f"{path}: kept, {c.why}")
        elif c.safety != "safe" and not spec.force:
            result.skipped.append(f"{path}: {c.safety}{', ' + c.why if c.why else ''}; force=true deletes it anyway")
        else:
            _remove([Path(path)], c.what, result)
    forget_sizes()
    log.info("Deleted %d bytes on request (%d items)", result.freed, len(result.items))
    return result
