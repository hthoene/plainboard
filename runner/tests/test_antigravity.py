import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from plainboard_protocol import Credentials, McpServerSpec, RunSpec, ToolDef

from plainboard_runner import notices
from plainboard_runner.providers import antigravity
from plainboard_runner.providers.antigravity import (
    AntigravityProvider,
    EventMapper,
    TurnRunner,
    analyze_turn_outcome,
    answer_server_request,
    bound_command_line,
    bound_output_tail,
    build_env,
    match_thinking_level,
    mcp_servers_config,
    models_from_setup,
    parse_authorization_url,
    parse_go_duration_seconds,
    parse_model_quota_windows,
    parse_summary_windows,
    parse_version,
    prepare_profile,
    quota_user_agent,
    read_last_step_after,
    read_last_step_index,
    remove_session_files,
    validate_login_response,
)

TOOL = ToolDef(name="create_ticket", description="Create a ticket.", input_schema={"type": "object", "properties": {"title": {"type": "string"}}})


def spec(**overrides) -> RunSpec:
    data = {
        "run_id": "r1",
        "provider": "antigravity",
        "model": "gemini-4-neon",
        "system_prompt": "You are a test agent.",
        "prompt": "Do it.",
        "workspace": {"path": "/workspaces/p/a"},
        "credentials": {"antigravity_auth_mode": "google", "antigravity_account": "default"},
        "bridge": {"base_url": "http://app", "run_id": "r1", "run_token": "t", "tools": [TOOL.model_dump()]},
    }
    return RunSpec.model_validate({**data, **overrides})


def test_environment_isolation_and_profile_preparation(tmp_path):
    home = tmp_path / "acc"
    acp_dir = prepare_profile(home, "oauth-personal")
    assert json.loads((acp_dir / "settings.json").read_text()) == {"auth": {"type": "oauth-personal"}}

    base_env = {
        "PATH": "/usr/bin",
        "GEMINI_API_KEY": "leaked-key",
        "GOOGLE_API_KEY": "leaked-google",
        "GOOGLE_CLOUD_PROJECT": "leaked-proj",
        "DISPLAY": ":0",
        "WAYLAND_DISPLAY": "wayland-0",
        "CHROME_CRASHPAD_PIPE_NAME": "pipe",
        "CUSTOM_SECRET": "keep-me",
    }
    env = build_env(home, Credentials(antigravity_auth_mode="google"), base_env, tmp_path / "tmp")
    assert "GEMINI_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env
    assert "GOOGLE_CLOUD_PROJECT" not in env
    assert "DISPLAY" not in env and "WAYLAND_DISPLAY" not in env
    assert "CHROME_CRASHPAD_PIPE_NAME" not in env
    assert env["CUSTOM_SECRET"] == "keep-me"
    assert env["GEMINI_HOME"] == str(home)
    assert env["AGY_ACP_FORCE_FILE_STORAGE"] == "1"
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"
    assert env["SSH_CONNECTION"] == "127.0.0.1 0 127.0.0.1 0"

    api_env = build_env(
        home,
        Credentials(antigravity_auth_mode="api_key", gemini_api_key="AIza-test"),
        base_env,
        tmp_path / "tmp",
    )
    assert api_env["GEMINI_API_KEY"] == "AIza-test"
    assert "SSH_CONNECTION" not in api_env

    with pytest.raises(antigravity.AntigravityError):
        build_env(home, Credentials(antigravity_auth_mode="api_key"), base_env, tmp_path / "tmp")


def test_account_home_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "ACCOUNTS_HOME", tmp_path / "accounts")
    assert antigravity.account_home("default") == tmp_path / "accounts" / "default"
    assert antigravity.account_home("ab12", api_key=True) == tmp_path / "accounts" / "ab12-api-key"
    with pytest.raises(ValueError):
        antigravity.account_home("../escape")


