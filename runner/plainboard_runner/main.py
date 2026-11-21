"""Runner HTTP service (internal network only).

POST /runs streams NDJSON events for one agent turn. The app is the only client.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import StreamingResponse
from plainboard_protocol import ACCOUNT_KEY_PATTERN, DEFAULT_ACCOUNT, BranchRef, CandidatesSpec, CheckResult, CleanupCandidate, CleanupResult, CleanupSpec, CommitRef, CompletionResult, CompletionSpec, Credentials, DeleteSpec, ErrorEvent, JobPort, JobSpec, JobState, LimitsEvent, MachineUsage, ProjectFiles, ProviderAccount, ProviderInfo, PullRequestCheck, PullRequestRef, PullRequestState, RepoSpec, RerunRef, RunSpec, TidyResult, TidySpec, TrainCheckSpec, TrainLandSpec, TrainResult, TrainSpec, TrimSpec, UpstreamRef, UpstreamState
from pydantic import BaseModel, Field

from . import cleanup
from . import config
from . import connect
from . import errors
from . import github
from .browser import browsers
from . import jobs
from . import notices
from . import ports
from . import trains
from . import translate
from . import usage
from .providers import get_provider, list_providers
from .tool_bridge import ToolBridge
from .workspace import WorkspaceError, config_args, git_env, prepare_workspace, project_folder, remove_folder, run_cmd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("plainboard.runner")

_background: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _background.add(asyncio.create_task(usage.keep_measured()))
    _background.add(asyncio.create_task(usage.keep_sampling()))
    # An external runner connects out to PlainBoard (connect.py); the built-in one is called by the app.
    task = asyncio.create_task(connect.serve(), name="connect") if connect.enabled() else None
    try:
        yield
    finally:
        if task:
            task.cancel()
        await browsers.close_all()


app = FastAPI(title="PlainBoard Runner", lifespan=lifespan)
app.include_router(translate.router)  # for the harness of a turn on a model server that speaks Chat Completions
_active: dict[str, asyncio.Task[None]] = {}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/browser/{key}")
async def browser(websocket: WebSocket, key: str) -> None:
    """A browser in this container, shown in PlainBoard (see browser.py). One per key."""
    await browsers.serve(websocket, key)


@app.get("/usage", response_model=MachineUsage)
async def machine_usage() -> MachineUsage:
    """Disk and memory of this machine; the folder sizes as last measured (see usage.py)."""
    return await usage.current()


@app.post("/usage/measure", response_model=MachineUsage)
async def measure_usage_now() -> MachineUsage:
    """Measure the folders again now, in the background."""
    usage.measure_soon(force=True)
    return await usage.current()


_cleaning = asyncio.Lock()


@app.post("/cleanup", response_model=CleanupResult)
async def clean_up(body: CleanupSpec) -> CleanupResult:
    """Free room with what can be made again (see cleanup.py); one cleanup at a time."""
    if _cleaning.locked():
        return CleanupResult(skipped=["a cleanup runs already"])
    async with _cleaning:
        result = await asyncio.to_thread(cleanup.run, body)
    if result.freed:
        usage.measure_soon(force=True)  # the sizes shown are out of date now
    return result


@app.post("/cleanup/candidates", response_model=list[CleanupCandidate])
async def cleanup_candidates(body: CandidatesSpec) -> list[CleanupCandidate]:
    """What the manager may delete, with sizes and how safe that is (see cleanup.py)."""
    return await asyncio.to_thread(cleanup.candidates, body)


@app.post("/cleanup/trim", response_model=CleanupResult)
async def cleanup_trim(body: TrimSpec) -> CleanupResult:
    """Shrink the big shared tool caches, oldest files first (see cleanup.py)."""
    if _cleaning.locked():
        return CleanupResult(skipped=["a cleanup runs already"])
    async with _cleaning:
        result = await asyncio.to_thread(cleanup.trim, body)
    if result.freed:
        usage.measure_soon(force=True)
    return result


@app.post("/cleanup/delete", response_model=CleanupResult)
async def cleanup_delete(body: DeleteSpec) -> CleanupResult:
    """Delete candidates the manager chose; each is looked at again first."""
    async with _cleaning:
        result = await asyncio.to_thread(cleanup.delete, body)
    if result.freed:
        usage.measure_soon(force=True)
    return result


@app.get("/providers", response_model=list[ProviderInfo])
async def providers() -> list[ProviderInfo]:
    return list(await asyncio.gather(*(p.catalog() for p in list_providers())))


def _signin_provider(name: str):
    try:
        return get_provider(name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


AccountKey = Query(DEFAULT_ACCOUNT, pattern=ACCOUNT_KEY_PATTERN)


@app.get("/providers/{name}/account", response_model=ProviderAccount)
async def provider_account(name: str, key: str = AccountKey) -> ProviderAccount:
    try:
        account = await _signin_provider(name).account(key)
    except Exception as exc:
        raise HTTPException(502, str(exc)[:1000]) from exc
    if account is None:
        raise HTTPException(404, f"{name} has no sign-in")
    return account


@app.post("/providers/{name}/login", response_model=ProviderAccount)
async def provider_login(name: str, key: str = AccountKey) -> ProviderAccount:
    try:
        return await _signin_provider(name).login(key)
    except NotImplementedError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, str(exc)[:1000]) from exc


@app.post("/providers/{name}/logout", response_model=ProviderAccount)
async def provider_logout(name: str, key: str = AccountKey) -> ProviderAccount:
    try:
        return await _signin_provider(name).logout(key)
    except NotImplementedError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, str(exc)[:1000]) from exc


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
    except BaseException as exc:  # whatever it is: the app is told, a stream never just ends
        errors.record(f"Run {spec.run_id}", exc)
        await queue.put(ErrorEvent(message=errors.describe(exc)[:4000]).model_dump_json())
        if not isinstance(exc, Exception):
            raise
    finally:
        if bridge:
            await bridge.aclose()
        await queue.put(None)


@app.post("/runs")
async def start_run(spec: RunSpec) -> StreamingResponse:
    if spec.run_id in _active:
        raise HTTPException(409, "Run already active")
    if connect.enabled():  # an external runner: tool calls go straight to PlainBoard's address
        spec.bridge.base_url = connect.APP_URL
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    task = asyncio.create_task(_execute(spec, queue))
    _active[spec.run_id] = task

    async def stream() -> AsyncIterator[str]:
        try:
            while (line := await queue.get()) is not None:
                yield line + "\n"
        except Exception as exc:  # the stream itself broke: still end it with the error
            errors.record(f"Stream of run {spec.run_id}", exc)
            yield ErrorEvent(message=errors.describe(exc)[:4000]).model_dump_json() + "\n"
        finally:
            _active.pop(spec.run_id, None)
            notices.drain(spec.run_id)
            if not task.done():  # client went away -> stop the agent
                task.cancel()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


_UPLOAD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _upload(name: str):
    if not _UPLOAD_NAME.fullmatch(name) or ".." in name:
        raise HTTPException(400, "Not an attachment's name")
    return config.UPLOADS_DIR / name


@app.post("/uploads/missing")
async def missing_uploads(names: list[str]) -> list[str]:
    """An external runner has no uploads volume: which of the project's attachments it lacks (the app sends them)."""
    return [name for name in names if _UPLOAD_NAME.fullmatch(name) and ".." not in name and not _upload(name).is_file()]


