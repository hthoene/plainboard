import asyncio
import collections
import json

import httpx
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage
from fastapi.testclient import TestClient
from plainboard_protocol import Credentials, JobSpec, McpServerSpec, RunSpec, ToolBridgeSpec, ToolDef, parse_event

from plainboard_runner import config, jobs
from plainboard_runner.main import app
from plainboard_runner.providers.claude_code import ClaudeCodeProvider, EventMapper, auth_env, external_mcp_config
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


def test_run_accepts_messages_with_large_screenshots(workspaces):
    spec = RunSpec(**{**run_spec(workspaces, "hi"), "credentials": {"claude_auth_mode": "runner_login"}})
    options = ClaudeCodeProvider()._options(spec, ToolBridge(spec.bridge, workspaces), collections.deque())
    assert options.max_buffer_size > 10 * 1024 * 1024


def test_plainboards_own_tools_are_in_the_prompt_and_the_users_mcp_servers_stay_on_demand(workspaces):
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    external = [{"name": "docs", "transport": "http", "url": "https://x/mcp"}]
    spec = RunSpec(**{**run_spec(workspaces, "hi"), "credentials": {"claude_auth_mode": "runner_login"}, "mcp_servers": external})
    options = ClaudeCodeProvider()._options(spec, ToolBridge(spec.bridge, workspaces), collections.deque())
    assert options.mcp_servers["plainboard"]["alwaysLoad"] is True and "alwaysLoad" not in options.mcp_servers["docs"]
    # It reaches Claude Code: the SDK passes the server's settings on, without the server object itself.
    transport = SubprocessCLITransport(prompt="hi", options=options)
    transport._cli_path = "claude"
    command = transport._build_command()
    config = json.loads(command[command.index("--mcp-config") + 1])["mcpServers"]
    assert config["plainboard"] == {"type": "sdk", "name": "plainboard", "alwaysLoad": True} and config["docs"]["url"] == "https://x/mcp"


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


async def test_jobs_get_secrets_but_cannot_replace_git_settings(workspaces, jobs_root):
    creds = Credentials(github_token="gh-real", secrets={"NPM_TOKEN": "npm-1", "GH_TOKEN": "fake"})
    await jobs.start(JobSpec(id="a5", command='echo "$NPM_TOKEN $GH_TOKEN"', workspace=str(workspaces / "p" / "a"), credentials=creds))
    assert (await ended("a5")).tail.strip() == "npm-1 gh-real"


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


SITE = """
import http.server, socket, threading

class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.server.server_port == 18734 and not self.path.startswith("/preview-base/18734/"):
            self.send_response(401)  # a server with a base path: nothing outside it
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", "/home")
            self.end_headers()
            return
        body = b"<html><head><title>Paper &amp; Paw\\n  shop</title></head></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

def serve(host, port):
    http.server.HTTPServer((host, port), Page).serve_forever()

threading.Thread(target=serve, args=("0.0.0.0", 18731), daemon=True).start()
threading.Thread(target=serve, args=("127.0.0.1", 18732), daemon=True).start()
threading.Thread(target=serve, args=("0.0.0.0", 18734), daemon=True).start()
silent = socket.socket()  # listens and never answers: not a web server
silent.bind(("0.0.0.0", 18733))
silent.listen()
threading.Event().wait()
"""


async def test_the_web_servers_of_a_job_are_found_without_anyone_naming_a_port(workspaces, jobs_root, monkeypatch):
    from plainboard_protocol import JobPort

    from plainboard_runner import ports

    monkeypatch.setattr(ports, "_seen", {})
    folder = workspaces / "p" / "a"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "site.py").write_text(SITE)
    await jobs.start(job_spec(workspaces, "b1", "python site.py"))
    await jobs.start(job_spec(workspaces, "b2", "sleep 30"))
    found = {}
    for _ in range(100):
        found = await ports.of_jobs(["b1", "b2", "ffff"])
        if len(found.get("b1", [])) == 3:
            break
        await asyncio.sleep(0.1)
    # The title is of the page "/" leads to. The second server is only reachable from inside the runner,
    # the third has /preview-base/<port>/ as its base path and answers nowhere else.
    assert found == {
        "b1": [
            JobPort(port=18731, title="Paper & Paw shop"),
            JobPort(port=18732, local=True, title="Paper & Paw shop"),
            JobPort(port=18734, title="Paper & Paw shop", base=True),
        ]
    }
    # Who may open a preview goes by every port a job listens on, also one where no web server answers.
    assert await ports.listening_of_jobs(["b1", "b2"]) == {"b1": [18731, 18732, 18733, 18734]}

    assert jobs.stop("b1") and jobs.stop("b2")
    await ended("b1")
    assert await ports.of_jobs(["b1", "b2"]) == {} and not ports._seen


