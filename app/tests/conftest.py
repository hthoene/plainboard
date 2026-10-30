from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from plainboard_protocol import (
    LimitsEvent,
    LimitWindow,
    AssistantTextEvent,
    CheckResult,
    CompactionEvent,
    CompletionResult,
    CompletionSpec,
    JobSpec,
    JobState,
    ModelInfo,
    ProviderInfo,
    ResultEvent,
    RunEvent,
    RunSpec,
    SessionEvent,
    ToolResultEvent,
    ToolUseEvent,
    UsageEvent,
)

from plainboard.orchestration.runner_client import RunnerError


class FakeRunner:
    """Stands in for the runner service. Understands the same directives as the mock provider:
    ``TOOL <name> <json>`` and ``SAY <text>``; ``CONTEXT <n>`` sets the reported context size,
    ``COMPACT <n>`` reports a compaction by the provider."""

    app = None  # set by fixture; used to execute tools through the real RunManager

    def __init__(self, base_url: str):
        self.specs: list[RunSpec] = []
        self.completions: list[CompletionSpec] = []
        self.notices: list[tuple[int, str]] = []
        self.limit_windows: list[LimitWindow] = []
        self.jobs: dict[str, JobState] = {}
        self.job_specs: list[JobSpec] = []
        self.reachable = True

    async def stream_run(self, spec: RunSpec) -> AsyncIterator[RunEvent]:
        self.specs.append(spec)
        runs = self.app.state.runs
        ctx = runs.context_for_token(spec.bridge.run_token, int(spec.run_id))
        session_id = spec.resume_session_id or f"session-{spec.run_id}"
        yield SessionEvent(session_id=session_id)
        said, context = [], 1000
        for i, line in enumerate(spec.prompt.splitlines()):
            line = line.strip()
            if line.startswith("TOOL "):
                _, name, *rest = line.split(" ", 2)
                args = json.loads(rest[0]) if rest else {}
                yield ToolUseEvent(id=f"t{i}", name=name, input=args)
                result = await runs.execute_tool(ctx, name, args)
                yield ToolResultEvent(tool_use_id=f"t{i}", content=result.text, is_error=result.is_error)
                said.append(result.text)
            elif line.startswith("SAY "):
                said.append(line[4:])
            elif line.startswith("CONTEXT "):
                context = int(line.split()[1])
            elif line.startswith("LIMITS "):
                _, name, used, resets_in = line.split()
                yield LimitsEvent(windows=[LimitWindow(name=name, utilization=float(used), resets_at=int(time.time()) + int(resets_in))])
            elif line.startswith("COMPACT "):
                yield CompactionEvent(tokens_before=int(line.split()[1]))
        text = " | ".join(said) or "ok"
        yield AssistantTextEvent(text=text)
        yield UsageEvent(context_tokens=context)
        yield ResultEvent(session_id=session_id, text=text, num_turns=1, input_tokens=10, output_tokens=5, cost_usd=0.001, context_tokens=context)

    async def complete(self, spec: CompletionSpec) -> CompletionResult:
        self.completions.append(spec)
        return CompletionResult(text=f"SUMMARY #{len(self.completions)}", input_tokens=100, output_tokens=10, cost_usd=0.0001)

    async def cancel(self, run_id: int) -> None:
        pass

    async def limits(self, credentials) -> list[LimitWindow]:
        return self.limit_windows

    async def notify_run(self, run_id: int, text: str) -> bool:
        self.notices.append((run_id, text))
        return True

    async def start_job(self, spec: JobSpec) -> JobState:
        self.job_specs.append(spec)
        self.jobs[spec.id] = JobState(running=True, log_path=f"/jobs/{spec.id}.log")
        return self.jobs[spec.id]

    async def job_status(self, ids: list[str]) -> dict[str, JobState]:
        if not self.reachable:
            raise RunnerError("Runner unreachable")
        return {i: self.jobs[i] for i in ids if i in self.jobs}

    async def stop_job(self, runner_id: str) -> bool:
        job = self.jobs.get(runner_id)
        return bool(job and job.running)

    async def providers(self) -> list[ProviderInfo]:
        return [ProviderInfo(name="claude_code", label="Claude Code", models=[ModelInfo(id="claude-sonnet-5-5", label="Sonnet")])]

    async def check_provider(self, spec):
        return CheckResult(ok=True, detail="fake")

    async def check_github(self, credentials, repo_url):
        return CheckResult(ok=bool(credentials.github_token), detail="fake")

    async def github_repos(self, credentials):
        self.github_token_used = credentials.github_token
        if not credentials.github_token:
            return {"ok": False, "detail": "No GitHub token configured", "repos": []}
        return {"ok": True, "detail": "", "repos": [{"full_name": "acme/api", "name": "api", "clone_url": "https://github.com/acme/api.git"}]}

    async def aclose(self) -> None:
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAINBOARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PLAINBOARD_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("PLAINBOARD_STATIC_DIR", str(tmp_path / "static"))
    monkeypatch.setenv("PLAINBOARD_SCHEDULER_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("PLAINBOARD_JOB_WATCH_INTERVAL_SECONDS", "3600")
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "PLAINBOARD_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    from plainboard import config, main

    config.get_config.cache_clear()
    monkeypatch.setattr(main, "RunnerClient", FakeRunner)
    app = main.create_app()
    FakeRunner.app = app
    with TestClient(app) as test_client:
        test_client.app_ref = app
        yield test_client
    config.get_config.cache_clear()


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


@pytest.fixture
def project(client):
    resp = client.post("/api/projects", json={"name": "Demo Project", "description": "A test project"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def agents_by_slug(client, project_id: int) -> dict:
    return {a["slug"]: a for a in client.get(f"/api/projects/{project_id}/agents").json()}


def runs_of(client, project_id: int, **params) -> list[dict]:
    return client.get(f"/api/projects/{project_id}/runs", params=params).json()


def idle(client, project_id: int) -> bool:
    return not runs_of(client, project_id, active=True)
