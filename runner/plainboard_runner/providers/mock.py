"""Deterministic offline provider for development and end-to-end tests (no credentials needed).

Prompt directives (one per line) let tests drive tool calls:
    TOOL <name> <json-args>     -> calls the tool through the bridge
    SAY <text>                  -> emits <text> as assistant text
Context size grows with every turn of a session so compression can be exercised.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from plainboard_protocol import (
    AssistantTextEvent,
    CompletionResult,
    CompletionSpec,
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

from ..tool_bridge import ToolBridge
from .base import Provider


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class MockProvider(Provider):
    name = "mock"

    def __init__(self) -> None:
        self._sessions: dict[str, int] = {}

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            label="Mock (offline testing)",
            models=[ModelInfo(id="mock-large", label="Mock Large", tier="large"), ModelInfo(id="mock-small", label="Mock Small", tier="small")],
            efforts=["low", "high"],
        )

    async def run(self, spec: RunSpec, bridge: ToolBridge) -> AsyncIterator[RunEvent]:
        session_id = spec.resume_session_id if spec.resume_session_id in self._sessions else str(uuid.uuid4())
        context = self._sessions.get(session_id) or estimate_tokens(spec.system_prompt)
        yield SessionEvent(session_id=session_id)

        said = []
        for i, line in enumerate(spec.prompt.splitlines()):
            line = line.strip()
            if line.startswith("TOOL "):
                _, name, *rest = line.split(" ", 2)
                args = json.loads(rest[0]) if rest else {}
                tool_id = f"mock-{i}"
                yield ToolUseEvent(id=tool_id, name=name, input=args)
                result = await bridge.call(name, args)
                context += estimate_tokens(result.text)
                yield ToolResultEvent(tool_use_id=tool_id, content=result.text, is_error=result.is_error)
            elif line.startswith("SAY "):
                said.append(line[4:])

        text = " ".join(said) or f"Mock reply ({spec.model}) to: {spec.prompt.strip()[-200:]}"
        for word in text.split(" "):
            yield TextDeltaEvent(text=word + " ")
            await asyncio.sleep(0)
        yield AssistantTextEvent(text=text)

        context += estimate_tokens(spec.prompt) + estimate_tokens(text)
        self._sessions[session_id] = context
        yield UsageEvent(context_tokens=context)
        yield ResultEvent(
            session_id=session_id,
            text=text,
            num_turns=1,
            input_tokens=estimate_tokens(spec.prompt),
            output_tokens=estimate_tokens(text),
            context_tokens=context,
        )

    async def complete(self, spec: CompletionSpec) -> CompletionResult:
        lines = [ln for ln in spec.prompt.splitlines() if ln.strip()]
        text = f"MOCK SUMMARY of {len(lines)} lines. Last: {lines[-1][:120] if lines else ''}"
        return CompletionResult(text=text, input_tokens=estimate_tokens(spec.prompt), output_tokens=estimate_tokens(text))