def test_mcp_servers_config_formats_stdio_http_and_sse(tmp_path):
    run = spec(
        mcp_servers=[
            McpServerSpec(name="docs", transport="http", url="https://docs.example/mcp", headers={"X-Key": "1"}),
            McpServerSpec(name="events", transport="sse", url="https://events.example/sse", headers={"Auth": "b"}),
            McpServerSpec(name="fs", transport="stdio", command="npx", args=["fs"], env={"FOO": "bar"}),
        ]
    )
    v1 = mcp_servers_config(run, tmp_path / "bridge.json", v2=False)
    by_name = {s["name"]: s for s in v1}
    assert "type" not in by_name["plainboard"]
    assert by_name["plainboard"]["args"] == ["-m", "plainboard_runner.mcp_stdio"]
    assert by_name["plainboard"]["env"] == [{"name": "PLAINBOARD_BRIDGE_FILE", "value": str(tmp_path / "bridge.json")}]
    assert by_name["docs"] == {"type": "http", "name": "docs", "url": "https://docs.example/mcp", "headers": [{"name": "X-Key", "value": "1"}]}
    assert by_name["events"] == {"type": "sse", "name": "events", "url": "https://events.example/sse", "headers": [{"name": "Auth", "value": "b"}]}
    assert by_name["fs"] == {"name": "fs", "command": "npx", "args": ["fs"], "env": [{"name": "FOO", "value": "bar"}]}

    v2 = mcp_servers_config(run, tmp_path / "bridge.json", v2=True)
    by_name_v2 = {s["name"]: s for s in v2}
    assert by_name_v2["plainboard"]["type"] == "stdio"
    assert by_name_v2["fs"]["type"] == "stdio"


