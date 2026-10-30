"""Workspace preparation and process environment for agent runs."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

from plainboard_protocol import Credentials, WorkspaceSpec

from .config import WORKSPACES_ROOT

log = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 120
SETUP_TIMEOUT_SECONDS = 1800

# Scripts that succeeded since this container started. In memory on purpose: system packages are
# gone after a rebuild, so every script has to run again.
_setup_done: set[str] = set()
_setup_lock = asyncio.Lock()  # parallel runs wait for the install instead of repeating it


class WorkspaceError(RuntimeError):
    pass


def resolve_workspace(spec: WorkspaceSpec) -> Path:
    path = Path(spec.path).resolve()
    root = WORKSPACES_ROOT.resolve()
    if path != root and root not in path.parents:
        raise WorkspaceError(f"Workspace {path} is outside {root}")
    return path


def git_env(creds: Credentials) -> dict[str, str]:
    env: dict[str, str] = {
        "GIT_AUTHOR_NAME": creds.git_author_name,
        "GIT_AUTHOR_EMAIL": creds.git_author_email,
        "GIT_COMMITTER_NAME": creds.git_author_name,
        "GIT_COMMITTER_EMAIL": creds.git_author_email,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if creds.github_token:
        env["GH_TOKEN"] = creds.github_token
        env["GITHUB_TOKEN"] = creds.github_token
    return env


async def run_cmd(args: list[str], cwd: Path | None, env: dict[str, str], timeout: float = 600) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **env},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise WorkspaceError(f"Command timed out: {' '.join(args[:2])}")
    except asyncio.CancelledError:
        proc.kill()
        raise
    text = out.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise WorkspaceError(f"{' '.join(args[:2])} failed: {text[-1000:]}")
    return text


async def _update_clone(name: str, target: Path, env: dict[str, str], *, shared: bool) -> str | None:
    """Bring an existing clone up to date without touching local work. Returns a problem, if any."""
    if not (target / ".git").exists():
        return None
    try:
        await run_cmd(["git", "fetch", "--all", "--prune"], cwd=target, env=env, timeout=FETCH_TIMEOUT_SECONDS)
    except WorkspaceError as exc:
        return f"Could not update '{name}', it may be out of date: {str(exc)[-300:]}"
    if shared:  # the user's own folder: only refresh the remote branches
        return None
    try:
        if not await run_cmd(["git", "status", "--porcelain", "--untracked-files=no"], cwd=target, env=env):
            await run_cmd(["git", "merge", "--ff-only", "@{u}"], cwd=target, env=env)
    except WorkspaceError as exc:  # no upstream or own commits: local work stays as it is
        log.info("Not fast-forwarding %s: %s", name, exc)
    return None


async def _run_setup(script: str, cwd: Path, env: dict[str, str]) -> str | None:
    """Run the project's setup script once per container and version. Returns a problem, if any."""
    key = hashlib.sha256(script.encode()).hexdigest()
    async with _setup_lock:
        if key in _setup_done:
            return None
        log.info("Running setup script %s", key[:12])
        try:
            await run_cmd(["bash", "-e", "-c", script], cwd=cwd, env=env, timeout=SETUP_TIMEOUT_SECONDS)
        except WorkspaceError as exc:
            return f"The project's setup script failed, so tools may be missing: {str(exc)[-600:]}"
        _setup_done.add(key)
    return None


async def prepare_workspace(spec: WorkspaceSpec, creds: Credentials) -> tuple[Path, list[str]]:
    """Create the agent's working directory, clone missing repositories into sub folders, update
    existing clones and run the project's setup script.

    Returns the workspace path and human-readable problems (a failing step must not block the agent;
    it is told about it instead).
    """
    path = resolve_workspace(spec)
    path.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    for repo in spec.repos:
        target = (path / repo.name).resolve()
        if path not in target.parents:
            problems.append(f"Repository name '{repo.name}' is not a valid folder name")
            continue
        if target.exists() and any(target.iterdir()):
            if problem := await _update_clone(repo.name, target, git_env(creds), shared=spec.shared):
                problems.append(problem)
            continue
        args = ["git", "clone"] + (["--branch", repo.branch] if repo.branch else []) + [repo.url, str(target)]
        log.info("Cloning %s into %s", repo.url, target)
        try:
            await run_cmd(args, cwd=None, env=git_env(creds))
        except WorkspaceError as exc:
            problems.append(f"Could not clone '{repo.name}' ({repo.url}): {str(exc)[-300:]}")
    if spec.setup_script.strip() and (problem := await _run_setup(spec.setup_script, path, git_env(creds))):
        problems.append(problem)
    return path, problems