async def test_notice_reaches_the_agent_once_after_a_tool_call():
    from plainboard_runner import notices
    from plainboard_runner.providers.claude_code import notice_hook

    hook = notice_hook("r9")
    assert await hook({}, None, None) == {}
    notices.push("r9", "Please wrap up")
    output = (await hook({}, None, None))["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse" and output["additionalContext"] == "Please wrap up"
    assert await hook({}, None, None) == {}


async def test_sub_agent_tool_calls_leave_the_notice_for_the_agent():
    from plainboard_runner import notices
    from plainboard_runner.providers.claude_code import notice_hook

    hook = notice_hook("r10")
    notices.push("r10", "Please wrap up")
    assert await hook({"agent_id": "sub"}, None, None) == {}
    assert (await hook({}, None, None))["hookSpecificOutput"]["additionalContext"] == "Please wrap up"


class FakeInterruptClient:
    interrupts = 0

    async def interrupt(self):
        self.interrupts += 1


async def test_urgent_notice_interrupts_a_long_tool_call_after_the_grace_period(monkeypatch):
    from plainboard_runner import notices
    from plainboard_runner.providers.claude_code import UrgentNoticeWatch

    client = FakeInterruptClient()
    watch = UrgentNoticeWatch(client, "r11")
    watch.see(AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={})], model="m"))
    notices.push("r11", "Not urgent")
    monkeypatch.setattr(notices, "urgent_waiting", lambda run_id: 0.0)
    await watch.check()
    assert client.interrupts == 0  # no urgent message

    notices.push("r11", "Stop now", urgent=True)
    await watch.check()
    assert client.interrupts == 0  # still within the grace period

    monkeypatch.setattr(notices, "urgent_waiting", lambda run_id: 61.0)
    await watch.check()
    await watch.check()
    assert client.interrupts == 1 and watch.interrupted
    prompt = watch.resume_prompt()
    assert "Stop now" in prompt and "Not urgent" in prompt and "interrupted" in prompt
    assert not watch.interrupted and not watch.open_tools and notices.drain("r11") == []


async def test_urgent_notice_leaves_a_turn_without_a_running_tool_alone(monkeypatch):
    from plainboard_runner import notices
    from plainboard_runner.providers.claude_code import UrgentNoticeWatch

    client = FakeInterruptClient()
    watch = UrgentNoticeWatch(client, "r12")
    watch.see(AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={})], model="m"))
    watch.see(UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")]))
    monkeypatch.setattr(notices, "urgent_waiting", lambda run_id: 61.0)
    await watch.check()
    assert client.interrupts == 0


def test_urgent_flag_ends_with_pickup():
    from plainboard_runner import notices

    notices.push("r13", "Stop", urgent=True)
    assert notices.urgent_waiting("r13") >= 0 and "r13" in notices._urgent_since
    notices.drain("r13")
    assert notices.urgent_waiting("r13") == 0.0


def test_notify_only_reaches_active_runs():
    assert TestClient(app).post("/runs/nope/notify", json={"text": "x"}).json() == {"delivered": False}


def test_lists_the_turns_it_runs():
    assert TestClient(app).get("/runs").json() == []


def test_rate_limit_events_become_limit_windows():
    from claude_agent_sdk.types import RateLimitInfo

    from plainboard_runner.providers.claude_code import limit_windows

    raw = {"unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": 100}, "seven_day": {"utilization": 0.1, "resetsAt": 200}}}
    windows = limit_windows(RateLimitInfo(status="allowed", raw=raw))
    assert [(w.name, w.utilization, w.resets_at) for w in windows] == [("five_hour", 0.4, 100), ("seven_day", 0.1, 200)]
    hit = limit_windows(RateLimitInfo(status="rejected", rate_limit_type="five_hour", resets_at=5))
    assert [(w.name, w.utilization, w.resets_at, w.status) for w in hit] == [("five_hour", 1.0, 5, "rejected")]
    hit = limit_windows(RateLimitInfo(status="rejected", rate_limit_type="seven_day", resets_at=5))
    assert [(w.name, w.utilization, w.status) for w in hit] == [("seven_day", 1.0, "rejected")]
    assert limit_windows(RateLimitInfo(status="allowed")) == []
    assert limit_windows(RateLimitInfo(status="rejected", rate_limit_type="overage")) == []


def test_usage_answer_becomes_limit_windows():
    from datetime import UTC, datetime

    from plainboard_runner.providers.claude_code import usage_windows

    answer = {
        "five_hour": {"utilization": 10.0, "resets_at": None},
        "seven_day": {"utilization": 40.0, "resets_at": "2026-10-30T12:00:00+00:00"},
        "seven_day_sonnet": {"utilization": None, "resets_at": None},
        "seven_day_opus": None,
    }
    five, week = usage_windows(answer)
    assert (five.name, five.utilization, five.resets_at) == ("five_hour", 0.1, None)
    assert (week.name, week.utilization, week.resets_at) == ("seven_day", 0.4, int(datetime(2026, 10, 30, 12, tzinfo=UTC).timestamp()))
    assert usage_windows(None) == []


