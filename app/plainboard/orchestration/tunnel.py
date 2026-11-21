"""The app's end of an external runner's tunnel (the wire format: plainboard_protocol/tunnel.py).

The runner connected out to the app; everything the app asks of it goes back through that one
socket: ``TunnelTransport`` makes it an httpx transport, so ``RunnerClient`` and the preview proxy
work as with the built-in runner, and ``Tunnel.open_socket`` gives what ``relay`` needs for the
browser window and previews' sockets. Errors are the ones a network would raise: not connected is a
``ConnectError`` (nothing reached the runner), a line that broke off mid-way a ``ReadError``."""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
from plainboard_protocol.tunnel import BYTES, CHUNK, DATA, HOP_HEADERS, TEXT, pack, unpack
from websockets.exceptions import ConnectionClosedError

LOST = ("lost", None)


class TunnelClosed(ConnectionError):
    pass


async def _next(queue: asyncio.Queue, timeout: float | None) -> tuple[Any, Any]:
    if timeout is None:
        return await queue.get()
    return await asyncio.wait_for(queue.get(), timeout)


class Tunnel:
    """One connection of one runner. ``send`` writes a frame (text or bytes) to its socket; what
    comes from the runner is given to ``feed``."""

    def __init__(self, send: Callable[[str | bytes], Awaitable[None]], runner_id: int = 0) -> None:
        self.runner_id = runner_id
        self._send_frame = send
        self._lock = asyncio.Lock()
        self._streams: dict[int, asyncio.Queue] = {}
        self._ids = itertools.count(1)
        self.closed = False
        self.heard = time.monotonic()
        self.ended = asyncio.Event()  # set by close(): the socket's loop ends it with ``close_code``
        self.close_code = 1000

    async def _send(self, frame: str | bytes) -> None:
        if self.closed:
            raise TunnelClosed("The runner is not connected")
        try:
            async with self._lock:
                await self._send_frame(frame)
        except Exception as exc:  # the socket is gone
            raise TunnelClosed(f"The runner's connection broke off: {exc}") from exc

    async def send_json(self, message: dict) -> None:
        await self._send(json.dumps(message))

    async def send_data(self, stream_id: int, kind: int, data: bytes) -> None:
        await self._send(pack(stream_id, kind, data))

    def feed(self, frame: str | bytes) -> None:
        self.heard = time.monotonic()
        if isinstance(frame, bytes):
            stream_id, kind, payload = unpack(frame)
            if (queue := self._streams.get(stream_id)) is not None:
                queue.put_nowait((kind, payload))
            return
        message = json.loads(frame)
        if (queue := self._streams.get(message.get("id"))) is not None:  # type: ignore[arg-type]
            queue.put_nowait(("msg", message))

    def close(self, code: int = 1000) -> None:
        """The socket ended, or is to end (``code``, see plainboard_protocol.tunnel): everything still
        waiting for the runner hears it."""
        if not self.closed:
            self.close_code = code
        self.closed = True
        self.ended.set()
        for queue in self._streams.values():
            queue.put_nowait(LOST)
        self._streams.clear()

    def _open(self) -> tuple[int, asyncio.Queue]:
        stream_id, queue = next(self._ids), asyncio.Queue()
        self._streams[stream_id] = queue
        return stream_id, queue

    def _drop(self, stream_id: int) -> None:
        self._streams.pop(stream_id, None)

    async def request(
        self, method: str, port: int | None, path: str, headers: list[tuple[str, str]], body: AsyncIterable[bytes], timeout: float | None, request: httpx.Request | None = None
    ) -> httpx.Response:
        stream_id, queue = self._open()
        try:
            await self.send_json({"t": "req", "id": stream_id, "method": method, "port": port, "path": path, "headers": headers})
        except TunnelClosed as exc:
            self._drop(stream_id)
            raise httpx.ConnectError(str(exc), request=request) from exc
        try:
            async for chunk in body:
                for start in range(0, len(chunk), CHUNK):
                    await self.send_data(stream_id, DATA, bytes(chunk[start : start + CHUNK]))
            await self.send_json({"t": "end", "id": stream_id})
            kind, message = await _next(queue, timeout)
        except TunnelClosed as exc:
            self._drop(stream_id)
            raise httpx.ReadError(str(exc), request=request) from exc
        except TimeoutError as exc:
            await self._cancel(stream_id)
            raise httpx.ReadTimeout("The runner did not answer in time", request=request) from exc
        except BaseException:
            await self._cancel(stream_id)
            raise
        if (kind, message) == LOST:
            raise httpx.ReadError("The runner's connection broke off", request=request)
        if message.get("t") == "error":
            self._drop(stream_id)
            error = httpx.ConnectError if message.get("connect") else httpx.ReadError
            raise error(message.get("message") or "The runner could not answer", request=request)
        return httpx.Response(
            int(message["status"]), headers=[(k, v) for k, v in message.get("headers", [])], stream=_Body(self, stream_id, queue, timeout), request=request
        )

    async def _cancel(self, stream_id: int) -> None:
        if stream_id in self._streams:
            self._drop(stream_id)
            try:
                await self.send_json({"t": "cancel", "id": stream_id})
            except TunnelClosed:
                pass

    async def open_socket(self, port: int | None, path: str, headers: list[tuple[str, str]] | None = None, subprotocols: list[str] | None = None, timeout: float = 30) -> TunnelSocket:
        """A WebSocket on the runner's machine (its browser, a preview's server). Raises OSError if it can't be opened."""
        stream_id, queue = self._open()
        try:
            await self.send_json({"t": "ws", "id": stream_id, "port": port, "path": path, "headers": headers or [], "subprotocols": subprotocols or []})
            kind, message = await _next(queue, timeout)
        except (TunnelClosed, TimeoutError) as exc:
            await self._cancel(stream_id)
            raise OSError(f"The runner's socket did not open: {exc}") from exc
        if (kind, message) == LOST or message.get("t") != "ws_ok":
            self._drop(stream_id)
            raise OSError((message or {}).get("message") or "The runner's socket did not open")
        return TunnelSocket(self, stream_id, queue, message.get("subprotocol"))


