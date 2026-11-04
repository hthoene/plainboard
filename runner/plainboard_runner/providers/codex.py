"""Codex provider built on the Codex CLI's app server (``codex app-server``: JSON-RPC over stdio).

Auth: a ChatGPT subscription signed in on the runner (device code from Settings, or
``codex login --device-auth`` in the runner container), or an OpenAI API key. The two use separate
Codex homes, so an API key never replaces the subscription's sign-in.

Each run starts its own app server. PlainBoard's tools reach Codex as a stdio MCP server
(:mod:`plainboard_runner.mcp_stdio`); messages for a running turn are steered into it.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from plainboard_protocol import (
    AssistantTextEvent,
    CompactionEvent,
    CompletionResult,
    CompletionSpec,
    Credentials,
    LimitsEvent,
    LimitWindow,
    McpServerSpec,
    ModelInfo,
    ProviderAccount,
    ProviderInfo,
    ResultEvent,
    RunEvent,
    RunSpec,
    SessionEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseEvent,
    UsageEvent,
)

from .. import notices
from ..config import TOOL_RESULT_PREVIEW_CHARS
from ..mcp_stdio import BRIDGE_FILE_ENV, write_bridge_file
from ..tool_bridge import ToolBridge
from ..workspace import tool_env
from .base import Provider

log = logging.getLogger(__name__)

CODEX_BIN = os.environ.get("PLAINBOARD_CODEX_BIN", "codex")
SUBSCRIPTION_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
API_KEY_HOME = Path.home() / ".plainboard" / "codex-api-key"
BRIDGE_DIR = Path(tempfile.gettempdir()) / "plainboard-bridge"
MCP_SERVER_NAME = "plainboard"
MAX_LINE_BYTES = 64 * 1024 * 1024  # one line holds a whole item, e.g. a long command output
REQUEST_TIMEOUT = 120
TOOL_TIMEOUT_SECONDS = 3600  # ask_agent may wait for another agent's whole turn
NOTICE_GRACE_SECONDS = 60
NOTICE_CHECK_SECONDS = 2
LOGIN_TIMEOUT_SECONDS = 15 * 60
MODELS_TTL_SECONDS = 3600

MODELS = [
    ModelInfo(id="gpt-6.1-sol", label="GPT-6.1 Sol", tier="large"),
    ModelInfo(id="gpt-6-astra", label="GPT-6 Astra", tier="xlarge"),
    ModelInfo(id="gpt-6.1-terra", label="GPT-6.1 Terra", tier="medium"),
    ModelInfo(id="gpt-6-luna", label="GPT-6 Luna", tier="small"),
]
TIER_HINTS = {"astra": "xlarge", "sol": "large", "terra": "medium", "luna": "small"}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
TOOL_ITEMS = {"commandExecution", "mcpToolCall", "fileChange", "webSearch", "imageView"}
LIMIT_NAMES = {300: "codex_five_hour", 10080: "codex_seven_day"}


class CodexError(RuntimeError):
    pass


def toml_value(value: Any) -> str:
    """A value for ``codex -c key=value``: TOML, which shares JSON's string escapes."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(k), ensure_ascii=False)} = {toml_value(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"No TOML form for {type(value).__name__}")


def guess_tier(model_id: str) -> str:
    return next((tier for hint, tier in TIER_HINTS.items() if hint in model_id), "medium")


def limit_windows(snapshot: dict[str, Any] | None) -> list[LimitWindow]:
    """The plan's windows (5-hour and weekly) from a Codex rate limit snapshot."""
    windows = []
    for key in ("primary", "secondary"):
        window = (snapshot or {}).get(key)
        if not window or window.get("usedPercent") is None:
            continue
        minutes = window.get("windowDurationMins")
        name = LIMIT_NAMES.get(minutes) or (f"codex_{minutes}_min" if minutes else f"codex_{key}")
        used = window["usedPercent"] / 100
        windows.append(LimitWindow(name=name, utilization=used, resets_at=window.get("resetsAt"), status="rejected" if used >= 1 else None))
    return windows


def external_mcp_config(server: McpServerSpec) -> dict[str, Any]:
    if server.transport == "stdio":
        return {"command": server.command or "", "args": server.args, "env": server.env}
    return {"url": server.url or "", "http_headers": server.headers}  # Codex speaks streamable HTTP only