def test_a_turn_turned_away_by_a_used_up_limit_reports_the_window():
    from datetime import UTC, datetime

    from claude_agent_sdk import ResultMessage
    from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo

    from plainboard_runner.providers.claude_code import EventMapper, limit_hit

    now = datetime(2026, 11, 19, 15, 38, tzinfo=UTC)
    hit = limit_hit("You've hit your 5-hour limit · resets 7:10pm (UTC)", now)
    assert (hit.name, hit.utilization, hit.status, hit.resets_at) == ("five_hour", 1.0, "rejected", int(datetime(2026, 11, 19, 19, 10, tzinfo=UTC).timestamp()))
    assert limit_hit("You've hit your weekly limit · resets 2am (UTC)", now).resets_at == int(datetime(2026, 11, 20, 2, tzinfo=UTC).timestamp())
    assert limit_hit("Claude AI usage limit reached|1795200000", now).resets_at == 1795200000
    assert limit_hit("API Error: 500", now) is None and limit_hit(None) is None

    def result(error: str) -> ResultMessage:
        return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=0, session_id="s", result=error)

    events = EventMapper().map(result("You've hit your 5-hour limit · resets 7:10pm (UTC)"))
    assert [e.type for e in events] == ["limits", "result"] and events[0].windows[0].status == "rejected"
    mapper = EventMapper()  # Claude Code reported the window itself: it is not reported twice
    mapper.map(RateLimitEvent(rate_limit_info=RateLimitInfo(status="rejected", rate_limit_type="five_hour", resets_at=5), uuid="u", session_id="s"))
    assert [e.type for e in mapper.map(result("You've hit your 5-hour limit · resets 7:10pm (UTC)"))] == ["result"]


async def test_the_plan_of_a_claude_subscription(tmp_path, monkeypatch):
    import json

    from plainboard_protocol import Credentials

    from plainboard_runner.providers.claude_code import ClaudeCodeProvider, plan_of

    assert plan_of("max", "default_claude_max_20x") == ("Max 20x", 20.0)
    assert plan_of("claude_max", "default_claude_max_5x") == ("Max 5x", 5.0)
    assert plan_of("pro", None) == ("Pro", 1.0)
    assert plan_of("max", None) == ("Max", None)  # two sizes: the user says which
    assert plan_of("claude_ultra", None) == ("Ultra", None)
    assert plan_of(None, None) == (None, None)

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}}))
    provider = ClaudeCodeProvider()
    assert await provider.plan(Credentials(claude_auth_mode="runner_login")) == ("Max 20x", 20.0)
    assert await provider.plan(Credentials(claude_auth_mode="api_key", anthropic_api_key="sk")) == (None, None)


async def test_a_repository_uses_its_own_access(workspaces, tmp_path, monkeypatch):
    from plainboard_protocol import RepoSpec, WorkspaceSpec

    from plainboard_runner.workspace import prepare_workspace, repo_config

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    url = _git_repo(tmp_path / "src" / "repo", "v1")
    where = str(workspaces / "proj" / "dev")

    async def prepare(**access):
        path, problems = await prepare_workspace(WorkspaceSpec(path=where, repos=[RepoSpec(name="repo", url=url, **access)]), Credentials())
        assert not problems
        return path / "repo"

    # A deploy key: the clone itself keeps the ssh command, so the agent's own git commands use it too.
    clone = await prepare(ssh_key="PRIVATE KEY")
    command = _git(clone, "config", "--local", "core.sshCommand")
    (key_file,) = (tmp_path / "home" / ".plainboard" / "git-keys").iterdir()
    assert key_file.read_text() == "PRIVATE KEY\n" and key_file.as_posix() in command and "IdentitiesOnly=yes" in command
    assert _git(clone, "config", "--local", "plainboard.auth") == "ssh"
    # Back to the general access: our settings are gone again.
    clone = await prepare()
    assert "sshcommand" not in _git(clone, "config", "--local", "--list").lower() and "plainboard.auth" not in _git(clone, "config", "--local", "--list")

    # A token reaches only the repository's own host, with the user name the host expects.
    config = repo_config(RepoSpec(name="web", url="https://gitlab.com/acme/web.git", token="tok", token_user="deploy"))
    assert config == {"http.https://gitlab.com/.extraHeader": "Authorization: Basic ZGVwbG95OnRvaw=="}
    assert repo_config(RepoSpec(name="web", url="https://github.com/acme/web.git", token="tok")) == {
        "http.https://github.com/.extraHeader": "Authorization: Basic eC1hY2Nlc3MtdG9rZW46dG9r"
    }
    assert repo_config(RepoSpec(name="web", url="https://github.com/acme/web.git")) == {}

    # The general SSH key: for SSH addresses without a key of their own, and nothing else.
    general = Credentials(git_ssh_key="GENERAL KEY")
    ssh = repo_config(RepoSpec(name="web", url="git@github.com:acme/web.git"), general)
    assert "IdentitiesOnly=yes" in ssh["core.sshCommand"] and repo_config(RepoSpec(name="web", url="ssh://git@host/acme/web.git"), general) == ssh
    assert repo_config(RepoSpec(name="web", url="https://github.com/acme/web.git"), general) == {}
    assert repo_config(RepoSpec(name="web", url=url), general) == {}
    assert repo_config(RepoSpec(name="web", url="git@github.com:acme/web.git", ssh_key="PRIVATE KEY"), general) != ssh


