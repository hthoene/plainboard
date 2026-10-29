"""Workspace preparation and process environment for agent runs."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from plainboard_protocol import Credentials, WorkspaceSpec

from .config import WORKSPACES_ROOT

log = logging.getLogger(__name__)


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
        raise WorkspaceError(f"Command timed out: {' '.join(args)}")
    text = out.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise WorkspaceError(f"{' '.join(args[:2])} failed: {text[-1000:]}")
    return text


async def prepare_workspace(spec: WorkspaceSpec, creds: Credentials) -> tuple[Path, list[str]]:
    """Create the agent's working directory and clone missing repositories into sub folders.

    Returns the workspace path and human-readable problems (a failing clone must not block the
    agent; it is told about it instead).
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
            continue
        args = ["git", "clone"] + (["--branch", repo.branch] if repo.branch else []) + [repo.url, str(target)]
        log.info("Cloning %s into %s", repo.url, target)
        try:
            await run_cmd(args, cwd=None, env=git_env(creds))
        except WorkspaceError as exc:
            problems.append(f"Could not clone '{repo.name}' ({repo.url}): {str(exc)[-300:]}")
    return path, problems
