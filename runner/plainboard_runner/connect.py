"""An external runner: on another machine, it connects out to PlainBoard (``PLAINBOARD_URL``, with
``PLAINBOARD_RUNNER_TOKEN``) and answers what the app sends through that one WebSocket, so nothing
listens on this machine and it needs no TLS or DNS of its own (wire format: plainboard_protocol/tunnel.py).

Each request goes to this runner's own API on 127.0.0.1 (or, with a port, to a server an agent runs
here: a preview), each socket likewise (the browser window, a preview's hot reload). The line breaks
off (or nothing comes through it for ``DEAD_SECONDS``: half-open): the requests and sockets in flight
end, but turns, jobs and servers go on (a turn's stream is resumed, runlog.py), and it connects again,
waiting longer each time (up to a minute, with jitter; much longer after PlainBoard refused it). Agents'
tool calls go straight to ``PLAINBOARD_URL``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import time
import uuid
from pathlib import Path

import httpx
from plainboard_protocol.tunnel import BYTES, CLOSE_PROTOCOL, CLOSE_REFUSED, CLOSE_REPLACED, DATA, DEAD_SECONDS, HOP_HEADERS, PING_SECONDS, PROTOCOL, TEXT, pack, unpack
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

log = logging.getLogger("plainboard.runner.connect")

APP_URL = os.environ.get("PLAINBOARD_URL", "").strip().rstrip("/")
TOKEN = os.environ.get("PLAINBOARD_RUNNER_TOKEN", "").strip()
OWN_PORT = int(os.environ.get("PLAINBOARD_RUNNER_PORT", "8090"))
COMMIT_FILE = Path(os.environ.get("PLAINBOARD_COMMIT_FILE", "/etc/plainboard-commit"))
MAX_BACKOFF = 60.0
# After PlainBoard said no, come back slowly: a wrong token or an old version stays so until someone
# changes it, and another process with this token took over on purpose.
REFUSED_BACKOFF = 300.0
REPLACED_BACKOFF = 600.0
INSTANCE = uuid.uuid4().hex  # new with every start: PlainBoard tells a restart from a reconnect by it


def enabled() -> bool:
    return bool(APP_URL and TOKEN)


def socket_url(app_url: str) -> str:
    scheme, _, rest = app_url.partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}/api/runners/connect"


def commit() -> str:
    try:
        return COMMIT_FILE.read_text().strip()
    except OSError:
        return ""


class Session:
    """One connection: the requests and sockets the app opened through it."""

    def __init__(self, ws: ClientConnection, own_port: int = OWN_PORT, client: httpx.AsyncClient | None = None) -> None:
        self._ws = ws
        self._own_port = own_port
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10), follow_redirects=False)
        self._lock = asyncio.Lock()
        self._tasks: dict[int, asyncio.Task] = {}
        self._inbox: dict[int, asyncio.Queue] = {}  # request bodies and socket messages from the app
        self.heard = time.monotonic()
        self.dead_seconds = DEAD_SECONDS

    async def _send(self, frame: str | bytes) -> None:
        async with self._lock:
            await self._ws.send(frame)

    async def _json(self, message: dict) -> None:
        await self._send(json.dumps(message))

    def _port(self, port: int | None) -> int:
        return self._own_port if port is None else int(port)

    async def run(self) -> None:
        await self._json({"t": "hello", "version": commit(), "protocol": PROTOCOL, "instance": INSTANCE})
        pinger = asyncio.create_task(self._ping())
        reader = asyncio.create_task(self._read())
        try:
            # Whichever ends first: the line (closed, broken) or the ping (nothing heard: half-open).
            await asyncio.wait({pinger, reader}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (pinger, reader, *self._tasks.values()):
                task.cancel()
            await asyncio.gather(pinger, reader, *self._tasks.values(), return_exceptions=True)
            await self._client.aclose()

    async def _read(self) -> None:
        async for frame in self._ws:
            self.heard = time.monotonic()
            self._dispatch(frame)

    async def _ping(self) -> None:
        while True:
            await asyncio.sleep(min(PING_SECONDS, self.dead_seconds / 3))
            if time.monotonic() - self.heard > self.dead_seconds:
                log.warning("Nothing heard from PlainBoard for %.0f s: the line is taken as gone", self.dead_seconds)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._ws.close(), 5)
                return
            await self._json({"t": "ping"})

    def _dispatch(self, frame: str | bytes) -> None:
        if isinstance(frame, bytes):
            stream_id, kind, payload = unpack(frame)
            if (queue := self._inbox.get(stream_id)) is not None:
                queue.put_nowait((kind, payload))
            return
        message = json.loads(frame)
        t, stream_id = message.get("t"), message.get("id")
        if t in ("req", "ws"):
            self._inbox[stream_id] = asyncio.Queue()
            handler = self._request if t == "req" else self._socket
            self._tasks[stream_id] = asyncio.create_task(handler(stream_id, message))
        elif t == "end" and (queue := self._inbox.get(stream_id)) is not None:
            queue.put_nowait(("end", None))
        elif t == "close" and (queue := self._inbox.get(stream_id)) is not None:
            queue.put_nowait(("close", None))
        elif t == "cancel" and (task := self._tasks.get(stream_id)) is not None:
            task.cancel()

    def _done(self, stream_id: int) -> None:
        self._tasks.pop(stream_id, None)
        self._inbox.pop(stream_id, None)

    async def _request(self, stream_id: int, message: dict) -> None:
        queue = self._inbox[stream_id]

        async def body():
            while True:
                kind, payload = await queue.get()
                if kind != DATA:
                    return
                yield payload

        headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in HOP_HEADERS]
        url = f"http://127.0.0.1:{self._port(message.get('port'))}{message.get('path') or '/'}"
        try:
            # A body of a known length is passed on as it comes; one without (nearly always none) is read first.
            if any(k.lower() == "content-length" for k, _ in headers):
                content = body()
            else:
                content = b"".join([chunk async for chunk in body()])
            request = self._client.build_request(message.get("method", "GET"), url, headers=headers, content=content)
            try:
                response = await self._client.send(request, stream=True)
            except httpx.ConnectError as exc:
                await self._json({"t": "error", "id": stream_id, "message": f"Nothing answers there: {exc}", "connect": True})
                return
            try:
                await self._json({"t": "res", "id": stream_id, "status": response.status_code, "headers": response.headers.multi_items()})
                async for chunk in response.aiter_raw():
                    await self._send(pack(stream_id, DATA, chunk))
                await self._json({"t": "end", "id": stream_id})
            finally:
                await response.aclose()
        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            pass
        except Exception as exc:  # the local server broke off
            try:
                await self._json({"t": "error", "id": stream_id, "message": str(exc)[:300] or type(exc).__name__})
            except ConnectionClosed:
                pass
        finally:
            self._done(stream_id)

    async def _socket(self, stream_id: int, message: dict) -> None:
        queue = self._inbox[stream_id]
        port = self._port(message.get("port"))
        headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != "origin"]
        origin = next((v for k, v in message.get("headers", []) if k.lower() == "origin"), None)
        try:
            try:
                local = await connect(
                    f"ws://localhost:{port}{message.get('path') or '/'}",
                    host="127.0.0.1",
                    port=port,
                    origin=origin,
                    additional_headers=headers,
                    subprotocols=message.get("subprotocols") or None,
                    open_timeout=10,
                    max_size=None,
                    ping_interval=None,
                    compression=None,
                )
            except (OSError, TimeoutError, InvalidHandshake) as exc:
                await self._json({"t": "error", "id": stream_id, "message": str(exc)[:300] or "The socket did not open", "connect": True})
                return
            async with local:
                await self._json({"t": "ws_ok", "id": stream_id, "subprotocol": local.subprotocol})

                async def up() -> None:
                    while True:
                        kind, payload = await queue.get()
                        if kind == TEXT:
                            await local.send(payload.decode(errors="replace"))
                        elif kind == BYTES:
                            await local.send(payload)
                        else:
                            return

                async def down() -> None:
                    async for item in local:
                        await self._send(pack(stream_id, TEXT if isinstance(item, str) else BYTES, item.encode() if isinstance(item, str) else item))

                tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            await self._json({"t": "close", "id": stream_id, "code": 1000})
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        finally:
            self._done(stream_id)


def backoff_after(code: int | None, backoff: float) -> float:
    """How long to wait before connecting again, after the app closed the line with ``code``."""
    if code == CLOSE_REFUSED:
        log.error("PlainBoard refused this runner's token (unknown or revoked): check PLAINBOARD_RUNNER_TOKEN. Trying again in %.0f min", REFUSED_BACKOFF / 60)
        return REFUSED_BACKOFF
    if code == CLOSE_REPLACED:
        log.error("Another runner connected with this token and took over. Trying again in %.0f min", REPLACED_BACKOFF / 60)
        return REPLACED_BACKOFF
    if code == CLOSE_PROTOCOL:
        log.error("PlainBoard speaks another tunnel protocol: update this runner (docker pull, then start it again)")
        return REFUSED_BACKOFF
    return backoff


async def serve(app_url: str = APP_URL, token: str = TOKEN, own_port: int = OWN_PORT) -> None:
    """Stay connected to PlainBoard, for ever: again after every break, waiting longer each time (with jitter)."""
    url = socket_url(app_url)
    backoff = 1.0
    while True:
        wait = backoff
        try:
            async with connect(url, additional_headers=[("Authorization", f"Bearer {token}")], max_size=None, open_timeout=20, ping_interval=20, ping_timeout=20, compression=None) as ws:
                log.info("Connected to %s", app_url)
                backoff = wait = 1.0
                await Session(ws, own_port).run()
            log.info("Connection to %s ended (%s)", app_url, ws.close_code)
            wait = backoff_after(ws.close_code, wait)
        except asyncio.CancelledError:
            raise
        except InvalidStatus as exc:
            refused = exc.response.status_code in (401, 403)
            log.warning("PlainBoard refused the connection (%s)", exc.response.status_code)
            wait = backoff_after(CLOSE_REFUSED, wait) if refused else wait
        except (OSError, TimeoutError, InvalidHandshake, ConnectionClosed) as exc:
            log.warning("Can't reach %s: %s", app_url, exc)
        except Exception:
            log.exception("Connection to %s failed", app_url)
        await asyncio.sleep(wait * random.uniform(0.8, 1.2))
        backoff = min(max(backoff, 1.0) * 2, MAX_BACKOFF)