async def test_send_files_uploads_workspace_files_and_forwards_attachment_ids(tmp_path):
    (tmp_path / "shot.png").write_bytes(b"png")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "Shown in the chat: shot.png.", "is_error": False})

    spec = ToolBridgeSpec(base_url="http://app", run_id="9", run_token="t", tools=[ToolDef(name="send_files", description="", input_schema={}, local=True)])
    bridge = ToolBridge(spec, tmp_path)
    bridge._client = httpx.AsyncClient(base_url="http://app", transport=httpx.MockTransport(handler))
    result = await bridge.call("send_files", {"paths": ["shot.png"], "attachment_ids": [4, 5]})
    assert not result.is_error, result.text
    assert seen["path"] == "/api/internal/runs/9/chat-files"
    assert b'filename="shot.png"' in seen["body"] and b"4,5" in seen["body"]
    assert not (await bridge.call("send_files", {"attachment_ids": [4]})).is_error
    assert (await bridge.call("send_files", {"paths": ["missing.png"]})).is_error


def test_claude_asks_for_the_long_cache_lifetime():
    # Turns of an agent are often more than five minutes apart; the short lifetime would rewrite the whole context each time.
    for creds in (Credentials(claude_auth_mode="api_key", anthropic_api_key="key"), Credentials(claude_auth_mode="oauth_token", claude_oauth_token="tok")):
        assert auth_env(creds)["ENABLE_PROMPT_CACHING_1H"] == "1"


async def test_a_slow_tool_call_is_told_how_long_it_took(monkeypatch):
    from plainboard_runner.providers import claude_code
    from plainboard_runner.providers.claude_code import ToolTimer, notice_hook

    clock = [1000.0]
    monkeypatch.setattr(claude_code.time, "monotonic", lambda: clock[0])
    timer = ToolTimer()
    hook = notice_hook("r11", timer)
    await timer.start({"tool_name": "Bash"}, "t1", None)
    clock[0] += 30
    assert await hook({"tool_name": "Bash"}, "t1", None) == {}  # a quick call says nothing
    await timer.start({"tool_name": "Bash"}, "t2", None)
    clock[0] += 720
    output = (await hook({"tool_name": "Bash"}, "t2", None))["hookSpecificOutput"]
    assert "That call took 12 min" in output["additionalContext"]
    await timer.start({"tool_name": "mcp__plainboard__wait_job"}, "t3", None)
    clock[0] += 720
    assert await hook({"tool_name": "mcp__plainboard__wait_job"}, "t3", None) == {}  # it says so itself


def test_pull_request_state_is_read_from_githubs_roll_up():
    from plainboard_runner.github import parse_state

    state = parse_state(
        {
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "headRefOid": "abc",
            "url": "https://github.com/acme/api/pull/7",
            "title": "Add it",
            "statusCheckRollup": [
                {"__typename": "CheckRun", "name": "test", "workflowName": "ci", "status": "COMPLETED", "conclusion": "FAILURE", "detailsUrl": "l1"},
                {"__typename": "CheckRun", "name": "test", "workflowName": "ci", "status": "IN_PROGRESS", "conclusion": "", "detailsUrl": "l2"},  # re-run: the last one counts
                {"__typename": "CheckRun", "name": "lint", "workflowName": "ci", "status": "COMPLETED", "conclusion": "SKIPPED"},
                {"__typename": "StatusContext", "context": "deploy/preview", "state": "ERROR", "targetUrl": "l3"},
            ],
        }
    )
    assert (state.state, state.mergeable, state.head_sha, state.title) == ("open", "yes", "abc", "Add it")
    assert [(c.name, c.state, c.link) for c in state.checks] == [("ci / test", "pending", "l2"), ("ci / lint", "pass", ""), ("deploy/preview", "fail", "l3")]
    assert parse_state({"state": "MERGED", "mergeable": "UNKNOWN"}).state == "merged"
    assert parse_state({"state": "OPEN", "mergeable": "CONFLICTING"}).mergeable == "conflict"


async def test_merge_tries_the_methods_a_repository_allows(monkeypatch):
    from plainboard_runner import github

    calls = []

    async def fake_run(args, cwd, env, timeout=600):
        calls.append(next(a for a in args if a in ("--squash", "--merge", "--rebase")))
        if "--squash" in args:
            raise WorkspaceError("gh pr failed: Squash merges are not allowed on this repository")
        return ""

    monkeypatch.setattr(github, "run_cmd", fake_run)
    result = await github.merge(Credentials(github_token="t"), "acme/api", 7)
    assert result.ok and result.detail == "merge" and calls == ["--squash", "--merge"]

    async def conflict(args, cwd, env, timeout=600):
        calls.append("x")
        raise WorkspaceError("gh pr failed: Pull request is not mergeable")

    monkeypatch.setattr(github, "run_cmd", conflict)
    result = await github.merge(Credentials(github_token="t"), "acme/api", 7)
    assert not result.ok and "not mergeable" in result.detail and calls[2:] == ["x"]  # another method would fail the same way


