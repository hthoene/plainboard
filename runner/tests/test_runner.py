import asyncio
import json

import httpx
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage
from fastapi.testclient import TestClient
from plainboard_protocol import Credentials, JobSpec, McpServerSpec, ToolBridgeSpec, ToolDef, parse_event

from plainboard_runner import config, jobs
from plainboard_runner.main import app
from plainboard_runner.providers.claude_code import EventMapper, auth_env, external_mcp_config
from plainboard_runner.tool_bridge import ToolBridge
from plainboard_runner.workspace import WorkspaceError


@pytest.fixture(autouse=True)
def workspaces(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    monkeypatch.setattr("plainboard_runner.workspace.WORKSPACES_ROOT", tmp_path)
    monkeypatch.setattr("plainboard_runner.workspace._setup_done", set())
    return tmp_path


def run_spec(workspaces, prompt: str, tools=None) -> dict:
    return {
        "run_id": "r1",
        "provider": "mock",
        "model": "mock-large",
        "system_prompt": "You are a test agent.",
        "prompt": prompt,
        "workspace": {"path": str(workspaces / "p" / "a")},
        "credentials": {},
        "bridge": {"base_url": "http://app", "run_id": "r1", "run_token": "t", "tools": tools or []},
    }


def test_providers_lists_builtin():
    names = {p["name"] for p in TestClient(app).get("/providers").json()}
    assert {"claude_code", "mock"} <= names


def test_mock_run_streams_events(workspaces):
    with TestClient(app).stream("POST", "/runs", json=run_spec(workspaces, "SAY hello world")) as resp:
        events = [parse_event(line) for line in resp.iter_lines() if line]
    types = [e.type for e in events]
    assert types[0] == "session"
    assert "assistant_text" in types and types[-1] == "result"
    assert events[-1].text == "hello world"
    assert (workspaces / "p" / "a").is_dir()


def test_workspace_outside_root_is_rejected(workspaces):
    spec = run_spec(workspaces, "SAY x")
    spec["workspace"]["path"] = "/etc"
    with TestClient(app).stream("POST", "/runs", json=spec) as resp:
        events = [parse_event(line) for line in resp.iter_lines() if line]
    assert events[-1].type == "error" and "outside" in events[-1].message


async def test_bridge_proxies_remote_tools(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"text": "ok", "is_error": False})

    spec = ToolBridgeSpec(base_url="http://app", run_id="7", run_token="secret", tools=[ToolDef(name="list_tasks", description="", input_schema={})])
    bridge = ToolBridge(spec, tmp_path)
    bridge._client = httpx.AsyncClient(base_url="http://app", headers={"Authorization": "Bearer secret"}, transport=httpx.MockTransport(handler))
    result = await bridge.call("list_tasks", {"status": "todo"})
    assert result.text == "ok" and not result.is_error
    assert seen == {"path": "/api/internal/runs/7/tools/list_tasks", "auth": "Bearer secret", "body": {"status": "todo"}}
    assert (await bridge.call("nope", {})).is_error


async def test_attach_file_uploads_from_workspace(tmp_path):
    (tmp_path / "report.txt").write_text("hi")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "Attached report.txt to ticket #3.", "is_error": False})

    spec = ToolBridgeSpec(base_url="http://app", run_id="9", run_token="t", tools=[ToolDef(name="attach_file", description="", input_schema={}, local=True)])
    bridge = ToolBridge(spec, tmp_path)
    bridge._client = httpx.AsyncClient(base_url="http://app", transport=httpx.MockTransport(handler))
    result = await bridge.call("attach_file", {"task_number": 3, "path": "report.txt"})
    assert not result.is_error, result.text
    assert seen["path"] == "/api/internal/runs/9/attachments"
    assert b'name="task_number"' in seen["body"] and b"hi" in seen["body"]
    assert (await bridge.call("attach_file", {"task_number": 3, "path": "missing.txt"})).is_error


