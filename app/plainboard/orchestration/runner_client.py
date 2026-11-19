"""HTTP client for the runner service."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

import httpx
from plainboard_protocol import ProjectFiles, TidyResult, TidySpec, CheckResult, CleanupResult, CleanupSpec, CompletionResult, CompletionSpec, Credentials, JobPort, JobSpec, JobState, LimitsEvent, MachineUsage, ProviderAccount, ProviderInfo, PullRequestCheck, PullRequestState, RepoSpec, RunEvent, RunSpec, TrainCheckSpec, TrainLandSpec, TrainResult, TrainSpec, UpstreamState, parse_event

log = logging.getLogger(__name__)


class RunnerError(RuntimeError):
    pass


class RunnerUnavailable(RunnerError):
    """The runner could not be reached at all: nothing was started there. (After an update the app
    is up a moment before the runner is.)"""


class RunnerClient:
    def __init__(self, base_url: str):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(60, connect=10))
        self._providers: tuple[float, list[ProviderInfo]] | None = None

    async def stream_run(self, spec: RunSpec) -> AsyncIterator[RunEvent]:
        timeout = httpx.Timeout(None, connect=10)  # agent turns can take a long time
        try:
            async with self._client.stream("POST", "/runs", json=spec.model_dump(mode="json"), timeout=timeout) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise RunnerError(f"Runner rejected run ({resp.status_code}): {body[:500]}")
                async for line in resp.aiter_lines():
                    if line.strip():
                        yield parse_event(line)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise RunnerUnavailable(f"Runner unreachable: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RunnerError(f"Runner unreachable: {exc}") from exc

    async def cancel(self, run_id: int) -> None:
        try:
            await self._client.post(f"/runs/{run_id}/cancel")
        except httpx.HTTPError:
            pass

    async def active_runs(self) -> set[int]:
        """The turns the runner is running right now."""
        try:
            resp = await self._client.get("/runs", timeout=10)
            resp.raise_for_status()
            return {int(run_id) for run_id in resp.json()}
        except (httpx.HTTPError, ValueError) as exc:
            raise RunnerError(f"Runner unreachable: {exc}") from exc

    async def _post_job(self, path: str, body: object) -> httpx.Response:
        try:
            resp = await self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise RunnerError(f"Runner unreachable: {exc}") from exc
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
            raise RunnerError(f"Runner unreachable: {exc}") from exc
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
            raise RunnerError(f"Runner unreachable: {exc}") from exc
        providers = [ProviderInfo.model_validate(p) for p in resp.json()]
        self._providers = (time.monotonic(), providers)
        return providers

    async def provider_account(self, provider: str, key: str, action: str | None = None) -> ProviderAccount:
        """The subscription of account ``key`` signed in on the runner; ``action`` "login" or "logout" changes it first."""
        try:
            if action:
                resp = await self._client.post(f"/providers/{provider}/{action}", params={"key": key}, timeout=60)
            else:
                resp = await self._client.get(f"/providers/{provider}/account", params={"key": key}, timeout=60)
        except httpx.HTTPError as exc:
            raise RunnerError(f"Runner unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.json().get("detail", resp.text)[:1000] if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:1000])
        return ProviderAccount.model_validate(resp.json())

    async def provider_models(self, provider: str, credentials: Credentials) -> list[str]:
        """The models the account in ``credentials`` is offered by its endpoint."""
        try:
            resp = await self._client.post(f"/providers/{provider}/models", json=credentials.model_dump(mode="json"), timeout=40)
        except httpx.HTTPError as exc:
            raise RunnerError(f"Runner unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.json().get("detail", resp.text)[:1000] if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:1000])
        return [str(m) for m in resp.json()]

    async def check_provider(self, spec: CompletionSpec) -> CheckResult:
        try:
            resp = await self._client.post("/checks/provider", json=spec.model_dump(mode="json"), timeout=180)
            return CheckResult.model_validate(resp.json())
        except httpx.HTTPError as exc:
            return CheckResult(ok=False, detail=f"Runner unreachable: {exc}")

    async def check_github(self, credentials: Credentials, repos: list[RepoSpec] | None = None) -> CheckResult:
        try:
            resp = await self._client.post(
                "/checks/github", json={"credentials": credentials.model_dump(mode="json"), "repos": [r.model_dump(mode="json") for r in repos or []]}, timeout=180
            )
            return CheckResult.model_validate(resp.json())
        except httpx.HTTPError as exc:
            return CheckResult(ok=False, detail=f"Runner unreachable: {exc}")

    async def github_create_repo(self, credentials: Credentials, name: str, owner: str = "", private: bool = True) -> dict:
        body = {"credentials": credentials.model_dump(mode="json"), "name": name, "owner": owner, "private": private}
        try:
            resp = await self._client.post("/github/repos/create", json=body, timeout=90)
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": f"Runner unreachable: {exc}", "repos": []}
        if resp.status_code >= 400:
            return {"ok": False, "detail": resp.text[:300], "repos": []}
        return resp.json()

    async def tidy_branches(self, spec: TidySpec) -> TidyResult:
        try:
            resp = await self._client.post("/git/tidy", json=spec.model_dump(mode="json"), timeout=600)
        except httpx.HTTPError as exc:
            raise RunnerError(f"Runner unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise RunnerError(resp.text[:600])
        return TidyResult.model_validate(resp.json())

    async def usage(self, measure: bool = False) -> MachineUsage:
        """Disk and memory of the runner's machine; ``measure``: measure the folders again, in the background."""
        try:
            resp = await (self._client.post("/usage/measure", timeout=30) if measure else self._client.get("/usage", timeout=30))
            resp.raise_for_status()
            return MachineUsage.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise RunnerError(f"Runner unreachable: {exc}") from exc

    async def clean_up(self, spec: CleanupSpec) -> CleanupResult:
        """Free room on the workspaces' volume; can take long (sizes, deleting big folders)."""
        try:
            resp = await self._client.post("/cleanup", json=spec.model_dump(mode="json"), timeout=3 * 3600)
            resp.raise_for_status()
            return CleanupResult.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise RunnerError(f"Runner unreachable: {exc}") from exc

    async def remove_files(self, spec: ProjectFiles) -> CheckResult:
        try:
            resp = await self._client.post("/files/remove", json=spec.model_dump(mode="json"), timeout=300)
        except httpx.HTTPError as exc:
            return CheckResult(ok=False, detail=f"Runner unreachable: {exc}")
        if resp.status_code >= 400:
            return CheckResult(ok=False, detail=resp.text[:300])
        return CheckResult.model_validate(resp.json())

    async def github_repos(self, credentials: Credentials) -> dict:
        try:
            resp = await self._client.post("/github/repos", json=credentials.model_dump(mode="json"), timeout=120)
            return resp.json()
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": f"Runner unreachable: {exc}", "repos": []}

    async def _github(self, path: str, body: dict, timeout: float) -> httpx.Response:
        try:
            resp = await self._client.post(path, json=body, timeout=timeout)
        except httpx.HTTPError as exc:
            raise RunnerError(f"Runner unreachable: {exc}") from exc
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

    async def merge_pull_request(
        self, credentials: Credentials, repo: str, number: int, *, methods: list[str] | None = None, key: str | None = None, remote: RepoSpec | None = None, delete_branches: dict[str, str] | None = None
    ) -> CheckResult:
        body = {
            "credentials": credentials.model_dump(mode="json"),
            "repo": repo,
            "number": number,
            "methods": methods or [],
            "key": key,
            "remote": remote.model_dump(mode="json") if remote else None,
            "delete_branches": delete_branches or {},
        }
        try:
            resp = await self._client.post("/github/pull-request/merge", json=body, timeout=300)
            return CheckResult.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            return CheckResult(ok=False, detail=f"Runner unreachable: {exc}")

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
            return CheckResult(ok=False, detail=f"Runner unreachable: {exc}")

    async def branch_head(self, credentials: Credentials, remote: RepoSpec, branch: str) -> str | None:
        """The commit a branch points to on its remote; None if the branch is not there."""
        body = {"credentials": credentials.model_dump(mode="json"), "remote": remote.model_dump(mode="json"), "branch": branch}
        return (await self._github("/git/branch", body, 150)).json().get("sha")

    async def upstream(self, credentials: Credentials, repository: str, branch: str, commit: str) -> UpstreamState | None:
        """How far ``commit`` is behind PlainBoard's own upstream branch; None if GitHub does not know it there."""
        body = {"credentials": credentials.model_dump(mode="json"), "repository": repository, "branch": branch, "commit": commit}
        data = (await self._github("/github/upstream", body, 60)).json()
        return UpstreamState.model_validate(data) if data else None

    def browser_url(self, key: str) -> str:
        """The socket of the runner's browser for ``key`` (see the runner's browser.py)."""
        base = self._client.base_url
        return f"{'wss' if base.scheme == 'https' else 'ws'}://{base.netloc.decode()}/browser/{key}"

    async def aclose(self) -> None:
        await self._client.aclose()