def run_config(spec: RunSpec, bridge_file: Path) -> dict[str, Any]:
    servers: dict[str, Any] = {s.name: external_mcp_config(s) for s in spec.mcp_servers if s.name != MCP_SERVER_NAME}
    servers[MCP_SERVER_NAME] = {
        "command": sys.executable,
        "args": ["-m", "plainboard_runner.mcp_stdio"],
        "env": {BRIDGE_FILE_ENV: str(bridge_file)},
        "startup_timeout_sec": 30,
        "tool_timeout_sec": TOOL_TIMEOUT_SECONDS,
    }
    config: dict[str, Any] = {
        "mcp_servers": servers,
        # Codex drops variables named like *KEY*/*TOKEN* from commands by default; agents need GH_TOKEN and the user's secrets.
        "shell_environment_policy": {"inherit": "all", "ignore_default_excludes": True},
    }
    if spec.toolset == "board":  # smaller prompt: no web search, no project instructions
        config["web_search"] = "disabled"
        config["project_doc_max_bytes"] = 0
    return config


# For every app server: the sign-in in a file of the Codex home (the container has no keyring).
BASE_CONFIG: dict[str, Any] = {
    "cli_auth_credentials_store": "file",
    "history": {"persistence": "none"},
    "check_for_update_on_startup": False,
}
COMPLETION_CONFIG: dict[str, Any] = {"mcp_servers": {}, "web_search": "disabled", "project_doc_max_bytes": 0}


class AppServer:
    """One ``codex app-server`` process. Notifications and the server's requests arrive in :meth:`next`."""

    def __init__(self, home: Path, env: dict[str, str] | None = None, config: dict[str, Any] | None = None, cwd: Path | None = None) -> None:
        self.home = home
        self.env = env or {}
        self.config = config or {}
        self.cwd = cwd
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=30)
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._messages: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._ids = 0
        self._tasks: list[asyncio.Task[None]] = []

    async def __aenter__(self) -> AppServer:
        self.home.mkdir(parents=True, exist_ok=True)
        args = [CODEX_BIN, "app-server"]
        for key, value in {**BASE_CONFIG, **self.config}.items():
            args += ["-c", f"{key}={toml_value(value)}"]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **self.env, "CODEX_HOME": str(self.home)},
                cwd=str(self.cwd) if self.cwd else None,
                limit=MAX_LINE_BYTES,
            )
        except FileNotFoundError as exc:
            raise CodexError("The Codex CLI is not installed in the runner") from exc
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._read_stderr())]
        try:
            await self.request("initialize", {"clientInfo": {"name": "plainboard", "title": "PlainBoard", "version": "0.1"}})
            await self._send({"method": "initialized", "params": {}})
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        proc = self._proc
        if proc and proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.stdin.close()  # type: ignore[union-attr]
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:
                    proc.kill()
        for task in self._tasks:
            task.cancel()

    def stderr(self) -> str:
        return "\n".join(self.stderr_tail)

    async def _read(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while line := await self._proc.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if "method" not in message and "id" in message:
                    future = self._pending.pop(message["id"], None)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(CodexError((message["error"] or {}).get("message") or "Codex request failed"))
                        else:
                            future.set_result(message.get("result"))
                else:
                    await self._messages.put(message)
        finally:
            error = CodexError(f"Codex exited\n{self.stderr()}".strip())
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            await self._messages.put(None)

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while line := await self._proc.stderr.readline():
            self.stderr_tail.append(line.decode(errors="replace").rstrip())

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(message) + "\n").encode())
        await self._proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = REQUEST_TIMEOUT) -> Any:
        self._ids += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[self._ids] = future
        await self._send({"id": self._ids, "method": method, "params": params})
        return await asyncio.wait_for(future, timeout)

    async def respond(self, request_id: Any, result: Any = None, error: str | None = None) -> None:
        if error is not None:
            await self._send({"id": request_id, "error": {"code": -32601, "message": error}})
        else:
            await self._send({"id": request_id, "result": result})

    async def next(self, timeout: float | None = None) -> dict[str, Any] | None:
        """The next notification or server request; None once Codex has exited."""
        return await asyncio.wait_for(self._messages.get(), timeout)


async def answer_server_request(server: AppServer, message: dict[str, Any]) -> None:
    """Approve whatever Codex asks: agents have full control of the runner, like Claude's bypass mode."""
    method = message.get("method")
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        await server.respond(message["id"], {"decision": "accept"})
    elif method in ("execCommandApproval", "applyPatchApproval"):
        await server.respond(message["id"], {"decision": "approved"})
    elif method == "mcpServer/elicitation/request":
        await server.respond(message["id"], {"action": "decline"})
    elif method == "item/tool/requestUserInput":
        await server.respond(message["id"], {"answers": {}})
    else:
        await server.respond(message["id"], error=f"PlainBoard does not handle {method}")


