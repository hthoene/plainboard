"""Messages for a running turn. Providers pick them up between the agent's steps."""

from __future__ import annotations

_pending: dict[str, list[str]] = {}


def push(run_id: str, text: str) -> None:
    _pending.setdefault(run_id, []).append(text)


def drain(run_id: str) -> list[str]:
    return _pending.pop(run_id, [])