def _git_repo(path, content: str) -> str:
    import subprocess

    path.mkdir(parents=True)
    run = lambda *a: subprocess.run(a, cwd=path, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    (path / "README.md").write_text(content)
    run("git", "add", ".")
    run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return f"file://{path}"


async def test_workspace_clones_each_repository(workspaces, tmp_path):
    from plainboard_protocol import RepoSpec, WorkspaceSpec

    from plainboard_runner.workspace import prepare_workspace

    front = _git_repo(tmp_path / "src" / "front", "frontend")
    back = _git_repo(tmp_path / "src" / "back", "backend")
    spec = WorkspaceSpec(
        path=str(workspaces / "proj" / "dev"),
        repos=[RepoSpec(name="frontend", url=front), RepoSpec(name="backend", url=back, branch="main"), RepoSpec(name="broken", url="file:///nope")],
    )
    path, problems = await prepare_workspace(spec, Credentials())
    assert (path / "frontend" / "README.md").read_text() == "frontend"
    assert (path / "backend" / "README.md").read_text() == "backend"
    assert len(problems) == 1 and "broken" in problems[0]
    # Second run: existing clones are kept, nothing is re-cloned.
    (path / "frontend" / "local.txt").write_text("work in progress")
    _, problems = await prepare_workspace(spec, Credentials())
    assert (path / "frontend" / "local.txt").exists() and len(problems) == 1


def _git(path, *args: str) -> str:
    import subprocess

    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def _commit(path, content: str) -> None:
    (path / "README.md").write_text(content)
    _git(path, "commit", "-qam", content)


async def _cloned(workspaces, tmp_path, **spec_args):
    from plainboard_protocol import RepoSpec, WorkspaceSpec

    from plainboard_runner.workspace import prepare_workspace

    source = tmp_path / "src" / "repo"
    url = _git_repo(source, "v1")
    spec = WorkspaceSpec(path=str(workspaces / "proj" / "dev"), repos=[RepoSpec(name="repo", url=url)], **spec_args)
    path, _ = await prepare_workspace(spec, Credentials())
    return source, path / "repo", lambda: prepare_workspace(spec, Credentials())


async def test_existing_clone_gets_new_commits_and_branches(workspaces, tmp_path):
    source, clone, prepare = await _cloned(workspaces, tmp_path)
    _commit(source, "v2")
    _git(source, "branch", "feature")
    _, problems = await prepare()
    assert not problems
    assert (clone / "README.md").read_text() == "v2"
    assert _git(clone, "rev-parse", "origin/feature") == _git(source, "rev-parse", "feature")


async def test_update_keeps_local_changes_and_commits(workspaces, tmp_path):
    source, clone, prepare = await _cloned(workspaces, tmp_path)
    (clone / "README.md").write_text("work in progress")
    _commit(source, "v2")
    _, problems = await prepare()
    assert not problems and (clone / "README.md").read_text() == "work in progress"
    assert _git(clone, "rev-parse", "origin/main") == _git(source, "rev-parse", "main")

    _git(clone, "commit", "-qam", "my work")
    mine = _git(clone, "rev-parse", "HEAD")
    _, problems = await prepare()
    assert not problems and _git(clone, "rev-parse", "HEAD") == mine


async def test_shared_folder_is_only_fetched(workspaces, tmp_path):
    source, clone, prepare = await _cloned(workspaces, tmp_path, shared=True)
    _commit(source, "v2")
    _, problems = await prepare()
    assert not problems and (clone / "README.md").read_text() == "v1"
    assert _git(clone, "rev-parse", "origin/main") == _git(source, "rev-parse", "main")


async def test_failed_update_is_reported(workspaces, tmp_path):
    import shutil

    source, clone, prepare = await _cloned(workspaces, tmp_path)
    shutil.rmtree(source)
    _, problems = await prepare()
    assert len(problems) == 1 and "Could not update 'repo'" in problems[0]
    assert (clone / "README.md").read_text() == "v1"


async def test_setup_script_runs_once_per_version(workspaces, tmp_path):
    from plainboard_protocol import WorkspaceSpec

    from plainboard_runner.workspace import prepare_workspace

    log = tmp_path / "setup.log"
    spec = WorkspaceSpec(path=str(workspaces / "p" / "a"), setup_script=f"echo v1 >> {log}")
    other_agent = WorkspaceSpec(path=str(workspaces / "p" / "b"), setup_script=f"echo v1 >> {log}")
    assert (await prepare_workspace(spec, Credentials()))[1] == []
    assert (await prepare_workspace(other_agent, Credentials()))[1] == []
    assert log.read_text().split() == ["v1"]  # the second agent does not repeat it

    changed = WorkspaceSpec(path=str(workspaces / "p" / "a"), setup_script=f"echo v2 >> {log}")
    await prepare_workspace(changed, Credentials())
    assert log.read_text().split() == ["v1", "v2"]


async def test_failed_setup_script_is_reported_and_retried(workspaces, tmp_path):
    from plainboard_protocol import WorkspaceSpec

    from plainboard_runner.workspace import prepare_workspace

    marker = tmp_path / "fixed"
    spec = WorkspaceSpec(path=str(workspaces / "p" / "a"), setup_script=f"echo broken; test -f {marker}; echo done")
    _, problems = await prepare_workspace(spec, Credentials())
    assert len(problems) == 1 and "setup script failed" in problems[0] and "broken" in problems[0]
    marker.write_text("")
    assert (await prepare_workspace(spec, Credentials()))[1] == []


def test_clone_problems_reach_the_agent(workspaces, monkeypatch):
    from plainboard_runner.providers import get_provider

    mock = get_provider("mock")
    seen = {}
    original = mock.run

    def capture(spec, bridge):
        seen["prompt"] = spec.prompt
        return original(spec, bridge)

    monkeypatch.setattr(mock, "run", capture)
    spec = run_spec(workspaces, "SAY ok")
    spec["workspace"]["repos"] = [{"name": "broken", "url": "file:///nope"}]
    with TestClient(app).stream("POST", "/runs", json=spec) as resp:
        events = [parse_event(line) for line in resp.iter_lines() if line]
    assert events[-1].type == "result"  # the run still happens
    assert seen["prompt"].startswith("[Workspace notice]") and "Could not clone 'broken'" in seen["prompt"]


def test_github_repo_listing(monkeypatch):
    calls = {}

    async def fake_run_cmd(args, cwd, env, timeout=600):
        calls["args"], calls["env"] = args, env
        return "\n".join(
            [
                '{"full_name":"acme/api","name":"api","clone_url":"https://github.com/acme/api.git","description":"API","default_branch":"main","private":true}',
                '{"full_name":"me/site","name":"site","clone_url":"https://github.com/me/site.git","description":null,"default_branch":"master","private":false}',
            ]
        )

    monkeypatch.setattr("plainboard_runner.main.run_cmd", fake_run_cmd)
    client = TestClient(app)
    assert client.post("/github/repos", json={}).json()["ok"] is False  # no token
    body = client.post("/github/repos", json={"github_token": "tok"}).json()
    assert body["ok"] and [r["full_name"] for r in body["repos"]] == ["acme/api", "me/site"]
    assert body["repos"][0]["private"] is True
    assert calls["env"]["GH_TOKEN"] == "tok" and "--paginate" in calls["args"]


def test_event_mapper_translates_sdk_messages():
    mapper = EventMapper()
    events = []
    events += mapper.map(SystemMessage(subtype="init", data={"session_id": "s1"}))
    events += mapper.map(
        AssistantMessage(
            content=[TextBlock(text="Working on it"), ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
            model="claude-sonnet-5-5",
            usage={"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 50, "output_tokens": 40},
        )
    )
    events += mapper.map(UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="file.txt", is_error=False)]))
    events += mapper.map(
        ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="s1",
                      total_cost_usd=0.01, usage={"input_tokens": 20, "output_tokens": 80}, result="Done")
    )
    assert [e.type for e in events] == ["session", "assistant_text", "tool_use", "usage", "tool_result", "result"]
    assert events[3].context_tokens == 1100
    assert events[-1].context_tokens == 1100 and events[-1].text == "Done" and events[-1].session_cost_usd == 0.01