@app.put("/uploads/{name}")
async def put_upload(name: str, request: Request) -> dict[str, bool]:
    path = _upload(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{name}.part")
    part.write_bytes(await request.body())
    part.replace(path)
    return {"ok": True}


@app.get("/runs")
async def active_runs() -> list[str]:
    """The turns running here: one the app still waits for but that is not listed was lost."""
    return [run_id for run_id, task in _active.items() if not task.done()]


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


@app.post("/jobs/ports", response_model=dict[str, list[JobPort]])
async def job_ports(ids: list[str]) -> dict[str, list[JobPort]]:
    """The web servers these jobs have started (a job without one is left out)."""
    return await ports.of_jobs(ids)


@app.post("/jobs/listening", response_model=dict[str, list[int]])
async def job_listening(ids: list[str]) -> dict[str, list[int]]:
    """Every port these jobs listen on, web server or not: the app opens a preview only on one of them."""
    return await ports.listening_of_jobs(ids)


@app.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str) -> dict[str, bool]:
    return {"stopped": jobs.stop(job_id)}


class RunNotice(BaseModel):
    text: str
    urgent: bool = False


@app.post("/runs/{run_id}/notify")
async def notify_run(run_id: str, body: RunNotice) -> dict[str, bool]:
    if run_id not in _active:
        return {"delivered": False}
    notices.push(run_id, body.text, body.urgent)
    return {"delivered": True}


