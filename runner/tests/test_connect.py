"""The runner's end of the tunnel (connect.py): requests and sockets of the app, answered locally."""

from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import FastAPI, Request
from plainboard_protocol.tunnel import DATA, pack, unpack

from plainboard_runner import connect

END = object()


class FakeSocket:
    """The WebSocket to PlainBoard: what the app sends comes from ``incoming``, what the runner sends lands in ``sent``."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list = []

    async def send(self, frame) -> None:
        self.sent.append(frame)

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.incoming.get()
        if frame is END:
            raise StopAsyncIteration
        return frame

    def messages(self, stream_id: int) -> list:
        out = []
        for frame in self.sent:
            if isinstance(frame, bytes):
                sid, _kind, data = unpack(frame)
                if sid == stream_id:
                    out.append(data)
            elif json.loads(frame).get("id") == stream_id:
                out.append(json.loads(frame))
        return out


def _local() -> FastAPI:
    app = FastAPI()
    app.state.hang = asyncio.Event()

    @app.get("/health")
    async def health(request: Request) -> dict:
        return {"status": "ok", "port": request.url.port}

    @app.post("/echo")
    async def echo(request: Request) -> dict:
        return {"body": (await request.body()).decode()}

    @app.get("/hang")
    async def hang() -> dict:
        await app.state.hang.wait()
        return {}

    return app


async def _until(predicate, seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


async def test_requests_of_the_app_are_answered_by_the_local_runner_and_end_with_the_line():
    ws = FakeSocket()
    session = connect.Session(ws, own_port=8090, client=httpx.AsyncClient(transport=httpx.ASGITransport(app=_local())))
    running = asyncio.create_task(session.run())
    await _until(lambda: ws.sent)
    assert json.loads(ws.sent[0])["t"] == "hello"

    ws.incoming.put_nowait(json.dumps({"t": "req", "id": 1, "method": "GET", "port": None, "path": "/health", "headers": []}))
    ws.incoming.put_nowait(json.dumps({"t": "end", "id": 1}))
    body = json.dumps(["a", "b"]).encode()
    ws.incoming.put_nowait(json.dumps({"t": "req", "id": 2, "method": "POST", "port": 5173, "path": "/echo", "headers": [["content-length", str(len(body))], ["content-type", "application/json"]]}))
    ws.incoming.put_nowait(pack(2, DATA, body))
    ws.incoming.put_nowait(json.dumps({"t": "end", "id": 2}))
    await _until(lambda: any(isinstance(m, dict) and m["t"] == "end" for m in ws.messages(1)) and any(isinstance(m, dict) and m["t"] == "end" for m in ws.messages(2)))

    first = ws.messages(1)
    assert first[0]["t"] == "res" and first[0]["status"] == 200
    assert json.loads(b"".join(m for m in first if isinstance(m, bytes))) == {"status": "ok", "port": 8090}  # none: the runner's own API
    second = ws.messages(2)
    assert json.loads(b"".join(m for m in second if isinstance(m, bytes))) == {"body": '["a", "b"]'}

    # The line breaks off: what is in flight ends with it, as when the app goes away.
    ws.incoming.put_nowait(json.dumps({"t": "req", "id": 3, "method": "GET", "port": None, "path": "/hang", "headers": []}))
    ws.incoming.put_nowait(json.dumps({"t": "end", "id": 3}))
    await _until(lambda: 3 in session._tasks)
    task = session._tasks[3]
    ws.incoming.put_nowait(END)
    await asyncio.wait_for(running, 5)
    assert task.cancelled() or task.done()


async def test_nothing_listening_is_told_as_such():
    ws = FakeSocket()
    session = connect.Session(ws, own_port=8090)
    running = asyncio.create_task(session.run())
    ws.incoming.put_nowait(json.dumps({"t": "req", "id": 1, "method": "GET", "port": 1, "path": "/", "headers": []}))
    ws.incoming.put_nowait(json.dumps({"t": "end", "id": 1}))
    ws.incoming.put_nowait(json.dumps({"t": "ws", "id": 2, "port": 1, "path": "/", "headers": [], "subprotocols": []}))
    await _until(lambda: ws.messages(1) and ws.messages(2))
    assert ws.messages(1)[0]["t"] == "error" and ws.messages(1)[0]["connect"] is True
    assert ws.messages(2)[0]["t"] == "error"
    ws.incoming.put_nowait(END)
    await asyncio.wait_for(running, 5)


def test_the_socket_address_follows_plainboards():
    assert connect.socket_url("https://board.example.com") == "wss://board.example.com/api/runners/connect"
    assert connect.socket_url("http://10.0.0.2:8080") == "ws://10.0.0.2:8080/api/runners/connect"