def test_event_mapper_reports_provider_compaction():
    events = EventMapper().map(SystemMessage(subtype="compact_boundary", data={"compact_metadata": {"trigger": "auto", "pre_tokens": 190000}}))
    assert [e.type for e in events] == ["compaction"]
    assert events[0].tokens_before == 190000


def test_auth_env_modes():
    assert auth_env(Credentials(claude_auth_mode="oauth_token", claude_oauth_token="tok"))["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert auth_env(Credentials(claude_auth_mode="api_key", anthropic_api_key="key"))["ANTHROPIC_API_KEY"] == "key"
    assert "ANTHROPIC_API_KEY" not in auth_env(Credentials(claude_auth_mode="runner_login"))
    with pytest.raises(RuntimeError):
        auth_env(Credentials(claude_auth_mode="oauth_token"))


def test_external_mcp_config():
    assert external_mcp_config(McpServerSpec(name="fs", transport="stdio", command="npx", args=["-y", "x"]))["command"] == "npx"
    http = external_mcp_config(McpServerSpec(name="api", transport="http", url="https://x/mcp", headers={"A": "b"}))
    assert http == {"type": "http", "url": "https://x/mcp", "headers": {"A": "b"}}


def job_spec(workspaces, job_id: str, command: str) -> JobSpec:
    return JobSpec(id=job_id, command=command, workspace=str(workspaces / "p" / "a"), credentials=Credentials())


async def ended(job_id: str):
    for _ in range(100):
        state = jobs.status([job_id])[job_id]
        if not state.running:
            return state
        await asyncio.sleep(0.05)
    raise AssertionError("job did not end")


@pytest.fixture
def jobs_root(workspaces, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_ROOT", workspaces / "jobs")
    monkeypatch.setattr(jobs, "_jobs", {})


async def test_job_runs_in_workspace_and_reports_exit_code_and_tail(workspaces, jobs_root):
    state = await jobs.start(job_spec(workspaces, "a1", "pwd; echo oops >&2; exit 3"))
    assert state.running and state.log_path.endswith("a1.log")
    state = await ended("a1")
    assert not state.running and state.exit_code == 3
    assert str(workspaces / "p" / "a") in state.tail and "oops" in state.tail


async def test_job_status_only_returns_known_jobs(workspaces, jobs_root):
    await jobs.start(job_spec(workspaces, "a2", "true"))
    assert set(jobs.status(["a2", "ffff"])) == {"a2"}
    assert jobs.stop("ffff") is False
    await ended("a2")
    assert jobs.stop("a2") is False


async def test_stopping_a_job_reaches_its_children(workspaces, jobs_root):
    marker = workspaces / "child-alive"
    await jobs.start(job_spec(workspaces, "a3", f"(sleep 1; touch {marker}) & sleep 30"))
    await asyncio.sleep(0.2)
    assert jobs.stop("a3") is True
    assert (await ended("a3")).exit_code != 0
    await asyncio.sleep(1.2)
    assert not marker.exists()


async def test_job_outside_workspaces_is_rejected(workspaces, jobs_root):
    with pytest.raises(WorkspaceError):
        await jobs.start(JobSpec(id="a4", command="true", workspace="/etc", credentials=Credentials()))


async def test_notice_reaches_the_agent_once_after_a_tool_call():
    from plainboard_runner import notices
    from plainboard_runner.providers.claude_code import notice_hook

    hook = notice_hook("r9")
    assert await hook({}, None, None) == {}
    notices.push("r9", "Please wrap up")
    output = (await hook({}, None, None))["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse" and output["additionalContext"] == "Please wrap up"
    assert await hook({}, None, None) == {}


def test_notify_only_reaches_active_runs():
    assert TestClient(app).post("/runs/nope/notify", json={"text": "x"}).json() == {"delivered": False}


def test_rate_limit_events_become_limit_windows():
    from claude_agent_sdk.types import RateLimitInfo

    from plainboard_runner.providers.claude_code import limit_windows

    raw = {"unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": 100}, "seven_day": {"utilization": 0.1, "resetsAt": 200}}}
    windows = limit_windows(RateLimitInfo(status="allowed", raw=raw))
    assert [(w.name, w.utilization, w.resets_at) for w in windows] == [("five_hour", 0.4, 100), ("seven_day", 0.1, 200)]
    hit = limit_windows(RateLimitInfo(status="rejected", rate_limit_type="seven_day", resets_at=5))
    assert [(w.name, w.utilization, w.status) for w in hit] == [("seven_day", 1.0, "rejected")]
    assert limit_windows(RateLimitInfo(status="allowed")) == []
    assert limit_windows(RateLimitInfo(status="rejected", rate_limit_type="overage")) == []


def test_usage_answer_becomes_limit_windows():
    from datetime import UTC, datetime

    from plainboard_runner.providers.claude_code import usage_windows

    answer = {"five_hour": {"utilization": 40.0, "resets_at": "2026-10-30T12:00:00+00:00"}, "seven_day": {"utilization": None, "resets_at": None}, "seven_day_opus": None}
    (window,) = usage_windows(answer)
    assert (window.name, window.utilization, window.resets_at) == ("five_hour", 0.4, int(datetime(2026, 10, 30, 12, tzinfo=UTC).timestamp()))
    assert usage_windows(None) == []