@app.post("/providers/{name}/limits", response_model=LimitsEvent)
async def limits(name: str, credentials: Credentials) -> LimitsEvent:
    """The plan limits of the account in ``credentials``; no windows if they can't be read."""
    provider = _signin_provider(name)
    try:
        windows = await provider.limits(credentials)
    except Exception as exc:
        log.warning("No limits from %s: %s", provider.name, exc)
        return LimitsEvent()
    try:
        plan, size = await provider.plan(credentials)
    except Exception as exc:
        log.info("No plan from %s: %s", provider.name, str(exc)[:200])
        plan, size = None, None
    return LimitsEvent(windows=windows, complete=provider.limits_complete, plan=plan, plan_size=size)


@app.post("/providers/{name}/models", response_model=list[str])
async def provider_models(name: str, credentials: Credentials) -> list[str]:
    """What the endpoint of the account in ``credentials`` offers."""
    try:
        return await get_provider(name).models(credentials)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, str(exc)[:1000]) from exc


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
    jq = f".[] | {REPO_FIELDS}"
    path = "user/repos?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member"
    try:
        out = await run_cmd(["gh", "api", "--paginate", path, "--jq", jq], cwd=None, env=git_env(credentials), timeout=90)
    except WorkspaceError as exc:
        return GitHubReposResult(ok=False, detail=str(exc)[:400])
    repos = [GitHubRepo.model_validate_json(line) for line in out.splitlines() if line.strip()]
    return GitHubReposResult(ok=True, repos=repos)


REPO_FIELDS = "{full_name, name, clone_url, description, default_branch, private}"


class NewGitHubRepo(BaseModel):
    credentials: Credentials
    name: str = Field(pattern=r"^[\w.-]{1,100}$")
    owner: str = Field(default="", pattern=r"^[\w.-]{0,100}$")  # an organisation. Empty: the token's own account
    private: bool = True
    description: str = ""


@app.post("/github/repos/create", response_model=GitHubReposResult)
async def github_create_repo(body: NewGitHubRepo) -> GitHubReposResult:
    """A new repository on GitHub, with a first commit so it has a branch agents can start from."""
    if not body.credentials.github_token:
        return GitHubReposResult(ok=False, detail="No GitHub token configured")
    args = ["gh", "api", "-X", "POST", f"orgs/{body.owner}/repos" if body.owner else "user/repos"]
    args += ["-f", f"name={body.name}", "-f", f"description={body.description}", "-F", f"private={str(body.private).lower()}", "-F", "auto_init=true"]
    try:
        out = await run_cmd([*args, "--jq", REPO_FIELDS], cwd=None, env=git_env(body.credentials), timeout=60)
    except WorkspaceError as exc:
        return GitHubReposResult(ok=False, detail=str(exc)[-400:])
    return GitHubReposResult(ok=True, repos=[GitHubRepo.model_validate_json(out.splitlines()[-1])])


@app.post("/github/pull-request", response_model=PullRequestState)
async def github_pull_request(ref: PullRequestRef) -> PullRequestState:
    """State and checks of a pull request, for the app's watcher."""
    try:
        return await github.pull_request(ref.credentials, ref.repo, ref.number)
    except (WorkspaceError, ValueError) as exc:
        raise HTTPException(502, str(exc)[:600]) from exc


