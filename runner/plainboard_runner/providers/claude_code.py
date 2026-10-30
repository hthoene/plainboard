"""Claude Code provider built on the Claude Agent SDK (bundled Claude Code CLI).

Auth: a Claude subscription OAuth token (``claude setup-token``), an Anthropic API key,
or an interactive ``claude login`` performed inside the runner container.
"""

from __future__ import annotations

import collections
import logging
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk.types import StreamEvent
from plainboard_protocol import (
    AssistantTextEvent,
    CompactionEvent,
    CompletionResult,
    CompletionSpec,
    Credentials,
    McpServerSpec,
    ModelInfo,
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

from ..config import TOOL_RESULT_PREVIEW_CHARS
from ..tool_bridge import ToolBridge
from ..workspace import git_env
from .base import Provider

log = logging.getLogger(__name__)

MCP_SERVER_NAME = "plainboard"
BOARD_TOOLSET_BUILTINS = ["Read", "Glob", "Grep", "Bash", "Edit", "Write"]
# Interactive tools make no sense for headless agents; they talk back via chat or the board.
DISALLOWED_TOOLS = ["AskUserQuestion"]

MODELS = [
    ModelInfo(id="claude-opus-5-5", label="Claude Opus 5.5", tier="large"),
    ModelInfo(id="claude-sonnet-5-5", label="Claude Sonnet 5.5", tier="medium"),
    ModelInfo(id="claude-haiku-5-5", label="Claude Haiku 5.5", tier="small"),
    ModelInfo(id="claude-fable-5-1", label="Claude Fable 5.1", tier="xlarge"),
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def auth_env(creds: Credentials) -> dict[str, str]:
    env = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_AGENT_SDK_CLIENT_APP": "plainboard/0.1"}
    if creds.claude_auth_mode == "oauth_token":
        if not creds.claude_oauth_token:
            raise RuntimeError("Claude OAuth token is not configured (Settings → Claude).")
        env["CLAUDE_CODE_OAUTH_TOKEN"] = creds.claude_oauth_token
    elif creds.claude_auth_mode == "api_key":
        if not creds.anthropic_api_key:
            raise RuntimeError("Anthropic API key is not configured (Settings → Claude).")
        env["ANTHROPIC_API_KEY"] = creds.anthropic_api_key
    # "runner_login": rely on credentials stored by `claude login` in the runner's home volume.
    return env


def context_tokens_from_usage(usage: dict[str, Any] | None) -> int:
    if not usage:
        return 0
    return int(
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + (usage.get("output_tokens") or 0)
    )


def _tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
        elif isinstance(item, dict):
            parts.append(f"[{item.get('type', 'content')}]")
    return "\n".join(parts)


def external_mcp_config(server: McpServerSpec) -> dict[str, Any]:
    if server.transport == "stdio":
        return {"type": "stdio", "command": server.command or "", "args": server.args, "env": server.env}
    return {"type": server.transport, "url": server.url or "", "headers": server.headers}


def build_mcp_server(bridge: ToolBridge):
    tools = []
    for definition in bridge.tools:

        def make_handler(name: str):
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                result = await bridge.call(name, args)
                return {"content": [{"type": "text", "text": result.text}], "is_error": result.is_error}

            return handler

        tools.append(tool(definition.name, definition.description, definition.input_schema)(make_handler(definition.name)))
    return create_sdk_mcp_server(name=MCP_SERVER_NAME, version="1.0.0", tools=tools)


class EventMapper:
    """Translates Agent SDK messages into protocol events (kept separate for unit tests)."""

    def __init__(self) -> None:
        self.context_tokens = 0
        self.last_text: str | None = None

    def map(self, message: Any) -> list[RunEvent]:
        events: list[RunEvent] = []
        if isinstance(message, StreamEvent):
            event = message.event or {}
            delta = event.get("delta") or {}
            if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                events.append(TextDeltaEvent(text=delta.get("text", "")))
        elif isinstance(message, SystemMessage):
            if message.subtype == "init" and message.data.get("session_id"):
                events.append(SessionEvent(session_id=message.data["session_id"]))
            elif message.subtype == "compact_boundary":
                metadata = message.data.get("compact_metadata") or {}
                events.append(CompactionEvent(tokens_before=int(metadata.get("pre_tokens") or 0)))
        elif isinstance(message, AssistantMessage):
            if message.parent_tool_use_id:  # sub-agent chatter: keep out of the transcript
                return events
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    self.last_text = block.text
                    events.append(AssistantTextEvent(text=block.text))
                elif isinstance(block, ToolUseBlock):
                    events.append(ToolUseEvent(id=block.id, name=block.name, input=block.input or {}))
            tokens = context_tokens_from_usage(message.usage)
            if tokens:
                self.context_tokens = tokens
                events.append(UsageEvent(context_tokens=tokens))
        elif isinstance(message, UserMessage):
            if message.parent_tool_use_id or isinstance(message.content, str):
                return events
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    events.append(
                        ToolResultEvent(
                            tool_use_id=block.tool_use_id,
                            content=_tool_result_text(block.content)[:TOOL_RESULT_PREVIEW_CHARS],
                            is_error=bool(block.is_error),
                        )
                    )
        elif isinstance(message, ResultMessage):
            usage = message.usage or {}
            error = None
            if message.is_error:
                error = "; ".join(message.errors or []) or message.result or message.subtype
            events.append(
                ResultEvent(
                    session_id=message.session_id,
                    is_error=message.is_error,
                    error=error,
                    text=message.result if not message.is_error else self.last_text,
                    num_turns=message.num_turns,
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                    cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                    cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
                    session_cost_usd=float(message.total_cost_usd or 0.0),  # keeps growing across resumed turns
                    context_tokens=self.context_tokens,
                )
            )
        return events


class ClaudeCodeProvider(Provider):
    name = "claude_code"

    def info(self) -> ProviderInfo:
        return ProviderInfo(name=self.name, label="Claude Agent", models=MODELS, efforts=EFFORTS)

    def _options(self, spec: RunSpec, bridge: ToolBridge, stderr_tail: collections.deque[str]) -> ClaudeAgentOptions:
        env = {**auth_env(spec.credentials), **git_env(spec.credentials), **spec.env}
        mcp_servers: dict[str, Any] = {s.name: external_mcp_config(s) for s in spec.mcp_servers if s.name != MCP_SERVER_NAME}
        mcp_servers[MCP_SERVER_NAME] = build_mcp_server(bridge)
        allowed = [f"mcp__{MCP_SERVER_NAME}__{t.name}" for t in bridge.tools] + [f"mcp__{name}" for name in mcp_servers if name != MCP_SERVER_NAME]
        common: dict[str, Any] = dict(
            model=spec.model,
            cwd=str(bridge.workspace),
            env=env,
            permission_mode="bypassPermissions",
            resume=spec.resume_session_id,
            max_turns=spec.max_turns,
            mcp_servers=mcp_servers,
            allowed_tools=allowed,
            disallowed_tools=DISALLOWED_TOOLS,
            include_partial_messages=True,
            stderr=stderr_tail.append,
        )
        if spec.effort:
            common["effort"] = spec.effort
        if spec.toolset == "full":
            return ClaudeAgentOptions(
                tools={"type": "preset", "preset": "claude_code"},
                system_prompt={"type": "preset", "preset": "claude_code", "append": spec.system_prompt},
                setting_sources=["project"],
                **common,
            )
        return ClaudeAgentOptions(
            tools=BOARD_TOOLSET_BUILTINS,
            system_prompt=spec.system_prompt,
            setting_sources=[],
            **common,
        )

    async def run(self, spec: RunSpec, bridge: ToolBridge) -> AsyncIterator[RunEvent]:
        stderr_tail: collections.deque[str] = collections.deque(maxlen=30)
        mapper = EventMapper()
        got_result = False
        try:
            async with ClaudeSDKClient(options=self._options(spec, bridge, stderr_tail)) as client:
                await client.query(spec.prompt)
                async for message in client.receive_response():
                    for event in mapper.map(message):
                        got_result = got_result or event.type == "result"
                        yield event
        except Exception as exc:
            detail = "\n".join(stderr_tail)
            raise RuntimeError(f"{exc}\n{detail}".strip()) from exc
        if not got_result:
            yield ResultEvent(is_error=True, error="Claude Code ended without a result", context_tokens=mapper.context_tokens)

    async def complete(self, spec: CompletionSpec) -> CompletionResult:
        stderr_tail: collections.deque[str] = collections.deque(maxlen=30)
        options = ClaudeAgentOptions(
            model=spec.model,
            tools=[],
            system_prompt=spec.system_prompt,
            setting_sources=[],
            max_turns=1,
            cwd="/tmp",
            env=auth_env(spec.credentials),
            stderr=stderr_tail.append,
            **({"effort": spec.effort} if spec.effort else {}),
        )
        texts: list[str] = []
        result: ResultMessage | None = None
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(spec.prompt)
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        texts.extend(b.text for b in message.content if isinstance(b, TextBlock))
                    elif isinstance(message, ResultMessage):
                        result = message
        except Exception as exc:
            raise RuntimeError(f"{exc}\n" + "\n".join(stderr_tail)) from exc
        if result is None or result.is_error:
            reason = (result.result or "; ".join(result.errors or [])) if result else "no result"
            raise RuntimeError(f"Completion failed: {reason}")
        usage = result.usage or {}
        return CompletionResult(
            text="\n".join(texts).strip() or (result.result or ""),
            input_tokens=int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cost_usd=float(result.total_cost_usd or 0.0),
        )