def _mcp_result_text(item: dict[str, Any]) -> tuple[str, bool]:
    if item.get("error"):
        return str((item["error"] or {}).get("message") or "Tool failed"), True
    parts = []
    for block in ((item.get("result") or {}).get("content") or []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, dict):
            parts.append(f"[{block.get('type', 'content')}]")
    return "\n".join(parts), item.get("status") == "failed"


class EventMapper:
    """Translates app server notifications of one thread into protocol events (kept separate for unit tests)."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.context_tokens = 0
        self.last_text: str | None = None
        self.final_text: str | None = None
        self.model_calls = 0
        self.baseline: dict[str, int] | None = None
        self.total: dict[str, int] = {}
        self.error: str | None = None
        self.open_tools: set[str] = set()

    def usage(self) -> dict[str, int]:
        """Tokens used by this run: the thread's total now minus its total before the run."""
        base = self.baseline or {}
        used = {key: max(0, int(self.total.get(key) or 0) - int(base.get(key) or 0)) for key in ("inputTokens", "cachedInputTokens", "outputTokens")}
        return {
            "input_tokens": max(0, used["inputTokens"] - used["cachedInputTokens"]),
            "cache_read_tokens": used["cachedInputTokens"],
            "output_tokens": used["outputTokens"],
        }

    def map(self, method: str, params: dict[str, Any]) -> list[RunEvent]:
        if params.get("threadId") not in (None, self.thread_id):
            return []  # a sub-agent's thread: keep it out of the transcript
        events: list[RunEvent] = []
        if method == "item/agentMessage/delta":
            if params.get("delta"):
                events.append(TextDeltaEvent(text=params["delta"]))
        elif method == "item/started":
            item = params.get("item") or {}
            kind, item_id = item.get("type"), item.get("id", "")
            if kind in TOOL_ITEMS:
                self.open_tools.add(item_id)
            if kind == "commandExecution":
                events.append(ToolUseEvent(id=item_id, name="Bash", input={"command": item.get("command", "")}))
            elif kind == "mcpToolCall":
                args = item.get("arguments")
                events.append(ToolUseEvent(id=item_id, name=f"mcp__{item.get('server')}__{item.get('tool')}", input=args if isinstance(args, dict) else {}))
            elif kind == "fileChange":
                events.append(ToolUseEvent(id=item_id, name="Edit", input={"files": [c.get("path", "") for c in item.get("changes") or []]}))
        elif method == "item/completed":
            item = params.get("item") or {}
            kind, item_id = item.get("type"), item.get("id", "")
            self.open_tools.discard(item_id)
            if kind == "agentMessage":
                text = item.get("text") or ""
                if text.strip():
                    self.last_text = text
                    if item.get("phase") == "final_answer":
                        self.final_text = text
                    events.append(AssistantTextEvent(text=text))
            elif kind == "commandExecution":
                failed = item.get("status") != "completed" or item.get("exitCode") not in (0, None)
                output = item.get("aggregatedOutput") or ""
                if item.get("exitCode") not in (0, None):
                    output = f"{output}\n[exit code {item['exitCode']}]".strip()
                events.append(ToolResultEvent(tool_use_id=item_id, content=output[:TOOL_RESULT_PREVIEW_CHARS], is_error=failed))
            elif kind == "mcpToolCall":
                text, failed = _mcp_result_text(item)
                events.append(ToolResultEvent(tool_use_id=item_id, content=text[:TOOL_RESULT_PREVIEW_CHARS], is_error=failed))
            elif kind == "fileChange":
                summary = "\n".join(f"{(c.get('kind') or {}).get('type', 'update')} {c.get('path', '')}" for c in item.get("changes") or [])
                events.append(ToolResultEvent(tool_use_id=item_id, content=summary[:TOOL_RESULT_PREVIEW_CHARS], is_error=item.get("status") != "completed"))
            elif kind == "webSearch":
                events.append(ToolUseEvent(id=item_id, name="WebSearch", input={"query": item.get("query", "")}))
                events.append(ToolResultEvent(tool_use_id=item_id))
            elif kind == "imageView":
                events.append(ToolUseEvent(id=item_id, name="ViewImage", input={"path": str(item.get("path", ""))}))
                events.append(ToolResultEvent(tool_use_id=item_id))
            elif kind == "contextCompaction":
                events.append(CompactionEvent(tokens_before=self.context_tokens))
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            last, total = usage.get("last") or {}, usage.get("total") or {}
            if self.baseline is None:
                self.baseline = {key: int(total.get(key) or 0) - int(last.get(key) or 0) for key in ("inputTokens", "cachedInputTokens", "outputTokens")}
            self.total = total
            self.model_calls += 1
            tokens = int(last.get("totalTokens") or 0)
            if tokens:
                self.context_tokens = tokens
                events.append(UsageEvent(context_tokens=tokens))
        elif method == "account/rateLimits/updated":
            snapshot = params.get("rateLimits") or {}
            if snapshot.get("limitId") in (None, "codex") and (windows := limit_windows(snapshot)):
                events.append(LimitsEvent(windows=windows))
        elif method == "error":
            if not params.get("willRetry"):
                self.error = (params.get("error") or {}).get("message") or "Codex error"
        return events

    def result(self, status: str, error: str | None) -> ResultEvent:
        failed = status != "completed"
        message = error or self.error or (f"Turn {status}" if failed else None)
        return ResultEvent(
            session_id=self.thread_id,
            is_error=failed,
            error=message if failed else None,
            text=self.final_text or self.last_text,
            num_turns=self.model_calls,
            context_tokens=self.context_tokens,
            **self.usage(),
        )


