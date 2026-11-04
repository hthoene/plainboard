import asyncio
import json
import shutil

import pytest
from mcp import Client
from plainboard_protocol import McpServerSpec, RunSpec, ToolBridgeSpec, ToolCallResult, ToolDef

from plainboard_runner import notices
from plainboard_runner.mcp_stdio import build_server, write_bridge_file
from plainboard_runner.providers import codex
from plainboard_runner.providers.codex import AppServer, EventMapper, TurnRunner, limit_windows, run_config, toml_value

TOOL = ToolDef(name="create_ticket", description="Create a ticket.", input_schema={"type": "object", "properties": {"title": {"type": "string"}}})


def spec(**overrides) -> RunSpec:
    data = {
        "run_id": "r1",
        "provider": "codex",
        "model": "gpt-6.1-sol",
        "system_prompt": "You are a test agent.",
        "prompt": "Do it.",
        "workspace": {"path": "/workspaces/p/a"},
        "credentials": {},
        "bridge": {"base_url": "http://app", "run_id": "r1", "run_token": "t", "tools": [TOOL.model_dump()]},
    }
    return RunSpec.model_validate({**data, **overrides})


def item(method: str, **fields) -> tuple[str, dict]:
    return method, {"threadId": "th", "turnId": "tu", "item": fields}


def test_toml_values_for_config_overrides():
    assert toml_value({"command": "py", "args": ["-m", "x"], "env": {"A_B": 'q"ü'}, "n": 30, "on": True}) == (
        '{"command" = "py", "args" = ["-m", "x"], "env" = {"A_B" = "q\\"ü"}, "n" = 30, "on" = true}'
    )


def test_run_config_brings_board_tools_and_external_servers(tmp_path):
    config = run_config(
        spec(
            toolset="board",
            mcp_servers=[
                McpServerSpec(name="docs", transport="http", url="https://docs.example/mcp", headers={"X": "1"}),
                McpServerSpec(name="fs", transport="stdio", command="npx", args=["fs"]),
            ],
        ),
        tmp_path / "bridge.json",
    )
    servers = config["mcp_servers"]
    assert servers["plainboard"]["args"] == ["-m", "plainboard_runner.mcp_stdio"]
    assert servers["plainboard"]["env"] == {"PLAINBOARD_BRIDGE_FILE": str(tmp_path / "bridge.json")}
    assert servers["docs"] == {"url": "https://docs.example/mcp", "http_headers": {"X": "1"}}
    assert servers["fs"]["command"] == "npx"
    assert config["shell_environment_policy"]["ignore_default_excludes"] is True
    assert config["web_search"] == "disabled"
    assert "web_search" not in run_config(spec(), tmp_path / "b.json")


def test_event_mapper_translates_notifications():
    mapper = EventMapper("th")
    events = []
    for method, params in [
        ("item/agentMessage/delta", {"threadId": "th", "delta": "Hel"}),
        item("item/started", type="commandExecution", id="c1", command="ls"),
        item("item/completed", type="commandExecution", id="c1", command="ls", status="failed", exitCode=2, aggregatedOutput="nope"),
        item("item/started", type="mcpToolCall", id="m1", server="plainboard", tool="create_ticket", arguments={"title": "x"}),
        item("item/completed", type="mcpToolCall", id="m1", server="plainboard", tool="create_ticket", status="completed", result={"content": [{"type": "text", "text": "#7"}]}),
        item("item/completed", type="agentMessage", id="a1", text="Done.", phase="final_answer"),
        ("item/completed", {"threadId": "sub", "item": {"type": "agentMessage", "id": "a2", "text": "sub-agent chatter"}}),
        ("thread/tokenUsage/updated", {"threadId": "th", "tokenUsage": {"last": {"inputTokens": 100, "cachedInputTokens": 0, "outputTokens": 10, "totalTokens": 110}, "total": {"inputTokens": 1100, "cachedInputTokens": 600, "outputTokens": 60}}}),
        ("thread/tokenUsage/updated", {"threadId": "th", "tokenUsage": {"last": {"inputTokens": 200, "cachedInputTokens": 100, "outputTokens": 20, "totalTokens": 220}, "total": {"inputTokens": 1300, "cachedInputTokens": 700, "outputTokens": 80}}}),
        item("item/completed", type="contextCompaction", id="k1"),
    ]:
        events += mapper.map(method, params)
    kinds = [e.type for e in events]
    assert kinds == ["text_delta", "tool_use", "tool_result", "tool_use", "tool_result", "assistant_text", "usage", "usage", "compaction"]
    assert events[1].name == "Bash" and events[1].input == {"command": "ls"}
    assert events[2].is_error and "[exit code 2]" in events[2].content
    assert events[3].name == "mcp__plainboard__create_ticket" and events[4].content == "#7" and not events[4].is_error
    assert events[-1].tokens_before == 220 and not mapper.open_tools
    result = mapper.result("completed", None)
    # This run: 1300-1000 input of which 700-600 cached; 80-50 output.
    assert (result.input_tokens, result.cache_read_tokens, result.output_tokens) == (200, 100, 30)
    assert result.text == "Done." and result.session_id == "th" and result.context_tokens == 220 and not result.is_error
    failed = mapper.result("failed", "boom")
    assert failed.is_error and failed.error == "boom"


