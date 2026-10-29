from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from plainboard_protocol import CheckResult, CompletionResult, CompletionSpec, ProviderInfo, RunEvent, RunSpec

from ..tool_bridge import ToolBridge


class Provider(ABC):
    """An LLM backend able to run agentic turns.

    Implementations translate a :class:`RunSpec` into provider calls and yield normalized
    :data:`RunEvent` objects. Everything provider-specific (auth env, SDK types, tool wiring)
    stays inside the implementation.
    """

    name: str

    @abstractmethod
    def info(self) -> ProviderInfo: ...

    @abstractmethod
    def run(self, spec: RunSpec, bridge: ToolBridge) -> AsyncIterator[RunEvent]:
        """Execute one agent turn (may include many tool calls). Must end with a ResultEvent."""

    @abstractmethod
    async def complete(self, spec: CompletionSpec) -> CompletionResult:
        """Plain tool-less completion (used for summaries)."""

    async def check(self, spec: CompletionSpec) -> CheckResult:
        try:
            result = await self.complete(spec)
            return CheckResult(ok=True, detail=f"{spec.model}: {result.text.strip()[:80]}")
        except Exception as exc:
            return CheckResult(ok=False, detail=str(exc)[:500])