def text_input(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text, "text_elements": []}]


class TurnRunner:
    """Runs one PlainBoard turn on a thread: Codex turns, plus messages that arrive meanwhile."""

    def __init__(self, server: AppServer, mapper: EventMapper, spec: RunSpec) -> None:
        self.server = server
        self.mapper = mapper
        self.spec = spec
        self.turn_id: str | None = None
        self.interrupted = False

    async def _deliver_notices(self) -> None:
        """Steer waiting messages into the turn between steps. An urgent one interrupts a long tool call."""
        run_id = self.spec.run_id
        if not self.turn_id or not notices.pending(run_id) or self.interrupted:
            return
        if self.mapper.open_tools:
            if notices.urgent_waiting(run_id) >= NOTICE_GRACE_SECONDS:
                self.interrupted = True
                try:
                    await self.server.request("turn/interrupt", {"threadId": self.mapper.thread_id, "turnId": self.turn_id})
                except Exception:
                    self.interrupted = False
                    log.warning("Could not interrupt run %s", run_id, exc_info=True)
            return
        pending = notices.drain(run_id)
        try:
            await self.server.request("turn/steer", {"threadId": self.mapper.thread_id, "expectedTurnId": self.turn_id, "input": text_input("\n\n".join(pending))})
        except Exception:
            for text in pending:  # the turn just ended: maybe the next one takes them
                notices.push(run_id, text)

    def _resume_prompt(self) -> str:
        self.interrupted = False
        self.mapper.open_tools.clear()
        return "\n\n".join(["[PlainBoard] Your running tool call was interrupted for this message, and its work may be half done.", *notices.drain(self.spec.run_id)])

    async def run(self) -> AsyncIterator[RunEvent]:
        prompt = self.spec.prompt
        while True:
            params: dict[str, Any] = {"threadId": self.mapper.thread_id, "input": text_input(prompt), "model": self.spec.model}
            if self.spec.effort:
                params["effort"] = self.spec.effort
            self.turn_id = (await self.server.request("turn/start", params))["turn"]["id"]
            status, error = "failed", None
            while True:
                try:
                    message = await self.server.next(timeout=NOTICE_CHECK_SECONDS)
                except TimeoutError:
                    await self._deliver_notices()
                    continue
                if message is None:
                    raise CodexError(f"Codex exited during the turn\n{self.server.stderr()}".strip())
                if "id" in message:
                    await answer_server_request(self.server, message)
                    continue
                method, payload = message.get("method", ""), message.get("params") or {}
                for event in self.mapper.map(method, payload):
                    yield event
                if method == "item/completed":
                    await self._deliver_notices()
                elif method == "turn/completed" and (payload.get("turn") or {}).get("id") == self.turn_id:
                    turn = payload["turn"]
                    status, error = turn.get("status", "failed"), (turn.get("error") or {}).get("message")
                    break
            if status == "interrupted" and self.interrupted:
                prompt = self._resume_prompt()  # the turn goes on with the waiting messages
                continue
            yield self.mapper.result(status, error)
            return