def test_event_mapper_normalizes_tools_usage_and_compaction():
    mapper = EventMapper("s1")
    events = []
    long_cmd = "pytest " + ("a" * 9000) + " --tail-flag"
    for params in [
        {"sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Checking "}}},
        {"sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "now."}}},
        # Command execution: keeps command HEAD, not tail (T3 Code #40702)
        {"sessionId": "s1", "update": {"sessionUpdate": "tool_call", "toolCallId": "c1", "kind": "execute", "title": "Ran command", "rawInput": {"CommandLine": long_cmd}}},
        {"sessionId": "s1", "update": {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "failed", "rawOutput": {"combinedOutput": "err", "exitCode": 2}}},
        # MCP call via call_mcp_tool: shows mcp__server__tool, not "Ran command" (T3 Code #39474)
        {"sessionId": "s1", "update": {"sessionUpdate": "tool_call", "toolCallId": "m1", "kind": "other", "title": "Run mcp_plainboard_create_ticket", "rawInput": {"ServerName": "plainboard", "ToolName": "create_ticket", "Arguments": {"title": "Bug"}}}},
        {"sessionId": "s1", "update": {"sessionUpdate": "tool_call_update", "toolCallId": "m1", "status": "completed", "content": [{"type": "content", "content": {"type": "text", "text": "#42"}}]}},
        # Diff edit maps to Edit with file_path/old_string/new_string
        {"sessionId": "s1", "update": {"sessionUpdate": "tool_call", "toolCallId": "e1", "kind": "edit", "title": "Edit app.py", "content": [{"type": "diff", "path": "app.py", "oldText": "x=1", "newText": "x=2"}]}},
        {"sessionId": "s1", "update": {"sessionUpdate": "tool_call_update", "toolCallId": "e1", "status": "completed", "content": [{"type": "diff", "path": "app.py", "oldText": "x=1", "newText": "x=2"}]}},
        # Usage & compaction
        {"sessionId": "s1", "update": {"sessionUpdate": "usage_update", "used": 12345, "size": 1000000}},
        {"sessionId": "s1", "update": {"sessionUpdate": "compaction_update", "compactionId": "k1", "status": "completed"}},
        # Ignored other session
        {"sessionId": "other", "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "nope"}}},
    ]:
        events.extend(mapper.map("session/update", params))

    assert [e.type for e in events] == [
        "text_delta",
        "text_delta",
        "assistant_text",
        "tool_use",
        "tool_result",
        "tool_use",
        "tool_result",
        "tool_use",
        "tool_result",
        "usage",
        "compaction",
    ]
    assert events[2].text == "Checking now."
    assert events[3].name == "Bash" and events[3].input["command"].startswith("pytest aaaa") and events[3].input["command"].endswith("[Command truncated]")
    assert events[4].is_error and "[exit code 2]" in events[4].content
    assert events[5].name == "mcp__plainboard__create_ticket" and events[5].input == {"title": "Bug"}
    assert events[6].content == "#42" and not events[6].is_error
    assert events[7].name == "Edit" and events[7].input == {"file_path": "app.py", "old_string": "x=1", "new_string": "x=2"}
    assert events[9].context_tokens == 12345
    assert events[10].tokens_before == 12345

    # Token usage: outputTokens already includes thoughtTokens (T3 Code #39686)
    mapper.add_prompt_usage({"inputTokens": 500, "cachedReadTokens": 1200, "cachedWriteTokens": 50, "outputTokens": 300, "thoughtTokens": 180})
    res = mapper.result()
    assert (res.input_tokens, res.cache_read_tokens, res.cache_creation_tokens, res.output_tokens) == (500, 1200, 50, 300)


def _create_session_db(home: Path, session_id: str, rows: list[tuple[int, int, bytes]]) -> Path:
    db_path = home / "antigravity-acp" / "conversations" / f"{session_id}.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, step_payload BLOB)")
    conn.executemany("INSERT INTO steps (idx, step_type, step_payload) VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db_path


def test_turn_outcome_detects_502_quota_404_and_invalid_thinking_signature(tmp_path):
    now = 1_700_000_000.0
    # 502 Bad Gateway reported as end_turn (T3 Code #33596)
    _create_session_db(tmp_path, "s502", [(1, 17, b"Error 502, Message: upstream reset, Status: 502 Bad Gateway\ngoroutine 1")])
    step = read_last_step_after(tmp_path, "s502", -1)
    fail = analyze_turn_outcome("end_turn", step, now=now)
    assert fail is not None and not fail.is_quota and fail.message == "upstream reset"

    # Quota exhausted with Go reset delay (T3 Code #29594)
    _create_session_db(tmp_path, "sq", [(2, 17, b"Error 429, Message: limit, Status: RESOURCE_EXHAUSTED [reason:QUOTA_EXHAUSTED quotaResetDelay:2h30m]")])
    fail_q = analyze_turn_outcome("end_turn", read_last_step_after(tmp_path, "sq", 1), now=now)
    assert fail_q is not None and fail_q.is_quota and fail_q.reset_at == int(now + 2.5 * 3600)

    # Mid-turn 404 after planner response -> retryable with continue (T3 Code #39869)
    _create_session_db(tmp_path, "s404", [(1, 15, b"ok"), (2, 17, b"NOT_FOUND (code 404): edge miss")])
    fail_404 = analyze_turn_outcome("end_turn", read_last_step_after(tmp_path, "s404", 0), now=now)
    assert fail_404 is not None and fail_404.retry_continue and fail_404.message == "edge miss"

    # Broken thinking signature -> marks session broken so PlainBoard drops provider_session_id (T3 Code #28889)
    _create_session_db(tmp_path, "ssig", [(1, 17, b"messages.4.content.0.thinking.signature: Field required")])
    fail_sig = analyze_turn_outcome("end_turn", read_last_step_after(tmp_path, "ssig", -1), now=now)
    assert fail_sig is not None and fail_sig.session_broken and "session can no longer be resumed" in fail_sig.message.lower()

    # Clean step (planner response) -> no error
    _create_session_db(tmp_path, "sok", [(1, 15, b"done")])
    assert read_last_step_index(tmp_path, "sok") == 1
    assert analyze_turn_outcome("end_turn", read_last_step_after(tmp_path, "sok", 0), now=now) is None


def test_oauth_url_and_code_response_validation():
    url = "https://accounts.google.com/o/oauth2/auth?response_type=code&state=abc123&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback"
    pending = parse_authorization_url(url)
    assert pending.state == "abc123" and pending.redirect_uri == "https://antigravity.google/oauth-callback"

    assert validate_login_response(pending, "4/0A-bare-code_123") == "4/0A-bare-code_123"
    full_cb = "https://antigravity.google/oauth-callback?state=abc123&code=mycode"
    assert validate_login_response(pending, full_cb) == full_cb

    with pytest.raises(ValueError):
        validate_login_response(pending, "https://antigravity.google/oauth-callback?state=wrong&code=mycode")
    with pytest.raises(ValueError):
        validate_login_response(pending, "line1\nline2")
    with pytest.raises(antigravity.AntigravityError):
        parse_authorization_url("https://evil.example/o/oauth2/auth?response_type=code&state=x&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback")


def test_quota_summary_and_model_fallback_parsing():
    now = 1_700_000_000.0
    summary = {
        "groups": [
            {
                "displayName": "Gemini",
                "buckets": [
                    {"bucketId": "gemini-pro-5h", "window": "5h", "remainingFraction": 0.6, "resetTime": "2023-11-15T00:00:00Z"},
                    {"bucketId": "gemini-pro-weekly", "window": "weekly", "remainingFraction": 0.0, "resetTime": "2023-11-20T00:00:00Z"},
                    {"bucketId": "gemini-carbon-5h", "window": "5h", "remainingFraction": 1.0, "resetTime": "2023-11-15T03:13:20Z"},  # untouched: +5h from now
                    {"bucketId": "disabled-bucket", "window": "5h", "remainingFraction": 0.1, "disabled": True},
                ],
            },
            {
                "displayName": "3P",
                "buckets": [
                    {"bucketId": "3p-shared-weekly", "window": "weekly", "remainingFraction": 0.75, "resetTime": "2023-11-20T00:00:00Z"},
                ],
            },
        ]
    }
    windows = {w.name: w for w in parse_summary_windows(summary, requested_at=now)}
    assert set(windows) == {"antigravity_gemini_five_hour", "antigravity_gemini_seven_day", "antigravity_carbon_five_hour", "antigravity_shared_seven_day"}
    assert windows["antigravity_gemini_five_hour"].utilization == 0.4 and windows["antigravity_gemini_five_hour"].status is None
    assert windows["antigravity_gemini_seven_day"].utilization == 1.0 and windows["antigravity_gemini_seven_day"].status == "rejected"
    assert windows["antigravity_carbon_five_hour"].resets_at is None  # untouched rolling window omitted
    assert windows["antigravity_shared_seven_day"].utilization == 0.25
    assert "antigravity/hub/2.24.0" in quota_user_agent()

    fallback = parse_model_quota_windows(
        {
            "models": {
                "gemini-4-neon": {"modelProvider": "MODEL_PROVIDER_GOOGLE", "quotaInfo": {"remainingFraction": 0.5}, "weeklyQuotaInfo": {"remainingFraction": 0.8}},
                "claude-sonnet": {"modelProvider": "MODEL_PROVIDER_ANTHROPIC", "quotaInfo": {"remainingFraction": 0.2}},
            }
        }
    )
    by_name = {w.name: w for w in fallback}
    assert by_name["antigravity_gemini_quota"].utilization == 0.5
    assert by_name["antigravity_gemini_seven_day"].utilization == 0.2
    assert by_name["antigravity_claude_quota"].utilization == 0.8


def test_models_thinking_level_skills_and_cleanup(tmp_path):
    setup = {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "currentValue": "gemini-4-neon",
                "options": [
                    {"value": "gemini-4-neon", "name": "Gemini 4 Neon"},
                    {"groupId": "3p", "name": "Partner", "options": [{"value": "claude-opus-5-5", "name": "Claude Opus 5.5"}]},
                ],
            },
            {
                "id": "thinking",
                "category": "thought_level",
                "type": "select",
                "currentValue": "medium",
                "options": [{"value": "low"}, {"value": "medium"}, {"value": "high"}],
            },
        ]
    }
    models = models_from_setup(setup)
    assert [(m.id, m.tier) for m in models] == [("gemini-4-neon", "xlarge"), ("claude-opus-5-5", "large")]
    thinking_opt = setup["configOptions"][1]
    assert match_thinking_level("high", thinking_opt) == "high"
    assert match_thinking_level("max", thinking_opt) == "high"
    assert match_thinking_level("minimal", thinking_opt) == "low"
    assert parse_version("1.5.15") == (1, 5, 15)
    assert parse_go_duration_seconds("1h2m3.5s") == pytest.approx(3723.5)
    assert bound_output_tail("x" * 200, limit=50).startswith("[Earlier output truncated]")
    assert bound_command_line("echo " + "z" * 100, limit=20).startswith("echo zzz")

    provider = AntigravityProvider()
    assert provider.skills_folder(spec(toolset="full"), tmp_path) == tmp_path / ".gemini" / "skills"
    assert provider.skills_folder(spec(toolset="board"), tmp_path) is None
    assert provider.skills_folder(spec(toolset="read"), tmp_path) is None

    # Disposable session cleanup
    sid = "11111111-2222-4333-8444-555555555555"
    db = _create_session_db(tmp_path, sid, [])
    (tmp_path / "antigravity-acp" / "brain" / sid).mkdir(parents=True)
    (tmp_path / "antigravity-acp" / "brain" / sid / "note.md").write_text("hi")
    (tmp_path / "antigravity-acp" / "annotations").mkdir(parents=True)
    (tmp_path / "antigravity-acp" / "annotations" / f"{sid}.pbtxt").write_text("a")
    remove_session_files(tmp_path, sid)
    assert not db.exists()
    assert not (tmp_path / "antigravity-acp" / "brain" / sid).exists()
    assert not (tmp_path / "antigravity-acp" / "annotations" / f"{sid}.pbtxt").exists()


