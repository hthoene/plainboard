"""Frees room on the workspaces' volume with what can be made again, without any model.

Two uses. The manager lists candidates (``candidates``: build outputs, removed agents' folders, extra
work folders in an agent's folder (worktrees, more clones; kept while they hold work that is nowhere else), caches
and folders in the shared home, each with its size and how safe it is to delete) and deletes chosen
ones (``delete``). And only in an emergency (the volume nearly full) the app has the runner clean up by
itself (``run``, level soft).

For ``run`` the app decides when (``app/plainboard/orchestration/cleanup.py``) and which agent folders are safe:
their agent runs no turn and has no open assignment. Here, once more right before a folder is touched,
nothing may work in it (no process has its working directory there). What goes, in this order, until
``need`` bytes are free:

1. job logs past their retention;
2. files in the package caches of the shared home that were not used for a month (hard: a week);
   leftovers in the temp folder nothing changed in for ``tmp_days`` (regular cleanup only, see ``tmp_leftovers``);
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
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from plainboard_protocol import CandidatesSpec, CleanedItem, CleanupCandidate, CleanupFolder, CleanupResult, CleanupSpec, DeleteSpec, TrimSpec, UnpushedSpec, UnpushedWork

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


MAX_BRANCHES = 200  # a clone with more: only these are looked at


def _many(n: int, one: str, more: str = "") -> str:
    return f"{n} {one if n == 1 else more or one + 's'}"


def own_work(repo: Path) -> list[str]:
    """All of a clone's work that is on no remote: uncommitted changes, stashes, commits on no remote
    branch, local branches without an upstream (and none of that name on a remote)."""
    found = []
    if changes := len(_git(repo, "status", "--porcelain").splitlines()):
        found.append(_many(changes, "uncommitted change"))
    if stashes := len(_git(repo, "stash", "list").splitlines()):
        found.append(_many(stashes, "stash", "stashes"))
    on_remote = set(_git(repo, "for-each-ref", "--format=%(refname:lstrip=3)", "refs/remotes").split())
    ahead, alone = [], []
    for line in _git(repo, "for-each-ref", f"--count={MAX_BRANCHES}", "--format=%(refname:short)\t%(upstream)", "refs/heads").splitlines():
        name, _, upstream = line.partition("\t")
        if n := int(_git(repo, "rev-list", "--count", f"refs/heads/{name}", "--not", "--remotes").strip() or 0):
            ahead.append(f"{name} ({n})")
        elif not upstream and name not in on_remote:
            alone.append(name)
    try:
        detached = _git(repo, "rev-parse", "--symbolic-full-name", "HEAD").strip() == "HEAD"
    except subprocess.CalledProcessError:
        detached = False  # no commit yet
    if detached and (n := int(_git(repo, "rev-list", "--count", "HEAD", "--not", "--remotes").strip() or 0)):
        ahead.append(f"detached HEAD ({n})")
    if ahead:
        found.append("commits on no remote branch: " + ", ".join(ahead))
    if alone:
        found.append("branches without upstream: " + ", ".join(alone))
    return found


def unpushed_work(spec: UnpushedSpec) -> list[UnpushedWork]:
    """The clones in these folders with work that is on no remote (a project moves to another runner)."""
    root = config.WORKSPACES_ROOT.resolve()
    out = []
    for raw in spec.folders:
        folder = Path(raw)
        if folder.is_symlink() or not folder.is_dir():
            continue
        folder = folder.resolve()
        if folder == root or not folder.is_relative_to(root):
            continue
        for repo in repos(folder):
            try:
                problems = own_work(repo)
            except (subprocess.SubprocessError, OSError, ValueError) as exc:
                problems = [f"git could not tell ({type(exc).__name__})"]
            if problems:
                out.append(UnpushedWork(folder=raw, repo=repo.relative_to(folder).as_posix(), problems=problems))
    return out


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


TMP_OWN = ("plainboard-", "systemd-private-", "tmux-", "ssh-", "snap-private")  # the runner's own (sign-ins), the system's, live sessions


def _newest(path: Path) -> float:
    """The newest change of the path or of anything in it."""
    newest = path.lstat().st_mtime
    for dirpath, dirnames, files in os.walk(path):
        for name in dirnames + files:
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, name)).st_mtime)
            except OSError:
                pass
    return newest


def tmp_leftovers(older_than: float, root: Path | None = None) -> list[Path]:
    """Entries at the top of the temp folder left behind: the runner's user's (the agents'), files or folders, nothing
    in them changed since ``older_than`` and no process works in them. Never hidden ones (sockets, locks), links,
    sockets or pipes, nor the runner's own (sign-ins) and the system's."""
    root = root or Path(tempfile.gettempdir())
    cwds = _cwds()
    uid = os.getuid()
    found = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for path in entries:
        try:
            st = path.lstat()
            if path.name.startswith((".", *TMP_OWN)) or st.st_uid != uid or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                continue
            if any(_within(c, str(path)) for c in cwds) or _newest(path) >= older_than:
                continue
        except OSError:
            continue
        found.append(path)
    return found


def _unlink(path: Path, result: CleanupResult) -> None:
    try:
        n = _file_bytes(path.lstat())
        path.unlink()
    except OSError:
        return
    result.items.append(CleanedItem(path=str(path), what="tmp", bytes=n))
    result.freed += n


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

    if spec.tmp_days > 0:
        for path in tmp_leftovers(now - spec.tmp_days * 86400):
            if path.is_dir():
                _remove([path], "tmp", result)
            else:
                _unlink(path, result)

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
# Shared tool caches (below the home): nobody's, trimmed by PlainBoard itself. A project uses one when a
# clone of it has one of these files at its top.
TOOL_CACHES = {
    ".cache/sccache": ("sccache", ("Cargo.toml",)),
    ".cargo/registry/cache": ("cargo", ("Cargo.toml",)),
    ".npm/_cacache": ("npm", ("package.json",)),
    ".cache/yarn": ("yarn", ("package.json",)),
    ".cache/pnpm": ("pnpm", ("package.json",)),
    ".cache/pip": ("pip", ("pyproject.toml", "requirements.txt", "setup.py")),
    ".cache/uv": ("uv", ("pyproject.toml", "requirements.txt", "setup.py")),
    ".cache/go-build": ("go", ("go.mod",)),
}
SIZES_TTL = 900  # sizes of candidates are measured again after this, or after a deletion
_sizes: dict[str, tuple[int, float]] = {}  # path → bytes, newest change in it
_mentions: dict[tuple[str, ...], dict[str, list[str]]] = {}
_sized_at = 0.0


def _survey(paths: list[Path]) -> dict[Path, tuple[int, float]]:
    """Bytes on disk and the newest change of each path and of each entry right in it, in one walk at the lowest priority."""
    if not paths:
        return {}
    cmd = ["du", "-akx", "-d1", "--time", "--time-style=+%s", "--", *map(str, paths)]
    if shutil.which("ionice"):
        cmd = ["ionice", "-c3", *cmd]
    if shutil.which("nice"):
        cmd = ["nice", "-n19", *cmd]
    out = subprocess.run(cmd, capture_output=True, timeout=3600).stdout.decode(errors="replace")
    found: dict[Path, tuple[int, float]] = {}
    for line in out.splitlines():
        kb, _, rest = line.partition("\t")
        at, _, path = rest.partition("\t")
        if kb.isdigit():
            found[Path(path)] = (int(kb) * 1024, float(at) if at.isdigit() else 0.0)
    return found
_RANK = {"safe": 0, "busy": 1, "unsure": 2, "keep": 3}


def _measure(paths: list[Path]) -> dict[str, tuple[int, float]]:
    global _sizes, _mentions, _sized_at
    if time.time() - _sized_at > SIZES_TTL:
        _sizes, _mentions, _sized_at = {}, {}, time.time()
    missing = [p for p in paths if str(p) not in _sizes]
    _sizes.update({str(p): n for p, n in _survey(missing).items()})
    return _sizes


_ERE_SPECIAL = set(".[]()*+?{}|^$\\")
_NAME = "[[:alnum:]_.-]"  # what a folder's name is made of


def mention_pattern(home: str) -> str:
    """How a script names a folder of the home, for ``git grep -E``: ~/x, $HOME/x, ${HOME}/x or the full path, with all of the path after it."""
    prefix = "|".join("".join("\\" + ch if ch in _ERE_SPECIAL else ch for ch in p) for p in ("~", "$HOME", "${HOME}", home.rstrip("/")))
    return f"({prefix})/{_NAME}+(/{_NAME}+)*"


def _named(mention: str, rels: dict[str, str], home: str) -> list[str]:
    """The targets (relative to the home) a found path names: itself and each folder it is in, never a longer name."""
    prefix = next((p for p in ("~/", "$HOME/", "${HOME}/", home.rstrip("/") + "/") if mention.startswith(p)), None)
    parts = mention[len(prefix) :].split("/") if prefix else []
    return [rel for i in range(1, len(parts) + 1) if (rel := "/".join(parts[:i])) in rels]


def mentions(folders: list[Path], targets: list[Path], home: Path) -> dict[str, list[str]]:
    """Path of each target → the agent folders whose clones' files (tracked or untracked, never ignored) name it.

    git prints only the paths it finds (-o), not the lines: clones hold bundles and lock files with lines of megabytes,
    and matching those here holds the interpreter's lock, and with it the runner's event loop, for seconds."""
    if not targets or not folders:
        return {}
    key = tuple(sorted(map(str, folders + targets)))
    if key in _mentions:
        return _mentions[key]
    rels = {t.relative_to(home).as_posix(): str(t) for t in targets}
    found: dict[str, list[str]] = {}
    for folder in folders:
        for repo in repos(folder):
            try:
                out = subprocess.run(
                    ["git", "-C", str(repo), "grep", "-I", "-h", "-o", "--untracked", "-E", "-e", mention_pattern(str(home))],
                    capture_output=True,
                    timeout=GIT_TIMEOUT,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
                ).stdout.decode(errors="replace")
            except (subprocess.SubprocessError, OSError):
                continue
            for mention in sorted(set(out.splitlines())):
                for rel in _named(mention, rels, str(home)):
                    if str(folder) not in found.setdefault(rels[rel], []):
                        found[rels[rel]].append(str(folder))
    _mentions[key] = found
    return found


def users(folders: list[Path], markers: tuple[str, ...]) -> list[str]:
    """The agent folders with a clone that has one of these files at its top: they use that tool."""
    return [str(f) for f in folders if any((r / m).is_file() for r in repos(f) for m in markers)]


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
    """keep: work that would be lost with the folder. A worktree (``.git`` is a file) shares branches and
    stashes with its main clone, which keeps them: only its uncommitted changes and commits on no branch
    (detached HEAD) are its own."""
    worktree = (repo / ".git").is_file()
    try:
        if _git(repo, "status", "--porcelain").strip():
            return "keep", f"{name} has changes"
        if worktree:
            if _git(repo, "rev-list", "-n1", "HEAD", "--not", "--branches", "--remotes").strip():
                return "keep", f"{name} has commits on no branch"
            return "safe", ""
        if _git(repo, "stash", "list").strip():
            return "keep", f"{name} has a stash"
        if _git(repo, "rev-list", "-n1", "HEAD", "--branches", "--not", "--remotes").strip():
            return "keep", f"{name} has unpushed commits"
    except (subprocess.SubprocessError, OSError) as exc:
        return "unsure", f"{name}: git could not tell ({type(exc).__name__})"
    return "safe", ""


def extra_folders(folder: Path, clones: list[str]) -> list[Path]:
    """Folders at the top of an agent's folder besides its clones of the project's repositories: worktrees,
    more clones, scratch folders. Never hidden ones (tool settings) nor build outputs (listed as such)."""
    try:
        children = sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return []
    return [p for p in children if p.name not in clones and not p.name.startswith(".") and not (p.name in HARD_BUILD and _marked(p))]


def _work_state(folder: Path, busy: bool, cwds: set[str]) -> tuple[str, str]:
    """How safe an extra work folder is to delete: kept with work that is nowhere else; busy while its agent
    works or a process works in it; unsure if something in it is not in a clone."""
    safety, why = assess(folder)
    if safety == "keep":
        return safety, why
    if any(_within(c, str(folder)) for c in cwds):
        return "busy", "a process works in it"
    if busy:
        return "busy", why
    return safety, why


def _remove_workdir(path: Path, result: CleanupResult) -> None:
    """Delete an extra work folder; a worktree is also taken off its main clone's list (else its branch stays checked out there)."""
    common = None
    if (path / ".git").is_file():
        try:
            common = (path / _git(path, "rev-parse", "--git-common-dir").strip()).resolve()
        except (subprocess.SubprocessError, OSError):
            common = None
    _remove([path], "workdir", result)
    if common is not None and common.is_dir():
        try:
            _git(common, "worktree", "prune")
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("git worktree prune in %s failed: %s", common, exc)


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
        elif entry.clones is not None:
            for extra in extra_folders(folder, entry.clones):
                if wanted(extra):
                    safety, why = _work_state(extra, entry.busy, cwds)
                    found.append(CleanupCandidate(path=str(extra), what="workdir", safety=safety, why=why))  # type: ignore[arg-type]
        for path in build_outputs(folder, "hard"):
            found.append(CleanupCandidate(path=str(path), what="build", safety="busy" if busy else "safe"))

    home = Path.home()
    tools = {str(home / rel): tool for rel, (tool, _) in TOOL_CACHES.items()}
    for path, what in home_folders(home) if spec.home else []:
        if not wanted(path):
            continue
        if what == "cache":
            why = NEEDED_CACHES.get(path.name, "")
            found.append(CleanupCandidate(path=str(path), what="cache", safety="unsure" if why else "safe", why=why, tool=tools.get(str(path), "")))
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
            c.bytes, c.modified = measured.get(c.path, (0, 0.0))
            if c.what in ("home", "cache"):
                inside = sorted(((Path(p).name, n) for p, (n, _) in measured.items() if os.path.dirname(p) == c.path), key=lambda e: -e[1])
                c.entries = dict(inside[:6])
        agent_folders = [f for e in spec.folders if (f := _valid(e)) is not None]
        named = mentions(agent_folders, [Path(c.path) for c in found if c.what in ("home", "cache") and not c.tool], home)
        for c in found:
            c.mentioned = named.get(c.path, [])
    return found


def _file_bytes(st: os.stat_result) -> int:
    return st.st_blocks * 512 or st.st_size  # on disk, as du counts


def trim_oldest(root: Path, amount: int) -> int:
    """Delete the files of a cache used longest ago until ``amount`` bytes are gone; the tool fetches or builds them again."""
    files = []
    for dirpath, _, names in os.walk(root):
        for name in names:
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            files.append((max(st.st_atime, st.st_mtime), path, _file_bytes(st)))
    freed = 0
    for _, path, n in sorted(files):
        if freed >= amount:
            break
        try:
            os.unlink(path)
            freed += n
        except OSError:
            pass
    for dirpath, _, _ in sorted(os.walk(root), key=lambda w: -len(w[0])):
        if dirpath != str(root):
            try:
                os.rmdir(dirpath)  # only if empty now
            except OSError:
                pass
    return freed


def trim(spec: TrimSpec) -> CleanupResult:
    """Shrink the big shared tool caches, the biggest first and in each the oldest files first, never below
    ``keep`` nor below half, until ``need`` bytes are gone. Each item says which agent folders use that tool."""
    home = Path.home()
    roots = [(home / rel, tool, markers) for rel, (tool, markers) in TOOL_CACHES.items() if (home / rel).is_dir() and not (home / rel).is_symlink()]
    sizes = _size([r for r, _, _ in roots])
    folders = [f for e in spec.folders if (f := _valid(e)) is not None]
    result = CleanupResult()
    for root, tool, markers in sorted(roots, key=lambda r: -sizes.get(r[0], 0)):
        size = sizes.get(root, 0)
        if result.freed >= spec.need or size < spec.large:
            continue
        amount = min(size - max(spec.keep, size // 2), spec.need - result.freed)
        freed = trim_oldest(root, amount) if amount > 0 else 0
        if freed:
            result.items.append(CleanedItem(path=str(root), what="cache", bytes=freed, users=users(folders, markers)))
            result.freed += freed
            log.info("Trimmed the %s cache %s by %d bytes", tool, root, freed)
    forget_sizes()
    return result


def delete(spec: DeleteSpec) -> CleanupResult:
    """Delete chosen candidates: each is looked at again first. Busy and unsure ones only with ``force``,
    never what holds unpushed work."""
    paths = [os.path.normpath(os.path.expanduser(p)) for p in spec.paths]
    found = {c.path: c for c in candidates(CandidatesSpec(folders=spec.folders), only=set(paths), sizes=False)}
    result = CleanupResult()
    for path in paths:
        c = found.get(path)
        if c is None:
            result.skipped.append(f"{path}: gone already" if not os.path.lexists(path) else f"{path}: not a cleanup candidate (list_cleanup shows what can go)")
        elif c.safety == "keep":
            result.skipped.append(f"{path}: kept, {c.why}")
        elif c.safety != "safe" and not spec.force:
            result.skipped.append(f"{path}: {c.safety}{', ' + c.why if c.why else ''}; force=true deletes it anyway")
        elif c.what == "workdir":
            _remove_workdir(Path(path), result)
        else:
            _remove([Path(path)], c.what, result)
    forget_sizes()
    log.info("Deleted %d bytes on request (%d items)", result.freed, len(result.items))
    return result