class DeviceLogin:
    """A ChatGPT sign-in with a device code, kept alive in the background until it completes."""

    def __init__(self) -> None:
        self.server: AppServer | None = None
        self.task: asyncio.Task[None] | None = None
        self.url: str | None = None
        self.code: str | None = None
        self.error: str | None = None
        self.started = 0.0

    @property
    def active(self) -> bool:
        return bool(self.task and not self.task.done())

    async def start(self) -> None:
        await self.cancel()
        self.error = None
        server = AppServer(SUBSCRIPTION_HOME)
        await server.__aenter__()
        try:
            started = await server.request("account/login/start", {"type": "chatgptDeviceCode"})
        except BaseException:
            await server.close()
            raise
        self.server, self.url, self.code, self.started = server, started.get("verificationUrl"), started.get("userCode"), time.time()
        self.task = asyncio.create_task(self._wait(server, started.get("loginId")))

    async def _wait(self, server: AppServer, login_id: str | None) -> None:
        try:
            while True:
                message = await server.next(timeout=LOGIN_TIMEOUT_SECONDS)
                if message is None:
                    self.error = "Codex exited before the sign-in finished"
                    return
                if message.get("method") == "account/login/completed" and (message.get("params") or {}).get("loginId") in (None, login_id):
                    params = message["params"]
                    if not params.get("success"):
                        self.error = params.get("error") or "Sign-in failed"
                    return
        except TimeoutError:
            self.error = "The sign-in code expired"
        finally:
            self.url = self.code = None
            await server.close()

    async def cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(BaseException):
                await self.task
        elif self.server:
            await self.server.close()
        self.server = self.task = None
        self.url = self.code = None