async def test_how_far_the_running_plainboard_is_behind_its_upstream():
    from plainboard_runner import github

    seen = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if "private" in request.url.path and not request.headers.get("authorization"):
            return httpx.Response(404, json={"message": "Not Found"})
        if "unknown" in request.url.path:
            return httpx.Response(404, json={"message": "Not Found"})
        if "limited" in request.url.path:
            return httpx.Response(403, json={"message": "rate limit"})
        return httpx.Response(200, json={"status": "ahead", "ahead_by": 3, "behind_by": 0, "html_url": "https://github.com/acme/app/compare/abc1234...main"})

    transport = httpx.MockTransport(answer)
    state = await github.upstream(Credentials(github_token="tok"), "acme/app", "main", "abc1234", transport=transport)
    assert (state.behind, state.url) == (3, "https://github.com/acme/app/compare/abc1234...main")
    # A public repository is asked without the user's token.
    assert seen == [("https://api.github.com/repos/acme/app/compare/abc1234...main?per_page=1", None)]

    # A private one: the token only after the question without it was refused.
    seen.clear()
    assert (await github.upstream(Credentials(github_token="tok"), "acme/private", "main", "abc1234", transport=transport)).behind == 3
    assert [auth for _url, auth in seen] == [None, "Bearer tok"]

    # GitHub does not know the commit there (a local build): no answer, no error.
    assert await github.upstream(Credentials(), "acme/unknown", "main", "abc1234", transport=transport) is None
    with pytest.raises(WorkspaceError):
        await github.upstream(Credentials(), "acme/limited", "main", "abc1234", transport=transport)


def test_the_checks_of_a_commit_on_a_branch():
    from plainboard_runner.github import parse_check_runs

    checks = parse_check_runs(
        [
            {"name": "Tests", "status": "completed", "conclusion": "failure", "html_url": "l1"},
            {"name": "Tests", "status": "in_progress", "conclusion": None, "html_url": "l2"},  # re-run: the last one counts
            {"name": "Lint", "status": "completed", "conclusion": "success", "html_url": "l3"},
            {"name": "Docs", "status": "completed", "conclusion": "skipped"},
            {"name": "Firmware", "status": "completed", "conclusion": "cancelled", "html_url": "l4"},  # a newer push replaced the run: no result
            {"name": "Size", "status": "completed", "conclusion": "timed_out"},
        ]
    )
    assert [(c.name, c.state, c.link) for c in checks] == [("Tests", "pending", "l2"), ("Lint", "pass", "l3"), ("Docs", "pass", ""), ("Firmware", "cancelled", "l4"), ("Size", "fail", "")]


class FakeUsageClient:
    """Stands in for the SDK client: answers the usage request with nothing, like a subscription
    signed in with a token, and reports the limits with a turn's first answer."""

    asked: list[str] = []

    def __init__(self, options=None):
        self.options = options
        self._query = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _send_control_request(self, request, timeout=None):
        return {"rate_limits": self.usage}

    async def query(self, prompt):
        FakeUsageClient.asked.append(f"{self.options.model}: {prompt}")

    async def receive_response(self):
        from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo

        windows = {"seven_day": {"utilization": 0.41, "resetsAt": 1795143600, "status": "allowed"}, "seven_day_overage_included": {"utilization": 0.0, "status": "allowed"}}
        yield RateLimitEvent(rate_limit_info=RateLimitInfo(status="allowed", rate_limit_type="seven_day", utilization=0.41, raw={"unifiedWindows": windows}), uuid="u", session_id="s")


async def test_the_limits_of_a_token_subscription_come_from_a_minimal_turn(monkeypatch):
    from plainboard_runner.providers import claude_code

    monkeypatch.setattr(claude_code, "ClaudeSDKClient", FakeUsageClient)
    FakeUsageClient.asked = []
    provider = claude_code.ClaudeCodeProvider()
    token = Credentials(claude_auth_mode="oauth_token", claude_oauth_token="tok")

    # The usage request answers: no turn, no tokens.
    FakeUsageClient.usage = {"seven_day": {"utilization": 23, "resets_at": "2026-11-20T03:00:00+00:00"}}
    (window,) = await provider.limits(token)
    assert (window.name, window.utilization) == ("seven_day", 0.23) and FakeUsageClient.asked == []

    # It answers with nothing (the token is for model requests only): one word to the small model shows them.
    FakeUsageClient.usage = None
    windows = {w.name: w for w in await provider.limits(token)}
    assert windows["seven_day"].utilization == 0.41 and windows["seven_day"].resets_at == 1795143600 and "seven_day_overage_included" in windows
    assert FakeUsageClient.asked == ["claude-haiku-5-5: ok"]

    assert await provider.limits(Credentials(claude_auth_mode="api_key", anthropic_api_key="k")) == []  # an API key has no plan


def turn_end(text: str) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s1", total_cost_usd=0.01, usage={}, result=text)


