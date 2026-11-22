"""The project's Claude skills in an agent's workspace, and skills fetched from git for the app.

Before a turn the provider names the folder its skills go to (Claude Code: ``<workspace>/.claude/skills``).
What PlainBoard laid down there is listed in ``MANIFEST`` with its hash: a skill is fetched from the app (the
turn's token) only when its hash changed, and one that is no longer the agent's is removed. Folders PlainBoard
did not lay down stay as they are, and so does everything else in ``.claude``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath

from plainboard_protocol import RepoSpec, SkillFetch, SkillSpec

from .workspace import WorkspaceError, _no_secrets, config_args, git_env, run_cmd

log = logging.getLogger(__name__)

MANIFEST = ".plainboard-skills.json"
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_BYTES = 20 * 1024 * 1024  # all files of a skill, unpacked
MAX_FILES = 500
FETCH_TIMEOUT_SECONDS = 180

Download = Callable[[int], Awaitable[bytes]]


def _manifest(folder: Path) -> dict[str, dict]:
    try:
        data = json.loads((folder / MANIFEST).read_text())
        return {k: v for k, v in data.items() if isinstance(v, dict) and NAME.match(k)} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_manifest(folder: Path, manifest: dict[str, dict]) -> None:
    if not manifest:
        (folder / MANIFEST).unlink(missing_ok=True)
        return
    tmp = folder / f"{MANIFEST}.tmp"
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    os.replace(tmp, folder / MANIFEST)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _extract(data: bytes, target: Path) -> None:
    """The skill's zip into ``target``; every path is checked again, whatever the app sent."""
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise WorkspaceError(f"more than {MAX_FILES} files")
        for info in infos:
            parts = PurePosixPath(info.filename).parts
            if not parts or info.filename.startswith("/") or "\\" in info.filename or any(p in ("..", ".") for p in parts):
                raise WorkspaceError(f"bad path {info.filename[:200]}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise WorkspaceError(f"link {info.filename[:200]}")
            dest = target.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, dest.open("wb") as out:
                while chunk := src.read(65536):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise WorkspaceError("too large")
                    out.write(chunk)


async def sync(folder: Path, skills: list[SkillSpec], download: Download) -> list[str]:
    """Lay the agent's skills into ``folder`` and remove those PlainBoard laid down that it no longer gets.
    Returns problems for the turn's notice (a skill that fails must not stop the turn)."""
    manifest = _manifest(folder) if folder.is_dir() else {}
    if not skills and not manifest:
        return []
    folder.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    wanted = {s.name: s for s in skills if NAME.match(s.name)}
    changed = False
    for name in [n for n in manifest if n not in wanted]:
        await asyncio.to_thread(_remove, folder / name)
        del manifest[name]
        changed = True
    for name, skill in wanted.items():
        target = folder / name
        if manifest.get(name, {}).get("hash") == skill.hash and (target / "SKILL.md").is_file():
            continue
        if name not in manifest and (target.exists() or target.is_symlink()):
            problems.append(f"Skill '{name}' not laid down: {target} exists and is not PlainBoard's")
            continue
        tmp = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=folder))
        try:
            data = await download(skill.id)
            await asyncio.to_thread(_extract, data, tmp)
            await asyncio.to_thread(_remove, target)
            os.replace(tmp, target)
        except Exception as exc:  # the turn goes on without it
            log.warning("Skill %s not laid down: %s", name, exc)
            problems.append(f"Skill '{name}' could not be laid down: {str(exc)[:200]}")
            continue
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        manifest[name] = {"id": skill.id, "hash": skill.hash}
        changed = True
    if changed:
        _write_manifest(folder, manifest)
    return problems


def _pack(source: Path) -> bytes:
    out = io.BytesIO()
    total = files = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for root, dirs, names in os.walk(source):
            dirs[:] = sorted(d for d in dirs if d != ".git")
            for entry in [Path(root) / d for d in dirs] + [Path(root) / n for n in sorted(names)]:
                if entry.is_symlink():
                    raise WorkspaceError(f"The skill's folder holds a link, which skills may not: {entry.relative_to(source)}")
            for name in sorted(names):
                path = Path(root) / name
                files += 1
                total += path.stat().st_size
                if files > MAX_FILES or total > MAX_BYTES:
                    raise WorkspaceError(f"The skill's folder is too large (at most {MAX_FILES} files, {MAX_BYTES // (1024 * 1024)} MB)")
                archive.write(path, path.relative_to(source).as_posix())
    return out.getvalue()


async def fetch(spec: SkillFetch) -> bytes:
    """Clone ``spec.url`` (shallow) and give its folder ``spec.path`` as a zip."""
    repo = RepoSpec(name="skill", url=spec.url)
    with tempfile.TemporaryDirectory(prefix="plainboard-skill-") as tmp:
        clone = Path(tmp) / "repo"
        args = ["git", *config_args(repo, spec.credentials), "clone", "--depth", "1"] + (["--branch", spec.ref] if spec.ref else []) + ["--", spec.url, str(clone)]
        try:
            await run_cmd(args, cwd=None, env=git_env(spec.credentials), timeout=FETCH_TIMEOUT_SECONDS)
        except WorkspaceError as exc:
            raise WorkspaceError(_no_secrets(str(exc), repo, spec.credentials)[-500:]) from None
        source = (clone / spec.path).resolve() if spec.path else clone.resolve()
        if source != clone.resolve() and clone.resolve() not in source.parents:
            raise WorkspaceError("Not a folder in the repository")
        if not source.is_dir():
            raise WorkspaceError(f"The repository has no folder '{spec.path}'")
        return await asyncio.to_thread(_pack, source)