class CodexProvider(Provider):
    name = "codex"

    def __init__(self) -> None:
        self._models: tuple[float, list[ModelInfo]] | None = None
        self._login = DeviceLogin()
        self._api_key_lock = asyncio.Lock()

    def info(self) -> ProviderInfo:
        models = self._models[1] if self._models else MODELS
        return ProviderInfo(name=self.name, label="Codex", models=models, efforts=EFFORTS)

    async def catalog(self) -> ProviderInfo:
        if not self._models or time.monotonic() - self._models[0] > MODELS_TTL_SECONDS:
            try:
                async with AppServer(SUBSCRIPTION_HOME) as server:
                    listed = (await server.request("model/list", {}, timeout=20)).get("data") or []
                known = {m.id: m for m in MODELS}
                models = [known.get(m["id"]) or ModelInfo(id=m["id"], label=m.get("displayName") or m["id"], tier=guess_tier(m["id"])) for m in listed if not m.get("hidden")]
                self._models = (time.monotonic(), models or MODELS)
            except Exception as exc:
                log.warning("Could not list Codex models: %s", exc)
                self._models = (time.monotonic() - MODELS_TTL_SECONDS + 300, self._models[1] if self._models else MODELS)  # retry in 5 minutes
        return self.info()

    async def _home(self, creds: Credentials) -> Path:
        """The Codex home for these credentials. An API key is signed in to its own home."""
        if creds.codex_auth_mode != "api_key":
            return SUBSCRIPTION_HOME
        if not creds.openai_api_key:
            raise RuntimeError("OpenAI API key is not configured (Settings → Codex).")
        marker = API_KEY_HOME / ".plainboard-key"
        digest = hashlib.sha256(creds.openai_api_key.encode()).hexdigest()
        async with self._api_key_lock:
            if not marker.is_file() or marker.read_text().strip() != digest:
                async with AppServer(API_KEY_HOME) as server:
                    await server.request("account/login/start", {"type": "apiKey", "apiKey": creds.openai_api_key})
                marker.write_text(digest)
        return API_KEY_HOME

    async def run(self, spec: RunSpec, bridge: ToolBridge) -> AsyncIterator[RunEvent]:
        home = await self._home(spec.credentials)
        bridge_file = BRIDGE_DIR / f"{uuid.uuid4().hex}.json"
        write_bridge_file(bridge_file, spec.bridge, bridge.workspace)
        env = {**tool_env(spec.credentials), **spec.env}
        server = AppServer(home, env=env, config=run_config(spec, bridge_file), cwd=bridge.workspace)
        try:
            async with server:
                thread = {
                    "cwd": str(bridge.workspace),
                    "model": spec.model,
                    "sandbox": "danger-full-access",  # the runner container is the sandbox
                    "approvalPolicy": "never",
                    "developerInstructions": spec.system_prompt,
                }
                if spec.resume_session_id:
                    try:
                        opened = await server.request("thread/resume", {"threadId": spec.resume_session_id, "excludeTurns": True, **thread})
                    except CodexError as exc:
                        if "no rollout found" in str(exc).lower():
                            raise CodexError(f"Codex session not found: {spec.resume_session_id}") from exc
                        raise
                else:
                    opened = await server.request("thread/start", {**thread, "serviceName": "plainboard"})
                mapper = EventMapper(opened["thread"]["id"])
                yield SessionEvent(session_id=mapper.thread_id)
                async for event in TurnRunner(server, mapper, spec).run():
                    yield event
        except (CodexError, TimeoutError) as exc:
            raise RuntimeError(f"{exc}\n{server.stderr()}".strip()) from exc
        finally:
            bridge_file.unlink(missing_ok=True)

    async def complete(self, spec: CompletionSpec) -> CompletionResult:
        home = await self._home(spec.credentials)
        texts: list[str] = []
        with tempfile.TemporaryDirectory(prefix="plainboard-complete-") as cwd:
            server = AppServer(home, config=COMPLETION_CONFIG, cwd=Path(cwd))
            try:
                async with server:
                    opened = await server.request(
                        "thread/start",
                        {"cwd": cwd, "model": spec.model, "sandbox": "read-only", "approvalPolicy": "never", "baseInstructions": spec.system_prompt, "ephemeral": True},
                    )
                    mapper = EventMapper(opened["thread"]["id"])
                    params: dict[str, Any] = {"threadId": mapper.thread_id, "input": text_input(spec.prompt)}
                    if spec.effort:
                        params["effort"] = spec.effort
                    turn_id = (await server.request("turn/start", params))["turn"]["id"]
                    while True:
                        message = await server.next(timeout=600)
                        if message is None:
                            raise CodexError("Codex exited")
                        if "id" in message:
                            await answer_server_request(server, message)
                            continue
                        method, payload = message.get("method", ""), message.get("params") or {}
                        for event in mapper.map(method, payload):
                            if event.type == "assistant_text":
                                texts.append(event.text)
                        if method == "turn/completed" and (payload.get("turn") or {}).get("id") == turn_id:
                            turn = payload["turn"]
                            if turn.get("status") != "completed":
                                raise CodexError(f"Completion failed: {(turn.get('error') or {}).get('message') or mapper.error or turn.get('status')}")
                            break
            except (CodexError, TimeoutError) as exc:
                raise RuntimeError(f"{exc}\n{server.stderr()}".strip()) from exc
        usage = mapper.usage()
        return CompletionResult(
            text=(mapper.final_text or "\n".join(texts)).strip(),
            input_tokens=usage["input_tokens"] + usage["cache_read_tokens"],
            output_tokens=usage["output_tokens"],
        )

    async def limits(self, credentials: Credentials) -> list[LimitWindow]:
        if credentials.codex_auth_mode == "api_key" or not (SUBSCRIPTION_HOME / "auth.json").is_file():
            return []
        async with AppServer(SUBSCRIPTION_HOME) as server:
            if not (await server.request("account/read", {"refreshToken": False})).get("account"):
                return []
            answer = await server.request("account/rateLimits/read", None, timeout=30)
        return limit_windows((answer or {}).get("rateLimits"))

    async def account(self) -> ProviderAccount:
        state = ProviderAccount(login_url=self._login.url, login_code=self._login.code, login_error=self._login.error)
        if self._login.active:
            return state
        async with AppServer(SUBSCRIPTION_HOME) as server:
            account = (await server.request("account/read", {"refreshToken": False})).get("account") or {}
        if account.get("type") == "chatgpt":
            state.signed_in = True
            state.detail = " · ".join(str(p) for p in (account.get("email"), account.get("planType")) if p)
        elif account.get("type"):
            state.signed_in, state.detail = True, str(account["type"])
        return state

    async def login(self) -> ProviderAccount:
        await self._login.start()
        return ProviderAccount(login_url=self._login.url, login_code=self._login.code)

    async def logout(self) -> ProviderAccount:
        await self._login.cancel()
        async with AppServer(SUBSCRIPTION_HOME) as server:
            await server.request("account/logout", None)
        return ProviderAccount()