class FakeSessionClient:
    """Stands in for the SDK client with a session that goes on by itself: every entry of
    ``turns`` is one turn, the messages up to its result. Before a turn's result Claude Code asks
    the Stop hook, with the background work in flight then."""

    turns: list[tuple[list, list[dict]]] = []
    asked: list[str] = []

    def __init__(self, options=None):
        self.options = options
        self.turns = list(FakeSessionClient.turns)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        FakeSessionClient.asked.append(prompt)

    async def receive_response(self):
        if not self.turns:
            return
        messages, in_flight = self.turns.pop(0)
        for message in messages[:-1]:
            yield message
        await self.options.hooks["Stop"][0].hooks[0]({"hook_event_name": "Stop", "background_tasks": in_flight}, None, None)
        yield messages[-1]


async def run_session(monkeypatch, workspaces, turns) -> list:
    from plainboard_runner.providers import claude_code

    monkeypatch.setattr(claude_code, "ClaudeSDKClient", FakeSessionClient)
    FakeSessionClient.turns, FakeSessionClient.asked = turns, []
    spec = RunSpec(**{**run_spec(workspaces, "Write the report"), "credentials": {"claude_auth_mode": "runner_login"}})
    return [event async for event in ClaudeCodeProvider().run(spec, ToolBridge(spec.bridge, workspaces))]


