"""Runner HTTP service (internal network only).

POST /runs streams NDJSON events for one agent turn. The app is the only client.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from plainboard_protocol import CheckResult, CompletionResult, CompletionSpec, Credentials, ErrorEvent, JobSpec, JobState, ProviderInfo, RunSpec
from pydantic import BaseModel

from . import jobs
from . import notices
from .providers import get_provider, list_providers
from .tool_bridge import ToolBridge
from .workspace import WorkspaceError, git_env, prepare_workspace, run_cmd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("plainboard.runner")

app = FastAPI(title="PlainBoard Runner")
_active: dict[str, asyncio.Task[None]] = {}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/providers", response_model=list[ProviderInfo])
async def providers() -> list[ProviderInfo]:
    return [p.info() for p in list_providers()]


async def _execute(spec: RunSpec, queue: asyncio.Queue[str | None]) -> None:
    bridge: ToolBridge | None = None
    try:
        provider = get_provider(spec.provider)
        workspace, problems = await prepare_workspace(spec.workspace, spec.credentials)
        if problems:  # tell the agent instead of failing the whole run
            notice = "\n".join(f"- {p}" for p in problems)
            spec = spec.model_copy(update={"prompt": f"[Workspace notice]\n{notice}\n\n{spec.prompt}"})
        bridge = ToolBridge(spec.bridge, workspace)
        async for event in provider.run(spec, bridge):
            await queue.put(event.model_dump_json())
    except asyncio.CancelledError:
        await queue.put(ErrorEvent(message="Run cancelled", cancelled=True).model_dump_json())
        raise
    except Exception as exc:
        log.exception("Run %s failed", spec.run_id)
        await queue.put(ErrorEvent(message=str(exc)[:4000]).model_dump_json())
    finally:
        if bridge:
            await bridge.aclose()
        await queue.put(None)


@app.post("/runs")
async def start_run(spec: RunSpec) -> StreamingResponse:
    if spec.run_id in _active:
        raise HTTPException(409, "Run already active")
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    task = asyncio.create_task(_execute(spec, queue))
    _active[spec.run_id] = task

    async def stream() -> AsyncIterator[str]:
        try:
            while (line := await queue.get()) is not None:
                yield line + "\n"
        finally:
            _active.pop(spec.run_id, None)
            notices.drain(spec.run_id)
            if not task.done():  # client went away -> stop the agent
                task.cancel()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, bool]:
    task = _active.get(run_id)
    if task and not task.done():
        task.cancel()
        return {"cancelled": True}
    return {"cancelled": False}


@app.post("/jobs", response_model=JobState)
async def start_job(spec: JobSpec) -> JobState:
    try:
        return await jobs.start(spec)
    except (ValueError, WorkspaceError, OSError) as exc:
        raise HTTPException(400, str(exc)[:500]) from exc


@app.post("/jobs/status", response_model=dict[str, JobState])
async def job_status(ids: list[str]) -> dict[str, JobState]:
    return jobs.status(ids)


@app.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str) -> dict[str, bool]:
    return {"stopped": jobs.stop(job_id)}


class RunNotice(BaseModel):
    text: str


@app.post("/runs/{run_id}/notify")
async def notify_run(run_id: str, body: RunNotice) -> dict[str, bool]:
    if run_id not in _active:
        return {"delivered": False}
    notices.push(run_id, body.text)
    return {"delivered": True}


@app.post("/complete", response_model=CompletionResult)
async def complete(spec: CompletionSpec) -> CompletionResult:
    try:
        return await get_provider(spec.provider).complete(spec)
    except Exception as exc:
        raise HTTPException(502, str(exc)[:2000]) from exc


@app.post("/checks/provider", response_model=CheckResult)
async def check_provider(spec: CompletionSpec) -> CheckResult:
    try:
        provider = get_provider(spec.provider)
    except KeyError as exc:
        return CheckResult(ok=False, detail=str(exc))
    return await provider.check(spec)


class GitHubRepo(BaseModel):
    full_name: str
    name: str
    clone_url: str
    description: str | None = None
    default_branch: str | None = None
    private: bool = False


class GitHubReposResult(BaseModel):
    ok: bool
    detail: str = ""
    repos: list[GitHubRepo] = []


@app.post("/github/repos", response_model=GitHubReposResult)
async def github_repos(credentials: Credentials) -> GitHubReposResult:
    """All repositories the token can access (own, collaborator, organisation member)."""
    if not credentials.github_token:
        return GitHubReposResult(ok=False, detail="No GitHub token configured")
    jq = ".[] | {full_name, name, clone_url, description, default_branch, private}"
    path = "user/repos?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member"
    try:
        out = await run_cmd(["gh", "api", "--paginate", path, "--jq", jq], cwd=None, env=git_env(credentials), timeout=90)
    except WorkspaceError as exc:
        return GitHubReposResult(ok=False, detail=str(exc)[:400])
    repos = [GitHubRepo.model_validate_json(line) for line in out.splitlines() if line.strip()]
    return GitHubReposResult(ok=True, repos=repos)


class GitHubCheck(BaseModel):
    credentials: Credentials
    repo_urls: list[str] = []


@app.post("/checks/github", response_model=CheckResult)
async def check_github(body: GitHubCheck) -> CheckResult:
    """Verify the token (if any) and that every repository is reachable."""
    env = git_env(body.credentials)
    parts: list[str] = []
    ok = True
    if body.credentials.github_token:
        try:
            login = await run_cmd(["gh", "api", "user", "--jq", ".login"], cwd=None, env=env, timeout=30)
            parts.append(f"Authenticated as {login}")
        except WorkspaceError as exc:
            return CheckResult(ok=False, detail=f"Token rejected: {str(exc)[:300]}")
    elif not body.repo_urls:
        return CheckResult(ok=False, detail="No GitHub token configured")
    for url in body.repo_urls:
        try:
            await run_cmd(["git", "ls-remote", "--heads", url], cwd=None, env=env, timeout=60)
        except WorkspaceError:
            ok = False
            parts.append(f"not reachable: {url}")
    if body.repo_urls and ok:
        parts.append(f"{len(body.repo_urls)} repositor{'y' if len(body.repo_urls) == 1 else 'ies'} reachable")
    return CheckResult(ok=ok, detail="; ".join(parts))
