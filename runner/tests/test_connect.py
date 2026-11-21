"""The runner's end of the tunnel (connect.py): requests and sockets of the app, answered locally."""

from __future__ import annotations

import asyncio
import json
import time

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


def test_the_built_in_runner_connects_nowhere(monkeypatch):
    """Without PLAINBOARD_URL and a token (the compose setup) the runner is called by the app, as always."""
    monkeypatch.setattr(connect, "APP_URL", "")
    monkeypatch.setattr(connect, "TOKEN", "")
    assert connect.enabled() is False
    monkeypatch.setattr(connect, "APP_URL", "https://board.example.com")
    assert connect.enabled() is False  # a token is needed too


# --- robust against breaks ------------------------------------------------------------------------


async def test_the_run_log_resumes_where_the_reader_stopped_and_is_bounded():
    from plainboard_runner.runlog import Gone, RunLog

    kept = RunLog(max_lines=3)
    for n in range(2):
        await kept.put(f"line {n}")
    read = kept.follow(0)
    assert [await read.__anext__(), await read.__anext__()] == ["line 0", "line 1"]
    await read.aclose()  # the line broke here
    for n in range(2, 5):
        await kept.put(f"line {n}")
    await kept.put(None)
    assert kept.first == 2 and not kept.holds(1) and kept.holds(2)  # only the last 3 are held
    assert [line async for line in kept.follow(2)] == ["line 2", "line 3", "line 4"]
    try:
        [line async for line in kept.follow(1)]
        raise AssertionError("an old part must be refused")
    except Gone:
        pass


def test_an_external_runners_turn_goes_on_when_its_stream_breaks_and_the_stream_is_resumed(tmp_path, monkeypatch):
    import threading

    from fastapi.testclient import TestClient
    from plainboard_protocol import AssistantTextEvent, ResultEvent, SessionEvent, parse_event

    from plainboard_runner import config
    from plainboard_runner.main import app
    from plainboard_runner.providers import get_provider

    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    monkeypatch.setattr("plainboard_runner.workspace.WORKSPACES_ROOT", tmp_path)
    monkeypatch.setattr("plainboard_runner.workspace._setup_done", set())
    monkeypatch.setattr(connect, "APP_URL", "https://board.example.com")
    monkeypatch.setattr(connect, "TOKEN", "pbr_test")

    async def no_line() -> None:
        pass

    monkeypatch.setattr(connect, "serve", no_line)
    gate = threading.Event()

    async def run(spec, bridge):
        yield SessionEvent(session_id="s")
        yield AssistantTextEvent(text="one")
        while not gate.is_set():
            await asyncio.sleep(0.01)
        yield AssistantTextEvent(text="two")
        yield ResultEvent(session_id="s", text="two")

    monkeypatch.setattr(get_provider("mock"), "run", run)
    spec = {
        "run_id": "k1",
        "provider": "mock",
        "model": "mock-large",
        "system_prompt": "",
        "prompt": "x",
        "workspace": {"path": str(tmp_path / "p" / "a")},
        "credentials": {},
        "bridge": {"base_url": "http://app", "run_id": "k1", "run_token": "t", "tools": []},
    }
    with TestClient(app) as client:
        with client.stream("POST", "/runs", json=spec) as resp:
            lines = resp.iter_lines()
            assert parse_event(next(lines)).type == "session" and parse_event(next(lines)).text == "one"
        # The stream broke here (the line to PlainBoard is gone); the turn goes on.
        time.sleep(0.1)
        assert client.get("/runs").json() == ["k1"]
        gate.set()
        with client.stream("GET", "/runs/k1/events?after=2") as resp:
            rest = [parse_event(line) for line in resp.iter_lines() if line]
        assert [(e.type, getattr(e, "text", "")) for e in rest] == [("assistant_text", "two"), ("result", "two")]  # nothing missed, nothing twice
        assert client.get("/runs/other/events").status_code == 404  # a turn of before a restart: it is gone
        assert client.get("/runs/k1/events?after=99").status_code == 410


async def test_a_half_open_line_is_noticed_by_the_runner():
    class Silent(FakeSocket):
        closed = False

        async def close(self) -> None:
            self.closed = True
            self.incoming.put_nowait(END)

    ws = Silent()
    session = connect.Session(ws, own_port=8090)
    session.dead_seconds = 0.2  # nothing comes from PlainBoard, not even its ping
    await asyncio.wait_for(session.run(), 5)
    assert ws.closed


def test_after_a_refusal_the_runner_comes_back_slowly():
    from plainboard_protocol.tunnel import CLOSE_PROTOCOL, CLOSE_REFUSED, CLOSE_REPLACED

    assert connect.backoff_after(CLOSE_REFUSED, 2.0) == connect.REFUSED_BACKOFF >= 300
    assert connect.backoff_after(CLOSE_REPLACED, 2.0) == connect.REPLACED_BACKOFF
    assert connect.backoff_after(CLOSE_PROTOCOL, 2.0) == connect.REFUSED_BACKOFF
    assert connect.backoff_after(1006, 2.0) == 2.0  # a break: soon again
    assert len(connect.INSTANCE) == 32