@app.post("/github/commit-checks")
async def github_commit_checks(ref: CommitRef) -> list[PullRequestCheck]:
    """The checks of a commit on a branch, for a base branch that is checked after branches landed on it."""
    try:
        return await github.commit_checks(ref.credentials, ref.repo, ref.sha)
    except (WorkspaceError, ValueError) as exc:
        raise HTTPException(502, str(exc)[:600]) from exc


@app.post("/github/rerun-failed")
async def github_rerun_failed(ref: RerunRef) -> dict:
    """Run the failed jobs of these workflow runs once more: is a red check on the base branch only flaky?"""
    return {"rerun": await github.rerun_failed(ref.credentials, ref.repo, ref.run_ids)}


class MergeRequest(PullRequestRef):
    methods: list[str] = []  # merge methods to try, in this order; empty: the default order
    # The branches a train's pull request brought along: removed once they are in the base branch.
    key: str | None = None
    remote: RepoSpec | None = None
    delete_branches: dict[str, str] = {}  # branch -> the commit that landed
    # The train's checked commit, its base and its branch: the base branch is moved to exactly that
    # commit if it can be (a fast-forward), before the pull request is merged in any other way.
    base: str = ""
    head_sha: str = ""
    branch: str = ""
    restack: list[str] = []  # branches that may build on the train: moved onto the base branch after a squash


@app.post("/github/pull-request/merge", response_model=CheckResult)
async def github_merge(ref: MergeRequest) -> CheckResult:
    """Land a train's pull request. First the checked commit itself, pushed to the base branch (a fast-forward,
    so every commit stays as it was: branches built on them go on as they are). Refused when the base branch
    moved or its protection forbids it: then the pull request is merged with a method the repository allows."""
    pushed = None
    if ref.key and ref.remote and ref.base and ref.head_sha:
        pushed = await trains.land(TrainLandSpec(credentials=ref.credentials, key=ref.key, remote=ref.remote, base=ref.base, head_sha=ref.head_sha, delete_branches=ref.delete_branches))
        if pushed.ok:
            if ref.branch and await github.merged_soon(ref.credentials, ref.repo, ref.number):
                await trains.delete_branches(ref.credentials, ref.remote, ref.key, {ref.branch: ref.head_sha})
            return CheckResult(ok=True, detail="fast-forward")
    result = await github.merge(ref.credentials, ref.repo, ref.number, ref.methods)
    if result.ok and ref.key and ref.remote and ref.delete_branches:
        await trains.delete_branches(ref.credentials, ref.remote, ref.key, ref.delete_branches)
    if result.ok and result.detail == "squash" and ref.key and ref.remote and ref.base and ref.head_sha and ref.restack:
        moved = await trains.restack(ref.credentials, ref.remote, ref.key, ref.base, ref.head_sha, ref.restack)
        if moved:
            result.detail += "; moved onto it: " + ", ".join(moved)
    if not result.ok and pushed is not None:
        result.detail = f"push of the checked commit: {pushed.detail[-250:]} | merge: {result.detail[-300:]}"
    return result


@app.post("/github/pull-request/close")
async def github_close(ref: PullRequestRef) -> dict[str, bool]:
    return {"closed": await github.close(ref.credentials, ref.repo, ref.number)}


@app.post("/trains", response_model=TrainResult)
async def assemble_train(spec: TrainSpec) -> TrainResult:
    """Merge finished branches into one, so one check run covers all of them. With ``github`` the
    train also gets a pull request, whose checks are that run."""
    try:
        result = await trains.assemble(spec)
        if spec.github and result.merged:
            title = spec.title or f"Land {len(result.merged)} branches"
            result.number, result.url = await github.open_pull_request(spec.credentials, spec.github, result.base, spec.branch, title, spec.body)
        return result
    except (WorkspaceError, ValueError, OSError) as exc:
        raise HTTPException(502, str(exc)[:600]) from exc