def test_rate_limits_become_codex_windows():
    windows = limit_windows({"primary": {"usedPercent": 40, "windowDurationMins": 300, "resetsAt": 1000}, "secondary": {"usedPercent": 100, "windowDurationMins": 10080}})
    assert [(w.name, w.utilization, w.resets_at, w.status) for w in windows] == [("codex_five_hour", 0.4, 1000, None), ("codex_seven_day", 1.0, None, "rejected")]
    events = EventMapper("th").map("account/rateLimits/updated", {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 5, "windowDurationMins": 300}}})
    assert events[0].type == "limits" and events[0].windows[0].name == "codex_five_hour"
    assert EventMapper("th").map("account/rateLimits/updated", {"rateLimits": {"limitId": "other", "primary": {"usedPercent": 5}}}) == []


class FakeServer:
    """Plays a Codex app server: answers requests and feeds scripted notifications."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.requests: list[tuple[str, dict]] = []
        self.responses: list[tuple] = []

    async def request(self, method, params=None, timeout=0):
        self.requests.append((method, params))
        if method == "turn/start":
            return {"turn": {"id": f"tu{sum(m == 'turn/start' for m, _ in self.requests)}"}}
        return {}

    async def respond(self, request_id, result=None, error=None):
        self.responses.append((request_id, result, error))

    async def next(self, timeout=None):
        if not self.script:
            raise AssertionError("script ended")
        step = self.script.pop(0)
        if callable(step):
            step = step()
        if step == "wait":
            raise TimeoutError
        return step

    def stderr(self):
        return ""


def done(turn: str, status: str = "completed") -> dict:
    return {"method": "turn/completed", "params": {"threadId": "th", "turn": {"id": turn, "status": status}}}


async def collect(runner: TurnRunner) -> list:
    return [event async for event in runner.run()]


async def test_turn_steers_messages_in_between_steps():
    notices.drain("r1")
    server = FakeServer(
        [
            {"method": "item/started", "params": {"threadId": "th", "item": {"type": "commandExecution", "id": "c1", "command": "sleep 1"}}},
            lambda: notices.push("r1", "New ticket #3") or "wait",  # arrives while the command runs: wait for it
            {"method": "item/completed", "params": {"threadId": "th", "item": {"type": "commandExecution", "id": "c1", "status": "completed", "exitCode": 0}}},
            {"id": 9, "method": "item/commandExecution/requestApproval", "params": {}},
            done("tu1"),
        ]
    )
    events = await collect(TurnRunner(server, EventMapper("th"), spec()))
    steers = [p for m, p in server.requests if m == "turn/steer"]
    assert len(steers) == 1 and steers[0]["input"][0]["text"] == "New ticket #3" and steers[0]["expectedTurnId"] == "tu1"
    assert server.responses == [(9, {"decision": "accept"}, None)]
    assert events[-1].type == "result" and not events[-1].is_error


async def test_urgent_message_interrupts_a_long_tool_call(monkeypatch):
    notices.drain("r1")
    monkeypatch.setattr(codex, "NOTICE_GRACE_SECONDS", 0)
    server = FakeServer(
        [
            {"method": "item/started", "params": {"threadId": "th", "item": {"type": "commandExecution", "id": "c1", "command": "sleep 999"}}},
            lambda: notices.push("r1", "Stop now", urgent=True) or "wait",
            "wait",
            done("tu1", "interrupted"),
            done("tu2"),
        ]
    )
    events = await collect(TurnRunner(server, EventMapper("th"), spec()))
    methods = [m for m, _ in server.requests]
    assert methods == ["turn/start", "turn/interrupt", "turn/start"]
    assert "Stop now" in server.requests[2][1]["input"][0]["text"]
    assert [e.type for e in events].count("result") == 1 and not events[-1].is_error


async def test_board_tools_as_mcp_server():
    class Bridge:
        tools = [TOOL]

        async def call(self, name, args):
            return ToolCallResult(text=f"{name}:{args['title']}", is_error=args["title"] == "bad")

    async with Client(build_server(Bridge())) as client:
        listed = await client.list_tools()
        assert [t.name for t in listed.tools] == ["create_ticket"]
        ok = await client.call_tool("create_ticket", {"title": "x"})
        assert ok.content[0].text == "create_ticket:x" and not ok.is_error
        assert (await client.call_tool("create_ticket", {"title": "bad"})).is_error


@pytest.mark.skipif(not shutil.which(codex.CODEX_BIN), reason="Codex CLI not installed")
async def test_codex_app_server_loads_the_board_tools(tmp_path):
    """The real CLI: our config is accepted and the board tools reach it through the MCP server."""
    bridge_file = tmp_path / "bridge.json"
    run = spec(workspace={"path": str(tmp_path)})
    write_bridge_file(bridge_file, ToolBridgeSpec.model_validate(run.bridge.model_dump()), tmp_path)
    async with AppServer(tmp_path / "home", config=run_config(run, bridge_file), cwd=tmp_path) as server:
        status = await server.request("mcpServerStatus/list", {"serverName": "plainboard"}, timeout=60)
    assert "create_ticket" in json.dumps(status)
