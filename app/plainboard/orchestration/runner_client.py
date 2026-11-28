"""HTTP client for the runner service."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

import httpx
from plainboard_protocol import GitLogResult, GitLogSpec, GitPresentResult, GitPresentSpec, GitTagsResult, GitTagsSpec, PromotePlan, PromotePushSpec, PromoteResult, PromoteSpec, ProjectFiles, TidyResult, TidySpec, CandidatesSpec, CheckResult, CleanupCandidate, CleanupResult, CleanupSpec, CompletionResult, CompletionSpec, Credentials, DeleteSpec, JobPort, JobSpec, JobState, LeftoverResult, LeftoverSpec, JobTiming, LimitsEvent, MachineUsage, ProviderAccount, ProviderInfo, SignInAck, SignInFiles, SignInRefresh, PullRequestCheck, PullRequestState, RepoSpec, RunEvent, RunSpec, SkillFetch, TrainCheckSpec, TrainLandSpec, TrainResult, TrainSpec, TrimSpec, UnpushedSpec, UnpushedWork, UpstreamState, parse_event

from ..services.errors import ServiceError

log = logging.getLogger(__name__)


RESUME_SECONDS = 180.0  # how long a turn's stream waits for its external runner to be back
RESUME_RETRY_SECONDS = 1.0


class RunnerError(RuntimeError):
    pass


class RunnerUnavailable(RunnerError):
    """The runner could not be reached at all: nothing was started there. (After an update the app
    is up a moment before the runner is.)"""


class RunnerStreamBroken(RunnerError):
    """The connection of a turn's stream broke without the runner saying why. ``events``: how many it
    had sent until then; ``lost``: an external runner no longer has the turn (it restarted, e.g. to
    update itself, or stayed away too long), so nothing of it runs any more."""

    def __init__(self, message: str, events: int, lost: bool = False):
        super().__init__(message)
        self.events = events
        self.lost = lost


class UpdateRefused(RunnerError):
    """The runner can't take this version by itself (it needs a new image, or can't update itself at all)."""


def unreachable(exc: BaseException) -> str:
    """``Runner unreachable: ExceptionType: text``; some httpx errors (a reset connection) have no text at all."""
    text = str(exc).strip()
    return f"Runner unreachable: {type(exc).__name__}: {text}" if text else f"Runner unreachable: {type(exc).__name__}"


class RunnerClient:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        """``transport``: an external runner's tunnel (orchestration/tunnel.py) instead of the network."""
        # No connection is kept for the next request: uvicorn closes an idle one after a few seconds, and a
        # request that reuses it just then (likely on a loaded host) is reset with no message.
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(60, connect=10), limits=httpx.Limits(max_keepalive_connections=0), transport=transport
        )
        self._providers: tuple[float, list[ProviderInfo]] | None = None
        self.resumable = transport is not None  # an external runner: a broken turn stream is picked up again

    async def stream_run(self, spec: RunSpec) -> AsyncIterator[RunEvent]:
        timeout = httpx.Timeout(None, connect=10)  # agent turns can take a long time
        events = 0
        method, path, body = "POST", "/runs", spec.model_dump(mode="json")
        resume_until: float | None = None
        while True:
            try:
                async with self._client.stream(method, path, json=body, timeout=timeout) as resp:
                    if method == "GET" and resp.status_code in (404, 410):
                        # Gone: the runner was started anew, or it could not keep what was missed.
                        if events == 0 and resp.status_code == 404:
                            raise RunnerUnavailable("Runner unreachable: the turn never started there")
                        raise RunnerStreamBroken(f"The runner no longer has this turn ({resp.status_code}): it was started anew", events, lost=True)
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode(errors="replace")
                        raise RunnerError(f"Runner rejected run ({resp.status_code}): {text[:500]}")
                    resume_until = None
                    async for line in resp.aiter_lines():
                        if line.strip():
                            events += 1
                            yield parse_event(line)
                return
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if method == "POST":
                    raise RunnerUnavailable(unreachable(exc)) from exc
                error = exc  # the line is still away: tried again below
            except httpx.HTTPError as exc:
                log.warning("The stream of run %s broke after %s events: %r", spec.run_id, events, exc)
                error = exc
            if not self.resumable:
                raise RunnerStreamBroken(unreachable(error), events) from error
            # An external runner keeps the turn running through a broken line and has its events since:
            # the stream goes on from the last line read here, once the runner is connected again.
            now = time.monotonic()
            resume_until = resume_until or now + RESUME_SECONDS
            if now >= resume_until:
                raise RunnerStreamBroken(unreachable(error), events, lost=True) from error
            await asyncio.sleep(RESUME_RETRY_SECONDS)
            method, path, body = "GET", f"/runs/{spec.run_id}/events?after={events}", None

    async def cancel(self, run_id: int) -> None:
        try:
            await self._client.post(f"/runs/{run_id}/cancel")
        except httpx.HTTPError:
            pass

    async def stage_update(self, data: bytes, sha256: str, commit: str, timeout: float = 90) -> None:
        """An external runner's self-update (services/runner_update.py): it checks and unpacks the code.
        UpdateRefused: it can't take it; RunnerError: it did not arrive whole or the line broke."""
        try:
            resp = await self._client.post("/update", content=data, headers={"x-plainboard-sha256": sha256, "x-plainboard-commit": commit}, timeout=timeout)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code in (404, 405):
            raise UpdateRefused("its image is from before runners could update themselves")
        if resp.status_code >= 400:
            try:
                detail = str(resp.json().get("detail"))
            except (ValueError, AttributeError):
                detail = resp.text
            raise (UpdateRefused if resp.status_code == 409 else RunnerError)(detail[:300])

    async def restart_update(self, commit: str) -> None:
        """The staged update starts: the runner restarts."""
        try:
            resp = await self._client.post("/update/restart", json={"commit": commit}, timeout=30)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.text[:300])

    async def active_runs(self) -> set[int]:
        """The turns the runner is running right now."""
        try:
            resp = await self._client.get("/runs", timeout=10)
            resp.raise_for_status()
            return {int(run_id) for run_id in resp.json()}
        except (httpx.HTTPError, ValueError) as exc:
            raise RunnerError(unreachable(exc)) from exc

    async def _post_job(self, path: str, body: object) -> httpx.Response:
        try:
            resp = await self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.text[:500])
        return resp

    async def start_job(self, spec: JobSpec) -> JobState:
        return JobState.model_validate((await self._post_job("/jobs", spec.model_dump(mode="json"))).json())

    async def job_status(self, ids: list[str]) -> dict[str, JobState]:
        """States of the jobs the runner knows; a missing id means it restarted in the meantime."""
        return {k: JobState.model_validate(v) for k, v in (await self._post_job("/jobs/status", ids)).json().items()}

    async def job_ports(self, ids: list[str]) -> dict[str, list[JobPort]]:
        """The web servers running jobs have started: the runner's job id -> its ports."""
        found = (await self._post_job("/jobs/ports", ids)).json()
        return {job_id: [JobPort.model_validate(port) for port in ports] for job_id, ports in found.items()}

    async def job_listening(self, ids: list[str]) -> dict[str, list[int]]:
        """Every port these jobs listen on, web server or not (who may open a preview): job id -> ports."""
        return (await self._post_job("/jobs/listening", ids)).json()

    async def stop_job(self, runner_id: str) -> bool:
        return (await self._post_job(f"/jobs/{runner_id}/stop", {})).json()["stopped"]

    async def leftovers(self, spec: LeftoverSpec) -> LeftoverResult:
        """What agents left running outside their jobs (processes, containers); stopped with ``spec.stop``."""
        try:
            resp = await self._client.post("/leftovers", json=spec.model_dump(mode="json"), timeout=60)
            resp.raise_for_status()
            return LeftoverResult.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise RunnerError(unreachable(exc)) from exc

    async def notify_run(self, run_id: int, text: str, urgent: bool = False) -> bool:
        """Give a running turn a message it sees after the agent's current step. If urgent, a long step gets interrupted."""
        try:
            resp = await self._client.post(f"/runs/{run_id}/notify", json={"text": text, "urgent": urgent})
            return bool(resp.json().get("delivered"))
        except (httpx.HTTPError, ValueError):
            return False

    async def limits(self, provider: str, credentials: Credentials) -> LimitsEvent:
        """The plan limits of the account in ``credentials``; no windows if they can't be read right now."""
        try:
            resp = await self._client.post(f"/providers/{provider}/limits", json=credentials.model_dump(mode="json"), timeout=30)
            return LimitsEvent.model_validate(resp.json())
        except (httpx.HTTPError, ValueError):
            return LimitsEvent()

    async def complete(self, spec: CompletionSpec) -> CompletionResult:
        try:
            resp = await self._client.post("/complete", json=spec.model_dump(mode="json"), timeout=600)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code >= 400 and spec.effort:
            # Not every model takes every effort, and a summary must not fail over it: once more without.
            log.warning("Completion with effort %s failed on %s, trying without: %s", spec.effort, spec.model, resp.text[:300])
            return await self.complete(spec.model_copy(update={"effort": None}))
        if resp.status_code >= 400:
            raise RunnerError(resp.text[:1000])
        return CompletionResult.model_validate(resp.json())

    async def providers(self) -> list[ProviderInfo]:
        if self._providers and time.monotonic() - self._providers[0] < 300:
            return self._providers[1]
        try:
            resp = await self._client.get("/providers", timeout=10)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        providers = [ProviderInfo.model_validate(p) for p in resp.json()]
        self._providers = (time.monotonic(), providers)
        return providers

    async def provider_account(self, provider: str, key: str, action: str | None = None, own: bool = False) -> ProviderAccount:
        """The subscription of account ``key`` signed in on the runner; ``action`` "login" or "logout" changes it first.
        ``own``: a person's own sign-in, made in a private folder there (runner signins.py)."""
        params: dict[str, str] = {"key": key, **({"own": "true"} if own else {})}
        try:
            if action:
                resp = await self._client.post(f"/providers/{provider}/{action}", params=params, timeout=60)
            else:
                resp = await self._client.get(f"/providers/{provider}/account", params=params, timeout=60)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        return ProviderAccount.model_validate(self._answer(resp))

    async def provider_login_code(self, provider: str, key: str, code: str) -> ProviderAccount:
        """The code the provider's page showed after signing in, for an own sign-in in progress."""
        try:
            resp = await self._client.post(f"/providers/{provider}/login/code", params={"key": key}, json={"code": code}, timeout=60)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        return ProviderAccount.model_validate(self._answer(resp))

    @staticmethod
    def _answer(resp: httpx.Response):
        if resp.status_code >= 400:
            raise RunnerError(resp.json().get("detail", resp.text)[:1000] if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:1000])
        return resp.json()

    async def sign_ins(self) -> list[SignInFiles]:
        """People's own sign-ins the runner holds for the app (new, or refreshed by a run); none from a runner before them."""
        try:
            resp = await self._client.get("/signins", timeout=20)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code == 404:
            return []
        return [SignInFiles.model_validate(entry) for entry in self._answer(resp)]

    async def take_over(self, provider: str, key: str) -> SignInFiles:
        """The installation's sign-in of account ``key`` from the runner's home, which it no longer uses then."""
        try:
            resp = await self._client.post(f"/providers/{provider}/takeover", params={"key": key}, timeout=30)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        return SignInFiles.model_validate(self._answer(resp))

    async def refresh_sign_in(self, provider: str, credentials: Credentials) -> None:
        """A kept sign-in refreshed ahead of time if it is due soon; what changed comes with ``sign_ins``."""
        try:
            resp = await self._client.post("/signins/refresh", json=SignInRefresh(provider=provider, credentials=credentials).model_dump(), timeout=90)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.text[:300])

    async def update_sign_ins(self, entries: list[SignInFiles]) -> None:
        """What another runner refreshed, for the accounts in use here."""
        try:
            resp = await self._client.post("/signins/update", json=[e.model_dump() for e in entries], timeout=20)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code >= 400 and resp.status_code != 404:
            raise RunnerError(resp.text[:300])

    async def ack_sign_ins(self, acks: list[SignInAck]) -> None:
        try:
            resp = await self._client.post("/signins/ack", json=[a.model_dump() for a in acks], timeout=20)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.text[:1000])

    async def provider_models(self, provider: str, credentials: Credentials) -> list[str]:
        """The models the account in ``credentials`` is offered by its endpoint."""
        try:
            resp = await self._client.post(f"/providers/{provider}/models", json=credentials.model_dump(mode="json"), timeout=40)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.json().get("detail", resp.text)[:1000] if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:1000])
        return [str(m) for m in resp.json()]

    async def check_provider(self, spec: CompletionSpec) -> CheckResult:
        try:
            resp = await self._client.post("/checks/provider", json=spec.model_dump(mode="json"), timeout=180)
            return CheckResult.model_validate(resp.json())
        except httpx.HTTPError as exc:
            return CheckResult(ok=False, detail=unreachable(exc))

    async def check_github(self, credentials: Credentials, repos: list[RepoSpec] | None = None) -> CheckResult:
        try:
            resp = await self._client.post(
                "/checks/github", json={"credentials": credentials.model_dump(mode="json"), "repos": [r.model_dump(mode="json") for r in repos or []]}, timeout=180
            )
            return CheckResult.model_validate(resp.json())
        except httpx.HTTPError as exc:
            return CheckResult(ok=False, detail=unreachable(exc))

    async def github_create_repo(self, credentials: Credentials, name: str, owner: str = "", private: bool = True) -> dict:
        body = {"credentials": credentials.model_dump(mode="json"), "name": name, "owner": owner, "private": private}
        try:
            resp = await self._client.post("/github/repos/create", json=body, timeout=90)
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": unreachable(exc), "repos": []}
        if resp.status_code >= 400:
            return {"ok": False, "detail": resp.text[:300], "repos": []}
        return resp.json()

    async def fetch_skill(self, spec: SkillFetch) -> bytes:
        """A skill's folder from git as a zip (``SkillFetch``). A failure is the user's to read: a ServiceError."""
        try:
            resp = await self._client.post("/skills/fetch", json=spec.model_dump(mode="json"), timeout=200)
        except httpx.HTTPError as exc:
            raise ServiceError(unreachable(exc), 502) from exc
        if resp.status_code >= 400:
            try:
                detail = str(resp.json().get("detail") or resp.text)
            except ValueError:
                detail = resp.text
            raise ServiceError(detail[:600], 400 if resp.status_code < 500 else 502)
        return resp.content

    async def tidy_branches(self, spec: TidySpec) -> TidyResult:
        try:
            resp = await self._client.post("/git/tidy", json=spec.model_dump(mode="json"), timeout=600)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.text[:600])
        return TidyResult.model_validate(resp.json())

    async def git_log(self, spec: GitLogSpec) -> GitLogResult:
        try:
            resp = await self._client.post("/git/log", json=spec.model_dump(mode="json"), timeout=600)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.text[:600])  # also a runner of a version without it (404)
        return GitLogResult.model_validate(resp.json())

    async def git_tags(self, spec: GitTagsSpec) -> GitTagsResult:
        try:
            resp = await self._client.post("/git/tags", json=spec.model_dump(mode="json"), timeout=600)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.text[:600])  # also a runner of a version without it (404)
        return GitTagsResult.model_validate(resp.json())

    async def git_present(self, spec: GitPresentSpec) -> GitPresentResult:
        try:
            resp = await self._client.post("/git/present", json=spec.model_dump(mode="json"), timeout=20)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.text[:600])
        return GitPresentResult.model_validate(resp.json())

    async def missing_uploads(self, names: list[str]) -> list[str]:
        """Which of these attachment files an external runner does not have (yet)."""
        return [str(n) for n in (await self._post_job("/uploads/missing", names)).json()]

    async def delete_uploads(self, names: list[str]) -> int:
        """Remove these attachment files from an external runner's copies (their retention ended). How many it had."""
        return int((await self._post_job("/uploads/delete", names)).json())

    async def put_upload(self, name: str, content: bytes) -> None:
        try:
            resp = await self._client.put(f"/uploads/{name}", content=content, timeout=300)
        except httpx.HTTPError as exc:
            raise RunnerError(f"Runner unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.text[:300])

    async def usage(self, measure: bool = False) -> MachineUsage:
        """Disk and memory of the runner's machine; ``measure``: measure the folders again, in the background."""
        try:
            resp = await (self._client.post("/usage/measure", timeout=30) if measure else self._client.get("/usage", timeout=30))
            resp.raise_for_status()
            return MachineUsage.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise RunnerError(unreachable(exc)) from exc

    async def clean_up(self, spec: CleanupSpec) -> CleanupResult:
        """Free room on the workspaces' volume; can take long (sizes, deleting big folders)."""
        try:
            resp = await self._client.post("/cleanup", json=spec.model_dump(mode="json"), timeout=3 * 3600)
            resp.raise_for_status()
            return CleanupResult.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise RunnerError(unreachable(exc)) from exc

    async def cleanup_candidates(self, spec: CandidatesSpec) -> list[CleanupCandidate]:
        """What may be deleted, with sizes (measured with du, kept for some minutes) and safety."""
        try:
            resp = await self._client.post("/cleanup/candidates", json=spec.model_dump(mode="json"), timeout=1800)
            resp.raise_for_status()
            return [CleanupCandidate.model_validate(c) for c in resp.json()]
        except (httpx.HTTPError, ValueError) as exc:
            raise RunnerError(unreachable(exc)) from exc

    async def unpushed_work(self, spec: UnpushedSpec) -> list[UnpushedWork]:
        """The clones in these folders with work that is on no remote."""
        try:
            resp = await self._client.post("/workspaces/unpushed", json=spec.model_dump(mode="json"), timeout=120)
            resp.raise_for_status()
            return [UnpushedWork.model_validate(w) for w in resp.json()]
        except (httpx.HTTPError, ValueError) as exc:
            raise RunnerError(unreachable(exc)) from exc

    async def trim_caches(self, spec: TrimSpec) -> CleanupResult:
        """Shrink the big shared tool caches, oldest files first."""
        try:
            resp = await self._client.post("/cleanup/trim", json=spec.model_dump(mode="json"), timeout=3 * 3600)
            resp.raise_for_status()
            return CleanupResult.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise RunnerError(unreachable(exc)) from exc

    async def cleanup_delete(self, spec: DeleteSpec) -> CleanupResult:
        try:
            resp = await self._client.post("/cleanup/delete", json=spec.model_dump(mode="json"), timeout=3 * 3600)
            resp.raise_for_status()
            return CleanupResult.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise RunnerError(unreachable(exc)) from exc

    async def remove_files(self, spec: ProjectFiles) -> CheckResult:
        try:
            resp = await self._client.post("/files/remove", json=spec.model_dump(mode="json"), timeout=300)
        except httpx.HTTPError as exc:
            return CheckResult(ok=False, detail=unreachable(exc))
        if resp.status_code >= 400:
            return CheckResult(ok=False, detail=resp.text[:300])
        return CheckResult.model_validate(resp.json())

    async def github_repos(self, credentials: Credentials) -> dict:
        try:
            resp = await self._client.post("/github/repos", json=credentials.model_dump(mode="json"), timeout=120)
            return resp.json()
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": unreachable(exc), "repos": []}

    async def _github(self, path: str, body: dict, timeout: float) -> httpx.Response:
        try:
            resp = await self._client.post(path, json=body, timeout=timeout)
        except httpx.HTTPError as exc:
            raise RunnerError(unreachable(exc)) from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.text[:600])
        return resp

    async def pull_request(self, credentials: Credentials, repo: str, number: int) -> PullRequestState:
        """State and checks of a GitHub pull request (``repo`` is owner/name)."""
        body = {"credentials": credentials.model_dump(mode="json"), "repo": repo, "number": number}
        return PullRequestState.model_validate((await self._github("/github/pull-request", body, 90)).json())

    async def commit_checks(self, credentials: Credentials, repo: str, sha: str) -> list[PullRequestCheck]:
        """The checks GitHub ran for a commit that was pushed to a branch."""
        body = {"credentials": credentials.model_dump(mode="json"), "repo": repo, "sha": sha}
        return [PullRequestCheck.model_validate(c) for c in (await self._github("/github/commit-checks", body, 120)).json()]

    async def job_timings(self, credentials: Credentials, repo: str, sha: str) -> list[JobTiming]:
        """How long the jobs of Actions took for a commit, the slowest first, with their slowest steps."""
        body = {"credentials": credentials.model_dump(mode="json"), "repo": repo, "sha": sha}
        return [JobTiming.model_validate(j) for j in (await self._github("/github/job-timings", body, 150)).json()]

    async def rerun_failed(self, credentials: Credentials, repo: str, run_ids: list[int]) -> int:
        """Run the failed jobs of these workflow runs of Actions once more; how many were started again."""
        body = {"credentials": credentials.model_dump(mode="json"), "repo": repo, "run_ids": run_ids}
        return int((await self._github("/github/rerun-failed", body, 150)).json().get("rerun", 0))

    async def merge_pull_request(
        self,
        credentials: Credentials,
        repo: str,
        number: int,
        *,
        methods: list[str] | None = None,
        key: str | None = None,
        remote: RepoSpec | None = None,
        delete_branches: dict[str, str] | None = None,
        base: str = "",
        head_sha: str = "",
        branch: str = "",
        restack: list[str] | None = None,
    ) -> CheckResult:
        """Land a train's pull request: its checked commit pushed to ``base`` if that is a fast-forward,
        else merged with the first of ``methods`` the repository allows. After a squash, the ``restack``
        branches that build on the train are moved onto it."""
        body = {
            "credentials": credentials.model_dump(mode="json"),
            "repo": repo,
            "number": number,
            "methods": methods or [],
            "key": key,
            "remote": remote.model_dump(mode="json") if remote else None,
            "delete_branches": delete_branches or {},
            "base": base,
            "head_sha": head_sha,
            "branch": branch,
            "restack": restack or [],
        }
        try:
            resp = await self._client.post("/github/pull-request/merge", json=body, timeout=300)
            return CheckResult.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            return CheckResult(ok=False, detail=unreachable(exc))

    async def close_pull_request(self, credentials: Credentials, repo: str, number: int) -> None:
        try:
            await self._client.post("/github/pull-request/close", json={"credentials": credentials.model_dump(mode="json"), "repo": repo, "number": number}, timeout=90)
        except httpx.HTTPError:
            pass  # tidying up only

    async def assemble_train(self, spec: TrainSpec) -> TrainResult:
        """Merge finished branches into one train (one check run for all of them)."""
        return TrainResult.model_validate((await self._github("/trains", spec.model_dump(mode="json"), 900)).json())

    async def check_train(self, spec: TrainCheckSpec) -> JobState:
        """Start the repository's check command on the assembled train; poll it with ``job_status``."""
        return JobState.model_validate((await self._github("/trains/check", spec.model_dump(mode="json"), 60)).json())

    async def land_train(self, spec: TrainLandSpec) -> CheckResult:
        """Push a checked train to the base branch."""
        try:
            resp = await self._client.post("/trains/land", json=spec.model_dump(mode="json"), timeout=400)
            return CheckResult.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            return CheckResult(ok=False, detail=unreachable(exc))

    async def promotion_plan(self, spec: PromoteSpec) -> PromotePlan:
        """What would go from the base branch to the release branch; with ``checkout``, prepared for its check."""
        return PromotePlan.model_validate((await self._github("/promotions/plan", spec.model_dump(mode="json"), 600)).json())

    async def promotion_push(self, spec: PromotePushSpec) -> PromoteResult:
        """A checked commit of the base branch to the release branch (or a pull request, if the push is refused)."""
        try:
            resp = await self._client.post("/promotions/push", json=spec.model_dump(mode="json"), timeout=600)
            resp.raise_for_status()
            return PromoteResult.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            return PromoteResult(ok=False, detail=unreachable(exc))

    async def branch_head(self, credentials: Credentials, remote: RepoSpec, branch: str) -> str | None:
        """The commit a branch points to on its remote; None if the branch is not there."""
        body = {"credentials": credentials.model_dump(mode="json"), "remote": remote.model_dump(mode="json"), "branch": branch}
        return (await self._github("/git/branch", body, 150)).json().get("sha")

    async def upstream(self, credentials: Credentials, repository: str, branch: str, commit: str, version: str = "") -> UpstreamState | None:
        """How far ``commit`` is behind PlainBoard's own upstream branch, or the release ``version`` behind
        newer releases; None if GitHub does not know it there."""
        body = {"credentials": credentials.model_dump(mode="json"), "repository": repository, "branch": branch, "commit": commit, "version": version}
        data = (await self._github("/github/upstream", body, 60)).json()
        return UpstreamState.model_validate(data) if data else None

    def browser_url(self, key: str) -> str:
        """The socket of the runner's browser for ``key`` (see the runner's browser.py)."""
        base = self._client.base_url
        return f"{'wss' if base.scheme == 'https' else 'ws'}://{base.netloc.decode()}/browser/{key}"

    async def aclose(self) -> None:
        await self._client.aclose()
