from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path

from plainboard_protocol import CheckResult, CompletionResult, CompletionSpec, Credentials, LimitWindow, ModelInfo, ProviderAccount, ProviderInfo, RunEvent, RunSpec

from ..tool_bridge import ToolBridge


class ProviderError(RuntimeError):
    """A failed turn whose text already names the error that caused it."""


def merge_efforts(known: list[str], *more: list[str]) -> list[str]:
    """``known`` with the levels the other lists add, each after the level it follows there (``ultra`` after ``max``)."""
    out = list(known)
    for levels in more:
        for i, level in enumerate(levels):
            if level and level not in out:
                out.insert(out.index(levels[i - 1]) + 1 if i and levels[i - 1] in out else len(out), level)
    return out


class Provider(ABC):
    """An LLM backend able to run agentic turns.

    Implementations translate a :class:`RunSpec` into provider calls and yield normalized
    :data:`RunEvent` objects. Everything provider-specific (auth env, SDK types, tool wiring)
    stays inside the implementation.
    """

    name: str
    # The provider always reports all limit windows of an account at once (see LimitsEvent.complete).
    limits_complete: bool = False

    @abstractmethod
    def info(self) -> ProviderInfo: ...

    async def catalog(self) -> ProviderInfo:
        """Like :meth:`info`, but may ask the provider which models the account offers."""
        return self.info()

    def skills_folder(self, spec: RunSpec, workspace: Path) -> Path | None:
        """Where the turn's skills (``spec.skills``) go so the provider loads them; None: it has no skills."""
        return None

    @abstractmethod
    def run(self, spec: RunSpec, bridge: ToolBridge) -> AsyncIterator[RunEvent]:
        """Execute one agent turn (may include many tool calls). Must end with a ResultEvent."""

    @abstractmethod
    async def complete(self, spec: CompletionSpec) -> CompletionResult:
        """Plain tool-less completion (used for summaries)."""

    async def models(self, credentials: Credentials) -> list[str]:
        """The model ids the account in ``credentials`` is offered, asked from the provider. Empty: it has no such list."""
        return []

    async def limits(self, credentials: Credentials) -> list[LimitWindow]:
        """The account's plan limits, without using the model. Empty if it has none or they are unknown."""
        return []

    async def offered(self, credentials: Credentials) -> list[ModelInfo] | None:
        """The models the account in ``credentials`` may use, as its last read of the limits found them (only once).
        None: not known, or the provider lists the same models for every account."""
        return None

    async def plan(self, credentials: Credentials) -> tuple[str | None, float | None]:
        """The account's plan: its name and its size as a multiple of the provider's smallest plan. None: not known."""
        return None, None

    async def account(self, key: str, own: bool = False) -> ProviderAccount | None:
        """The subscription signed in on the runner for account ``key``. None: the provider has no sign-in of its own.
        ``own``: a person's own sign-in (signins.py): only one in progress; a finished one waits for the app."""
        return None

    async def login(self, key: str, own: bool = False) -> ProviderAccount:
        raise NotImplementedError(f"{self.name} has no sign-in")

    async def logout(self, key: str, own: bool = False) -> ProviderAccount:
        raise NotImplementedError(f"{self.name} has no sign-in")

    async def login_code(self, key: str, code: str) -> ProviderAccount:
        """The code the provider's page showed after signing in, for an own sign-in in progress."""
        raise NotImplementedError(f"{self.name} takes no sign-in code")

    async def take_over(self, key: str) -> dict[str, str]:
        """The installation's sign-in of account ``key`` in the runner's home, for the app to keep (signins.py);
        none if it is not signed in there. Raises ``signins.Busy`` while it is in use."""
        raise NotImplementedError(f"{self.name} has no sign-in")

    async def refresh_sign_in(self, credentials: Credentials) -> None:
        """Refresh a kept sign-in (``login_files``) ahead of time where it is due soon, so the runners that use it
        get it fresh and none of them has to refresh it itself (two refreshes of one token: the second fails)."""

    async def check(self, spec: CompletionSpec) -> CheckResult:
        try:
            result = await self.complete(spec)
            return CheckResult(ok=True, detail=f"{spec.model}: {result.text.strip()[:80]}")
        except Exception as exc:
            return CheckResult(ok=False, detail=str(exc)[:500])
