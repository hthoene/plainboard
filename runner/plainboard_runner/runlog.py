"""An external runner's turns outlive a broken line (connect.py): each turn's stream lines are kept
here, the last ``MAX_LINES`` / ``MAX_BYTES`` of them, so the app picks the stream up again where it
stopped reading (``GET /runs/<id>/events?after=<lines it has>``). That count is the acknowledgement:
nothing is sent twice and nothing is missed, as long as what was missed is still held.

A turn nobody follows for ``ORPHAN_SECONDS`` is cancelled (the app is not coming back for it); a finished
one is kept ``KEEP_SECONDS`` for a stream that is resumed late."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator

MAX_LINES = 20_000
MAX_BYTES = 8 * 1024 * 1024
ORPHAN_SECONDS = 600.0
KEEP_SECONDS = 600.0


class Gone(Exception):
    """The lines asked for are no longer held."""


class RunLog:
    def __init__(self, max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> None:
        self._lines: deque[str] = deque()
        self.first = 0  # the number of the oldest line held
        self._bytes = 0
        self._max_lines, self._max_bytes = max_lines, max_bytes
        self.done = False
        self.followers = 0
        self.since = time.monotonic()  # nobody follows / it ended, since then
        self._changed = asyncio.Event()

    @property
    def end(self) -> int:
        return self.first + len(self._lines)

    async def put(self, line: str | None) -> None:
        """As the queue ``_execute`` writes to: a line, or None at the end."""
        if line is None:
            self.done = True
            self.since = time.monotonic()
        else:
            self._lines.append(line)
            self._bytes += len(line)
            while len(self._lines) > 1 and (len(self._lines) > self._max_lines or self._bytes > self._max_bytes):
                self._bytes -= len(self._lines.popleft())
                self.first += 1
        self._changed.set()
        self._changed = asyncio.Event()

    def holds(self, after: int) -> bool:
        return self.first <= after <= self.end

    async def follow(self, after: int = 0) -> AsyncIterator[str]:
        """The lines from number ``after`` on, then each new one, until the turn ends."""
        if not self.holds(after):
            raise Gone()
        position = after
        self.followers += 1
        try:
            while True:
                changed = self._changed
                while position < self.end:
                    if position < self.first:
                        raise Gone()  # a follower too slow for the buffer
                    yield self._lines[position - self.first]
                    position += 1
                if self.done:
                    return
                await changed.wait()
        finally:
            self.followers -= 1
            self.since = time.monotonic()

    def orphaned(self, now: float) -> bool:
        return not self.done and self.followers == 0 and now - self.since >= ORPHAN_SECONDS

    def expired(self, now: float) -> bool:
        return self.done and self.followers == 0 and now - self.since >= KEEP_SECONDS