@app.post("/git/tidy", response_model=TidyResult)
async def tidy_branches(spec: TidySpec) -> TidyResult:
    """Remove the branches that are fully merged into the base branch."""
    try:
        return await trains.tidy(spec)
    except (WorkspaceError, ValueError, OSError) as exc:
        raise HTTPException(502, str(exc)[:600]) from exc


@app.post("/trains/check", response_model=JobState)
async def check_train(spec: TrainCheckSpec) -> JobState:
    try:
        return await trains.start_check(spec)
    except (WorkspaceError, ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)[:500]) from exc


@app.post("/trains/land", response_model=CheckResult)
async def land_train(spec: TrainLandSpec) -> CheckResult:
    return await trains.land(spec)


@app.post("/github/upstream")
async def github_upstream(ref: UpstreamRef) -> UpstreamState | None:
    try:
        return await github.upstream(ref.credentials, ref.repository, ref.branch, ref.commit)
    except WorkspaceError as exc:
        raise HTTPException(502, str(exc)[:600]) from exc


@app.post("/git/branch")
async def git_branch(ref: BranchRef) -> dict[str, str | None]:
    try:
        return {"sha": await trains.branch_head(ref.credentials, ref.remote, ref.branch)}
    except WorkspaceError as exc:
        raise HTTPException(502, str(exc)[:600]) from exc


@app.post("/files/remove", response_model=CheckResult)
async def remove_files(body: ProjectFiles) -> CheckResult:
    """What a deleted project leaves on this machine: its agents' folders, and the clones its trains were built in."""
    folders = [trains.folder(key) for key in body.trains]
    try:
        if body.workspace:
            folders.append(project_folder(body.workspace))
        removed = await asyncio.to_thread(lambda: sum(remove_folder(folder) for folder in folders))
    except (WorkspaceError, OSError) as exc:
        return CheckResult(ok=False, detail=str(exc)[:400])
    return CheckResult(ok=True, detail=f"{removed} folder{'' if removed == 1 else 's'} removed")


class GitHubCheck(BaseModel):
    credentials: Credentials
    repo_urls: list[str] = []
    repos: list[RepoSpec] = []  # with their own access (token or SSH key), if they have one


@app.post("/checks/github", response_model=CheckResult)
async def check_github(body: GitHubCheck) -> CheckResult:
    """Verify the token (if any) and that every repository is reachable with the access it would get."""
    repos = body.repos or [RepoSpec(name=url, url=url) for url in body.repo_urls]
    env = git_env(body.credentials)
    parts: list[str] = []
    ok = True
    if body.credentials.github_token:
        try:
            answer = await run_cmd(["gh", "api", "-i", "user"], cwd=None, env=env, timeout=30)
        except WorkspaceError as exc:
            return CheckResult(ok=False, detail=f"Token rejected: {str(exc)[:300]}")
        try:
            login, scopes = github.token_summary(answer)
        except (ValueError, KeyError):
            parts.append("Token accepted")
        else:
            if scopes is None:
                parts.append(f"Authenticated as {login} (fine-grained token: it reaches the repositories chosen for it)")
            elif not {"repo", "public_repo"} & set(scopes):
                return CheckResult(ok=False, detail=f"Authenticated as {login}, but the token lacks the scope 'repo': it cannot push or open pull requests")
            else:
                parts.append(f"Authenticated as {login} (scopes: {', '.join(scopes)})")
    elif not repos:
        return CheckResult(ok=False, detail="No GitHub token configured")
    for repo in repos:
        try:
            await run_cmd(["git", *config_args(repo, body.credentials), "ls-remote", "--heads", repo.url], cwd=None, env=env, timeout=60)
        except WorkspaceError:
            ok = False
            parts.append(f"not reachable: {repo.url}")
    if repos and ok:
        parts.append(f"{len(repos)} repositor{'y' if len(repos) == 1 else 'ies'} reachable")
    return CheckResult(ok=ok, detail="; ".join(parts))
