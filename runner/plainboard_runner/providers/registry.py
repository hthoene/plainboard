"""Provider registry. To add a provider (e.g. OpenAI): implement ``Provider`` and register it here."""

from __future__ import annotations

from .base import Provider

_PROVIDERS: dict[str, Provider] = {}


def register_provider(provider: Provider) -> None:
    _PROVIDERS[provider.name] = provider


def get_provider(name: str) -> Provider:
    try:
        return _PROVIDERS[name]
    except KeyError:
        raise KeyError(f"Unknown provider '{name}'. Available: {', '.join(_PROVIDERS)}") from None


def list_providers() -> list[Provider]:
    return list(_PROVIDERS.values())


def _register_builtin() -> None:
    from .claude_code import ClaudeCodeProvider
    from .mock import MockProvider

    register_provider(ClaudeCodeProvider())
    register_provider(MockProvider())


_register_builtin()