class FakeAcpServer:
    def __init__(self, script: list, prompt_results: list[dict]) -> None:
        self.script = list(script)
        self.prompt_results = list(prompt_results)
        self.version = (1, 5, 15)
        self.requests: list[tuple[str, dict]] = []
        self.notifications: list[tuple[str, dict]] = []
        self.responses: list[tuple] = []
        self._active_prompt: asyncio.Future | None = None

    def start_request(self, method: str, params: dict | None = None) -> asyncio.Future:
        self.requests.append((method, params or {}))
        fut = asyncio.get_running_loop().create_future()
        if method == "session/prompt":
            if self._active_prompt is None or self._active_prompt.done():
                self._active_prompt = fut
            else:
                fut.set_result({"stopReason": "end_turn"})
        else:
            fut.set_result({})
        return fut

    async def notify(self, method: str, params: dict | None = None) -> None:
        self.notifications.append((method, params or {}))
        if method == "session/cancel" and self._active_prompt and not self._active_prompt.done():
            self._active_prompt.set_result(self.prompt_results.pop(0))

    async def respond(self, request_id, result=None, error=None, code=-32601) -> None:
        self.responses.append((request_id, result, error))

    async def next(self, timeout=None):
        await asyncio.sleep(0)
        if not self.script:
            if self._active_prompt and not self._active_prompt.done():
                self._active_prompt.set_result(self.prompt_results.pop(0))
            await asyncio.sleep(10)
            return None
        step = self.script.pop(0)
        if callable(step):
            step = step()
        if step == "finish_prompt":
            if self._active_prompt and not self._active_prompt.done():
                self._active_prompt.set_result(self.prompt_results.pop(0))
            await asyncio.sleep(10)
            return None
        if step == "wait":
            await asyncio.sleep(0.05)
            return await self.next()
        return step

    def drain_nowait(self) -> list:
        return []

    def stderr(self) -> str:
        return ""