class _Body(httpx.AsyncByteStream):
    def __init__(self, tunnel: Tunnel, stream_id: int, queue: asyncio.Queue, timeout: float | None) -> None:
        self._tunnel, self._id, self._queue, self._timeout = tunnel, stream_id, queue, timeout
        self._done = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while not self._done:
            try:
                kind, payload = await _next(self._queue, self._timeout)
            except TimeoutError as exc:
                raise httpx.ReadTimeout("The runner did not answer in time") from exc
            if kind == DATA:
                yield payload
            elif (kind, payload) == LOST:
                self._done = True
                raise httpx.ReadError("The runner's connection broke off")
            elif kind == "msg" and payload.get("t") == "end":
                self._done = True
                self._tunnel._drop(self._id)
            elif kind == "msg" and payload.get("t") == "error":
                self._done = True
                self._tunnel._drop(self._id)
                raise httpx.ReadError(payload.get("message") or "The runner's answer broke off")

    async def aclose(self) -> None:
        if not self._done:
            self._done = True
            await self._tunnel._cancel(self._id)


class TunnelSocket:
    """A WebSocket through the tunnel, as ``relay`` uses one of the websockets library."""

    def __init__(self, tunnel: Tunnel, stream_id: int, queue: asyncio.Queue, subprotocol: str | None) -> None:
        self._tunnel, self._id, self._queue = tunnel, stream_id, queue
        self.subprotocol = subprotocol
        self._closed = False

    async def send(self, message: str | bytes) -> None:
        if self._closed:
            raise ConnectionClosedError(None, None)
        try:
            if isinstance(message, str):
                await self._tunnel.send_data(self._id, TEXT, message.encode())
            else:
                await self._tunnel.send_data(self._id, BYTES, bytes(message))
        except TunnelClosed as exc:
            self._closed = True
            raise ConnectionClosedError(None, None) from exc

    async def __aiter__(self) -> AsyncIterator[str | bytes]:
        while not self._closed:
            kind, payload = await self._queue.get()
            if kind == TEXT:
                yield payload.decode(errors="replace")
            elif kind == BYTES:
                yield payload
            else:  # closed by the other side, or the line is gone
                self._closed = True
                self._tunnel._drop(self._id)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._tunnel._drop(self._id)
            try:
                await self._tunnel.send_json({"t": "close", "id": self._id, "code": 1000})
            except TunnelClosed:
                pass

    async def __aenter__(self) -> TunnelSocket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class TunnelTransport(httpx.AsyncBaseTransport):
    """httpx over a runner's tunnel. The URL's port is a port on the runner's machine (a preview);
    none is the runner's own API. ``tunnel`` gives the runner's connection right now, or None."""

    def __init__(self, tunnel: Callable[[], Tunnel | None]) -> None:
        self._tunnel = tunnel

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        tunnel = self._tunnel()
        if tunnel is None or tunnel.closed:
            raise httpx.ConnectError("The runner is not connected", request=request)
        timeout = (request.extensions.get("timeout") or {}).get("read")
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in request.headers.raw if k.decode("latin-1").lower() not in HOP_HEADERS]
        body = request.stream if isinstance(request.stream, AsyncIterable) else _empty()
        return await tunnel.request(request.method, request.url.port, request.url.raw_path.decode("ascii"), headers, body, timeout, request)


async def _empty() -> AsyncIterator[bytes]:
    return
    yield b""
