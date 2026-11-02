"""Messages for a running turn. Providers pick them up between the agent's steps."""

from __future__ import annotations

import time

_pending: dict[str, list[str]] = {}
_urgent_since: dict[str, float] = {}


def push(run_id: str, text: str, urgent: bool = False) -> None:
    _pending.setdefault(run_id, []).append(text)
    if urgent:
        _urgent_since.setdefault(run_id, time.monotonic())


def drain(run_id: str) -> list[str]:
    _urgent_since.pop(run_id, None)
    return _pending.pop(run_id, [])


def urgent_waiting(run_id: str) -> float:
    """Seconds an urgent message has been waiting to be picked up; 0 if there is none."""
    since = _urgent_since.get(run_id)
    return time.monotonic() - since if since is not None else 0.0