def says(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m")


async def test_a_turn_that_ends_with_sub_agents_at_work_goes_on_when_they_report(monkeypatch, workspaces):
    """Seen in production: an agent started four analyses in the background and ended its turn with
    "I continue when they return". The assignment was closed with that note and the analyses lost."""
    analysis = {"id": "a1", "type": "subagent", "status": "running", "description": "Heap analysis"}
    server = {"id": "b1", "type": "shell", "status": "running", "description": "dev server", "command": "npm run dev"}
    events = await run_session(
        monkeypatch,
        workspaces,
        [
            ([says("Interim note: the analyses still run."), turn_end("Interim note")], [analysis, server]),
            ([says("One is back."), turn_end("Still waiting")], [analysis]),
            ([says("The report."), turn_end("The report")], [server]),  # a shell may never end: nobody waits for it
        ],
    )
    assert [e.text for e in events if e.type == "assistant_text"] == ["Interim note: the analyses still run.", "One is back.", "The report."]
    assert [e.text for e in events if e.type == "result"] == ["The report"]
    assert FakeSessionClient.asked == ["Write the report"]  # the session went on by itself: no prompt, no turn of waiting


async def test_a_session_that_ends_while_the_agent_waits_keeps_its_last_answer(monkeypatch, workspaces):
    analysis = {"id": "a1", "type": "subagent", "status": "running", "description": "Heap analysis"}
    events = await run_session(monkeypatch, workspaces, [([says("Interim note."), turn_end("Interim note")], [analysis])])
    assert [(e.text, e.is_error) for e in events if e.type == "result"] == [("Interim note", False)]


async def test_a_waiting_agent_gets_messages_at_once_and_stops_waiting_for_silent_work(monkeypatch):
    from plainboard_protocol import ResultEvent

    from plainboard_runner import notices
    from plainboard_runner.providers import claude_code
    from plainboard_runner.providers.claude_code import BackgroundWork

    class Client:
        asked: list[str] = []

        async def query(self, prompt):
            self.asked.append(prompt)

    client, work = Client(), BackgroundWork("r20")
    await work.stop_hook({"background_tasks": [{"id": "a1", "type": "subagent", "description": "Heap analysis"}]}, None, None)
    notices.push("r20", "The owner asks for the RAM numbers first")
    await work.check(client)
    assert client.asked == [] and notices.pending("r20")  # the agent still works: the message arrives after its tool call

    assert work.holds(ResultEvent(text="Interim note"))
    await work.check(client)
    assert client.asked == ["The owner asks for the RAM numbers first"] and not notices.pending("r20")

    # The agent answered and waits again; then nothing is heard for too long.
    work.see(says("Noted."))
    await work.stop_hook({"background_tasks": [{"id": "a1", "type": "subagent", "description": "Heap analysis"}]}, None, None)
    assert work.holds(ResultEvent(text="Still waiting"))
    await work.check(client)
    assert len(client.asked) == 1
    monkeypatch.setattr(claude_code, "BACKGROUND_SILENT_SECONDS", 0)
    await work.check(client)
    assert "silent" in client.asked[-1] and "finish with what you have" in client.asked[-1]
    await work.stop_hook({"background_tasks": [{"id": "a1", "type": "subagent", "description": "Heap analysis"}]}, None, None)
    assert not work.holds(ResultEvent(text="Report without the heap analysis"))  # this end is the turn's result


def test_a_deleted_projects_folders_are_removed(workspaces, tmp_path, monkeypatch):
    from plainboard_runner import trains

    monkeypatch.setattr(trains, "trains_root", lambda: tmp_path / "trains-root")
    (workspaces / "shop" / "dev").mkdir(parents=True)
    (workspaces / "shop" / "dev" / "file.txt").write_text("x")
    (workspaces / "other").mkdir()
    (trains.folder("p1-r1")).mkdir(parents=True)
    client = TestClient(app)

    body = client.post("/files/remove", json={"workspace": str(workspaces / "shop"), "trains": ["p1-r1", "p1-r2"]}).json()
    assert body == {"ok": True, "detail": "2 folders removed"}
    assert not (workspaces / "shop").exists() and not trains.folder("p1-r1").exists() and (workspaces / "other").exists()

    # Never anything but one project's folder below the root.
    for path in (str(workspaces), "/etc", str(workspaces / ".." / "elsewhere")):
        assert client.post("/files/remove", json={"workspace": path}).json()["ok"] is False, path
    assert (workspaces / "other").exists()
    assert client.post("/files/remove", json={"trains": ["../x"]}).status_code == 422


def test_what_a_token_may_do_is_read_from_githubs_answer():
    from plainboard_runner import github

    classic = "HTTP/2.0 200 OK\r\nContent-Type: application/json\r\nX-Oauth-Scopes: repo, workflow\r\n\r\n" + '{"login":"octo","id":1}'
    assert github.token_summary(classic) == ("octo", ["repo", "workflow"])
    assert github.token_summary("HTTP/2.0 200 OK\nX-Oauth-Scopes: \n\n" + '{"login":"octo"}') == ("octo", [])
    assert github.token_summary("HTTP/2.0 200 OK\nContent-Type: application/json\n\n" + '{"login":"octo"}') == ("octo", None)


def test_a_repository_is_created_on_github(monkeypatch):
    calls = []

    async def fake(args, cwd, env, timeout=600):
        calls.append((args, env.get("GH_TOKEN")))
        if "name=taken" in args:
            raise WorkspaceError("gh api failed: name already exists on this account (HTTP 422)")
        return '{"full_name":"acme/shop","name":"shop","clone_url":"https://github.com/acme/shop.git","description":"","default_branch":"main","private":true}'

    monkeypatch.setattr("plainboard_runner.main.run_cmd", fake)
    client = TestClient(app)
    body = client.post("/github/repos/create", json={"credentials": {"github_token": "tok"}, "name": "shop", "owner": "acme"}).json()
    assert body["ok"] and body["repos"][0]["clone_url"] == "https://github.com/acme/shop.git"
    args, token = calls[-1]
    assert args[:5] == ["gh", "api", "-X", "POST", "orgs/acme/repos"] and token == "tok"
    # Private, and with a first commit: a repository without a branch is nothing an agent can start from.
    assert "private=true" in args and "auto_init=true" in args

    assert client.post("/github/repos/create", json={"credentials": {"github_token": "tok"}, "name": "shop", "private": False}).json()["ok"]
    assert calls[-1][0][4] == "user/repos" and "private=false" in calls[-1][0]
    taken = client.post("/github/repos/create", json={"credentials": {"github_token": "tok"}, "name": "taken"}).json()
    assert not taken["ok"] and "already exists" in taken["detail"]
    assert client.post("/github/repos/create", json={"credentials": {}, "name": "shop"}).json()["detail"] == "No GitHub token configured"
    assert client.post("/github/repos/create", json={"credentials": {"github_token": "tok"}, "name": "a/b"}).status_code == 422


async def test_only_branches_that_are_fully_on_the_base_branch_are_removed(tmp_path, monkeypatch):
    import subprocess

    from plainboard_protocol import RepoSpec, TidySpec

    from plainboard_runner import trains

    monkeypatch.setattr(trains, "trains_root", lambda: tmp_path / "trains-root")
    url = _git_repo(tmp_path / "origin", "base")
    origin = tmp_path / "origin"
    old = {"GIT_AUTHOR_DATE": "2020-01-01T00:00:00", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"}

    def run(*args, env=None):
        return subprocess.run(args, cwd=origin, check=True, capture_output=True, text=True, env={**__import__("os").environ, **(env or {})}).stdout.strip()

    def commit(name: str, env=None):
        (origin / f"{name}.txt").write_text(name)
        run("git", "add", ".")
        run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name, env=env)

    # merged-old: its commit is on main. With main's own (young) commit on top, main's tip is not that commit.
    run("git", "checkout", "-q", "-b", "merged-old")
    commit("merged", env=old)
    run("git", "checkout", "-q", "main")
    run("git", "merge", "-q", "--ff-only", "merged-old")
    run("git", "branch", "develop")  # merged and old, but a name repositories keep
    run("git", "branch", "release/1.0")
    run("git", "branch", "in-flight")  # merged and old, but PlainBoard has it
    run("git", "branch", "stack-base")  # merged and old, but open pull requests are based on it
    run("git", "checkout", "-q", "-b", "own-work")
    commit("unmerged", env=old)  # a commit of its own: stays
    run("git", "checkout", "-q", "main")
    commit("young")
    run("git", "branch", "just-started")  # no commit of its own yet, and main's tip is young: stays

    real = trains.run_cmd

    async def host(args, cwd, env, timeout=600):
        if args[0] == "gh":
            assert args[3] == "repos/acme/api/pulls?state=open&per_page=100"
            return "main\nstack-base\n"
        return await real(args, cwd, env, timeout)

    monkeypatch.setattr(trains, "run_cmd", host)
    spec = TidySpec(credentials=Credentials(), key="p1-r1", remote=RepoSpec(name="origin", url=url), base="main", keep=["in-flight"], github="acme/api")
    result = await trains.tidy(spec)
    assert result.deleted == ["merged-old"]
    left = set(run("git", "branch", "--format=%(refname:short)").splitlines())
    assert {"main", "develop", "release/1.0", "in-flight", "stack-base", "own-work", "just-started"} <= left and "merged-old" not in left
    assert (await trains.tidy(spec)).deleted == []  # nothing left to remove


async def test_a_token_that_cannot_read_check_runs_reads_the_runs_of_actions(monkeypatch):
    """Seen in production: a fine-grained token got a 403 for the check runs of a commit, and the result of a
    check run that had long passed was never read."""
    from plainboard_runner import github

    asked = []

    async def host(args, cwd, env, timeout=600):
        asked.append(args[2])
        if "check-runs" in args[2]:
            raise WorkspaceError("gh api failed: Resource not accessible by personal access token (HTTP 403)")
        if "actions/runs" in args[2]:
            if env.get("GH_TOKEN") == "nothing":
                raise WorkspaceError("gh api failed: Resource not accessible by personal access token (HTTP 403)")
            return '{"name":"Deploy","status":"completed","conclusion":"success","html_url":"https://ci/1"}\n{"name":"Tests","status":"in_progress","conclusion":null,"html_url":"https://ci/2"}'
        return '{"context":"security/scan","state":"failure","target_url":"https://scan/1"}'

    monkeypatch.setattr(github, "run_cmd", host)
    checks = {c.name: (c.state, c.link) for c in await github.commit_checks(Credentials(github_token="fine"), "acme/api", "abc")}
    assert checks == {"Deploy": ("pass", "https://ci/1"), "Tests": ("pending", "https://ci/2"), "security/scan": ("fail", "https://scan/1")}
    assert asked == ["repos/acme/api/commits/abc/check-runs?per_page=100", "repos/acme/api/actions/runs?head_sha=abc&per_page=100", "repos/acme/api/commits/abc/status"]
    # Neither can be read: the error names both, so the user sees what the token lacks.
    with pytest.raises(WorkspaceError, match="the runs of Actions neither"):
        await github.commit_checks(Credentials(github_token="nothing"), "acme/api", "abc")


async def test_an_agent_cannot_leave_work_running_in_the_background_when_its_turn_ends():
    from plainboard_runner.providers.claude_code import DISALLOWED_TOOLS, BackgroundWork, ToolTimer

    assert "Monitor" in DISALLOWED_TOOLS
    refused = await ToolTimer().start({"tool_name": "Bash", "tool_input": {"command": "scripts/check-fast.sh", "run_in_background": True}}, "t1", None)
    assert refused["hookSpecificOutput"]["permissionDecision"] == "deny" and "start_job" in refused["hookSpecificOutput"]["permissionDecisionReason"]
    assert await ToolTimer().start({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "t2", None) == {}

    # A shell Claude Code moved to the background by itself: the stop is refused once.
    work = BackgroundWork("r30")
    shell = {"background_tasks": [{"id": "b1", "type": "shell", "description": "check", "command": "scripts/check-fast.sh"}]}
    refused = await work.stop_hook(shell, None, None)
    assert refused["decision"] == "block" and "scripts/check-fast.sh" in refused["reason"] and "start_job" in refused["reason"]
    assert await work.stop_hook({**shell, "stop_hook_active": True}, None, None) == {}
    assert await work.stop_hook({"background_tasks": []}, None, None) == {}


def test_claude_codes_limit_message_is_not_the_agents_answer(tmp_path):
    from claude_agent_sdk import AssistantMessage, TextBlock

    from pathlib import Path

    from plainboard_runner.providers.claude_code import EventMapper, resume_point, session_file

    limit = AssistantMessage(content=[TextBlock(text="You've hit your 5-hour limit · resets 4:20pm (UTC)")], model="<synthetic>", error="rate_limit")
    mapper = EventMapper()
    assert mapper.map(limit) == [] and mapper.last_text is None
    assert [e.type for e in mapper.map(AssistantMessage(content=[TextBlock(text="Done.")], model="claude-opus-5-5"))] == ["assistant_text"]

    # A session whose last turn ended in that message is taken up before it.
    assert session_file("/workspaces/megapanel-v5/devops", "s1") == Path.home() / ".claude/projects/-workspaces-megapanel-v5-devops/s1.jsonl"
    file = tmp_path / "s1.jsonl"
    entries = [
        {"type": "user", "uuid": "u1", "parentUuid": None, "message": {"role": "user", "content": "Assignment"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {"model": "claude-opus-5-5", "content": []}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "a1", "isApiErrorMessage": True, "message": {"model": "<synthetic>", "content": []}},
        {"type": "last-prompt", "leafUuid": "a2"},
    ]
    file.write_text("".join(json.dumps(e) + "\n" for e in entries))
    assert resume_point(file) == "a1"
    file.write_text("".join(json.dumps(e) + "\n" for e in entries[:2]))
    assert resume_point(file) is None and resume_point(tmp_path / "missing.jsonl") is None
