"""In-process pub/sub for live UI updates (one channel per project)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[int, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def subscribe(self, project_id: int) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers[project_id].add(queue)
        return queue

    def unsubscribe(self, project_id: int, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers[project_id].discard(queue)

    def publish(self, project_id: int, type_: str, data: BaseModel | dict[str, Any] | None = None) -> None:
        payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else (data or {})
        event = {"type": type_, "data": payload}
        for queue in list(self._subscribers.get(project_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # slow client: drop, it will resync on reconnect
                log.warning("Dropping event for slow subscriber (project %s)", project_id)


bus = EventBus()