async def test_turn_runner_permissions_steering_and_urgent_interrupt(tmp_path, monkeypatch):
    notices.drain("r1")
    monkeypatch.setattr(antigravity, "NOTICE_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(antigravity, "NOTICE_GRACE_SECONDS", 0)

    # 1. Permission auto-approval (full) vs read-only enforcement (read) vs interaction_* cancel
    server_perm = FakeAcpServer([], [])
    perm_opts = [{"optionId": "allow_1", "kind": "allow_once"}, {"optionId": "deny_1", "kind": "reject_once"}]
    await answer_server_request(
        server_perm,
        {"id": 1, "method": "session/request_permission", "params": {"toolCall": {"toolCallId": "c1", "kind": "execute"}, "options": perm_opts}},
        toolset="full",
    )
    await answer_server_request(
        server_perm,
        {"id": 2, "method": "session/request_permission", "params": {"toolCall": {"toolCallId": "c2", "kind": "execute"}, "options": perm_opts}},
        toolset="read",
    )
    await answer_server_request(
        server_perm,
        {"id": 3, "method": "session/request_permission", "params": {"toolCall": {"toolCallId": "c3", "kind": "read"}, "options": perm_opts}},
        toolset="read",
    )
    await answer_server_request(
        server_perm,
        {"id": 4, "method": "session/request_permission", "params": {"toolCall": {"toolCallId": "interaction_9", "kind": "other"}, "options": perm_opts}},
        toolset="full",
    )
    assert server_perm.responses == [
        (1, {"outcome": {"outcome": "selected", "optionId": "allow_1"}}, None),
        (2, {"outcome": {"outcome": "selected", "optionId": "deny_1"}}, None),
        (3, {"outcome": {"outcome": "selected", "optionId": "allow_1"}}, None),
        (4, {"outcome": {"outcome": "cancelled"}}, None),
    ]

    # 2. Urgent notice cancels a long tool call and resumes the turn
    server = FakeAcpServer(
        script=[
            {"method": "session/update", "params": {"sessionId": "s1", "update": {"sessionUpdate": "tool_call", "toolCallId": "c1", "kind": "execute", "title": "sleep 999"}}},
            lambda: notices.push("r1", "Stop now", urgent=True) or "wait",
            {"method": "session/update", "params": {"sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Stopped cleanly."}}}},
            "finish_prompt",
        ],
        prompt_results=[
            {"stopReason": "cancelled"},
            {"stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 5}},
        ],
    )
    events = [e async for e in TurnRunner(server, EventMapper("s1"), spec(), tmp_path, "Start").run()]
    assert any(m == "session/cancel" for m, _ in server.notifications)
    prompts = [p["prompt"][0]["text"] for m, p in server.requests if m == "session/prompt"]
    assert len(prompts) == 2 and "Stop now" in prompts[1]
    assert events[-1].type == "result" and not events[-1].is_error and events[-1].text == "Stopped cleanly."
