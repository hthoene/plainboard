"""HTTP client for the runner service."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import httpx
from plainboard_protocol import CheckResult, CompletionResult, CompletionSpec, Credentials, ProviderInfo, RunEvent, RunSpec, parse_event


class RunnerError(RuntimeError):
    pass


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
        except httpx.HTTPError as exc:
            raise RunnerError(f"Runner unreachable: {exc}") from exc

    async def cancel(self, run_id: int) -> None:
        try:
            await self._client.post(f"/runs/{run_id}/cancel")
        except httpx.HTTPError:
            pass

    async def complete(self, spec: CompletionSpec) -> CompletionResult:
        try:
            resp = await self._client.post("/complete", json=spec.model_dump(mode="json"), timeout=600)
        except httpx.HTTPError as exc:
            raise RunnerError(f"Runner unreachable: {exc}") from exc
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

    async def check_provider(self, spec: CompletionSpec) -> CheckResult:
        try:
            resp = await self._client.post("/checks/provider", json=spec.model_dump(mode="json"), timeout=180)
            return CheckResult.model_validate(resp.json())
        except httpx.HTTPError as exc:
            return CheckResult(ok=False, detail=f"Runner unreachable: {exc}")

    async def check_github(self, credentials: Credentials, repo_urls: list[str] | None = None) -> CheckResult:
        try:
            resp = await self._client.post(
                "/checks/github", json={"credentials": credentials.model_dump(mode="json"), "repo_urls": repo_urls or []}, timeout=180
            )
            return CheckResult.model_validate(resp.json())
        except httpx.HTTPError as exc:
            return CheckResult(ok=False, detail=f"Runner unreachable: {exc}")

    async def github_repos(self, credentials: Credentials) -> dict:
        try:
            resp = await self._client.post("/github/repos", json=credentials.model_dump(mode="json"), timeout=120)
            return resp.json()
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": f"Runner unreachable: {exc}", "repos": []}

    async def aclose(self) -> None:
        await self._client.aclose()
