"""External runners: the tunnel transport, the token, per-project routing and the switch rules."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from conftest import FakeRunner, agents_by_slug, end_job, idle, runs_of, wait_for
from plainboard_protocol.tunnel import DATA, pack, unpack
from starlette.websockets import WebSocketDisconnect

from plainboard.orchestration.runner_client import RunnerClient, RunnerUnavailable
from plainboard.orchestration.tunnel import Tunnel, TunnelTransport


class Remote:
    """The runner's end of a tunnel, in memory: answers what the app sends."""

    def __init__(self) -> None:
        self.tunnel: Tunnel | None = None
        self.requests: dict[int, dict] = {}
        self.bodies: dict[int, bytes] = {}
        self.cancelled: list[int] = []
        self.release = asyncio.Event()

    def feed(self, message) -> None:
        self.tunnel.feed(json.dumps(message) if isinstance(message, dict) else message)

    async def __call__(self, frame) -> None:
        if isinstance(frame, bytes):
            stream_id, kind, data = unpack(frame)
            if kind == DATA:
                self.bodies[stream_id] += data
            else:  # a socket's message: echoed in capitals
                asyncio.get_running_loop().call_soon(self.feed, pack(stream_id, kind, data.upper()))
            return
        message = json.loads(frame)
        t, stream_id = message["t"], message.get("id")
        if t == "req":
            self.requests[stream_id], self.bodies[stream_id] = message, b""
        elif t == "end":
            asyncio.create_task(self._answer(stream_id))
        elif t == "cancel":
            self.cancelled.append(stream_id)
        elif t == "ws":
            asyncio.get_running_loop().call_soon(self.feed, {"t": "ws_ok", "id": stream_id, "subprotocol": None})

    async def _answer(self, stream_id: int) -> None:
        path = self.requests[stream_id]["path"]
        if path == "/refused":
            self.feed({"t": "error", "id": stream_id, "message": "Nothing answers there", "connect": True})
            return
        self.feed({"t": "res", "id": stream_id, "status": 200, "headers": [["content-type", "application/json"]]})
        if path == "/jobs/status":
            self.feed(pack(stream_id, DATA, json.dumps({"abc": {"running": True}}).encode()))
        else:
            self.feed(pack(stream_id, DATA, b"first,"))
            await self.release.wait()
            self.feed(pack(stream_id, DATA, b"second"))
        self.feed({"t": "end", "id": stream_id})


def _pair() -> tuple[Tunnel, Remote]:
    remote = Remote()
    remote.tunnel = Tunnel(remote)
    return remote.tunnel, remote


async def test_the_tunnel_carries_requests_and_streamed_answers():
    tunnel, remote = _pair()
    runner = RunnerClient("http://runner", transport=TunnelTransport(lambda: tunnel))
    states = await runner.job_status(["abc"])
    assert states["abc"].running is True
    request = next(iter(remote.requests.values()))
    assert (request["method"], request["port"], request["path"]) == ("POST", None, "/jobs/status")  # None: the runner's own API
    assert json.loads(next(iter(remote.bodies.values()))) == ["abc"]

    previews = httpx.AsyncClient(transport=TunnelTransport(lambda: tunnel))
    async with previews.stream("GET", "http://localhost:5173/stream?x=1") as resp:
        chunks = resp.aiter_raw()
        assert await chunks.__anext__() == b"first,"  # streamed: before the server has finished
        remote.release.set()
        assert [c async for c in chunks] == [b"second"]
    assert remote.requests[2]["port"] == 5173 and remote.requests[2]["path"] == "/stream?x=1"


async def test_a_tunnel_that_breaks_off_fails_what_is_in_flight_and_nothing_reaches_a_runner_that_is_away():
    tunnel, remote = _pair()
    client = httpx.AsyncClient(transport=TunnelTransport(lambda: tunnel))
    async with client.stream("GET", "http://localhost:5173/stream") as resp:
        chunks = resp.aiter_raw()
        assert await chunks.__anext__() == b"first,"
        tunnel.close()
        with pytest.raises(httpx.ReadError):
            await chunks.__anext__()
    with pytest.raises(httpx.ConnectError):  # the line is gone
        await client.get("http://localhost:5173/")

    away = RunnerClient("http://runner", transport=TunnelTransport(lambda: None))
    with pytest.raises(RunnerUnavailable):  # nothing was started: the run waits for it, as for a runner that is not up yet
        async for _ in away.stream_run(_spec()):
            pass

    tunnel, remote = _pair()
    with pytest.raises(httpx.ConnectError):  # nothing listens on that port there
        await httpx.AsyncClient(transport=TunnelTransport(lambda: tunnel)).get("http://localhost:5999/refused")

    # A response the app stops reading is cancelled on the runner.
    async with httpx.AsyncClient(transport=TunnelTransport(lambda: tunnel)).stream("GET", "http://localhost:5173/stream") as resp:
        await resp.aiter_raw().__anext__()
    assert remote.cancelled


async def test_sockets_go_through_the_tunnel():
    tunnel, _remote = _pair()
    async with await tunnel.open_socket(5173, "/hmr", [("cookie", "a=1")]) as socket:
        await socket.send("hello")
        assert await socket.__aiter__().__anext__() == "HELLO"
    assert not tunnel._streams


def _spec():
    from plainboard_protocol import Credentials, RunSpec, ToolBridgeSpec, WorkspaceSpec

    return RunSpec(
        run_id="1", provider="mock", model="m", system_prompt="", prompt="", workspace=WorkspaceSpec(path="/w"), credentials=Credentials(), bridge=ToolBridgeSpec(base_url="", run_id="1", run_token="t")
    )


# --- in the app ---------------------------------------------------------------------------------


def _add_runner(client, name: str = "build-server") -> dict:
    resp = client.post("/api/runners", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _hub(client):
    return client.app_ref.state.runs.runners


def _connect(client, token: str):
    return client.websocket_connect("/api/runners/connect", headers={"authorization": f"Bearer {token}"})


def test_a_runner_connects_with_its_token_only_and_answers_through_the_socket(client):
    created = _add_runner(client)
    rid, token = created["runner"]["id"], created["token"]
    assert token.startswith("pbr_") and f"PLAINBOARD_RUNNER_TOKEN={token}" in created["command"]
    assert "PLAINBOARD_URL=http://testserver" in created["command"] and token in created["compose"]
    assert created["runner"]["connected"] is False
    hub = _hub(client)

    for wrong in ("", "pbr_nope", token[:-1]):
        with pytest.raises(WebSocketDisconnect) as refused:
            with _connect(client, wrong):
                pass
        assert refused.value.code == 4401
    assert not hub.connected(rid)

    with _connect(client, token) as ws:
        ws.send_text(json.dumps({"t": "hello", "version": "abc1234567890", "protocol": 1}))
        wait_for(lambda: hub.connected(rid))
        # A request of the app, answered by "the runner" at the other end of the real socket.
        pending = client.portal.start_task_soon(hub.client(rid).job_status, ["j1"])
        request = json.loads(ws.receive_text())
        assert (request["t"], request["path"], request["port"]) == ("req", "/jobs/status", None)
        assert unpack(ws.receive_bytes())[2] == b'["j1"]' and json.loads(ws.receive_text())["t"] == "end"
        sid = request["id"]
        ws.send_text(json.dumps({"t": "res", "id": sid, "status": 200, "headers": [["content-type", "application/json"]]}))
        ws.send_bytes(pack(sid, DATA, b'{"j1": {"running": false, "exit_code": 0}}'))
        ws.send_text(json.dumps({"t": "end", "id": sid}))
        assert pending.result(timeout=10)["j1"].exit_code == 0
        listed = client.get("/api/runners").json()[0]
        assert listed["connected"] is True and listed["version"] == "abc1234567"

        # A new token ends the connection, and the old one opens none.
        token = client.post(f"/api/runners/{rid}/token").json()["token"]
        wait_for(lambda: not hub.connected(rid))
    with pytest.raises(WebSocketDisconnect):
        with _connect(client, created["token"]):
            pass
    with _connect(client, token) as ws:
        ws.send_text(json.dumps({"t": "hello", "version": "", "protocol": 1}))
        wait_for(lambda: hub.connected(rid))
    wait_for(lambda: not hub.connected(rid))
    assert client.get("/api/runners").json()[0]["last_seen_at"]


def _job(client, pid: int, port: int, runner) -> int:
    from plainboard.db import sessionmaker
    from plainboard.models import Job

    agent_id = agents_by_slug(client, pid)["manager"]["id"]
    job_runner_id = f"srv{pid}x{port}x{time.monotonic_ns()}"

    async def add() -> int:
        async with sessionmaker()() as session:
            job = Job(project_id=pid, agent_id=agent_id, runner_id=job_runner_id, command="npm run dev")
            session.add(job)
            await session.commit()
            return job.id

    job_id = client.portal.call(add)
    runner.listening[job_runner_id] = [port]
    client.app_ref.state.preview_owners._at = -60.0
    return job_id


def _site(name: str):
    from fastapi import FastAPI

    site = FastAPI()

    @site.get("/{path:path}")
    async def page(path: str) -> dict:
        return {"site": name}

    return site


def test_each_project_runs_on_its_own_runner_and_a_runner_never_gets_another_projects_work(client, project):
    """Turns, jobs and previews of a project go to its runner; a project on the built-in runner never
    reaches the external one, and the other way round (its credentials and secrets with them)."""
    builtin = client.app_ref.state.runner
    external = FakeRunner("tunnel")
    hub = _hub(client)
    hub.make_client = lambda _rid: external
    rid = _add_runner(client)["runner"]["id"]
    other = client.post("/api/projects", json={"name": "Elsewhere"}).json()
    resp = client.put(f"/api/projects/{other['id']}/runner", json={"runner_id": rid})
    assert resp.status_code == 200 and resp.json()["runner_id"] == rid
    assert client.get(f"/api/projects/{other['id']}").json()["runner_id"] == rid

    # Away: its turns wait in the queue, and nothing of them reaches the built-in runner.
    away_manager = agents_by_slug(client, other["id"])["manager"]
    assert client.post(f"/api/agents/{away_manager['id']}/messages", json={"text": "SAY on the other server"}).status_code == 202
    here_manager = agents_by_slug(client, project["id"])["manager"]
    assert client.post(f"/api/agents/{here_manager['id']}/messages", json={"text": "SAY here"}).status_code == 202
    wait_for(lambda: idle(client, project["id"]))
    time.sleep(0.3)
    assert [r["status"] for r in runs_of(client, other["id"])] == ["queued"]
    assert external.specs == []

    with _connect(client, client.post(f"/api/runners/{rid}/token").json()["token"]) as ws:
        ws.send_text(json.dumps({"t": "hello", "version": "", "protocol": 1}))
        wait_for(lambda: idle(client, other["id"]))
        assert [s.prompt.splitlines()[-1] for s in external.specs] == ["SAY on the other server"]
        assert all(int(s.run_id) not in {r["id"] for r in runs_of(client, other["id"])} for s in builtin.specs)
        assert {int(s.run_id) for s in external.specs} == {r["id"] for r in runs_of(client, other["id"])}

        # Jobs
        async def start(pid: int) -> str:
            from plainboard.db import sessionmaker
            from plainboard.models import Agent, Project
            from sqlalchemy import select

            async with sessionmaker()() as session:
                p = await session.get(Project, pid)
                agent = await session.scalar(select(Agent).where(Agent.project_id == pid, Agent.is_manager))
                return (await client.app_ref.state.runs.start_job(session, p, agent, f"echo {pid}")).runner_id

        there = client.portal.call(start, other["id"])
        here = client.portal.call(start, project["id"])
        assert [s.id for s in external.job_specs] == [there] and here in [s.id for s in builtin.job_specs] and there not in [s.id for s in builtin.job_specs]

        # Previews: the same port on both runners; each project's server is served from its own.
        client.app_ref.state.preview_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=_site("built-in")))
        hub._previews[rid] = httpx.AsyncClient(transport=httpx.ASGITransport(app=_site("external")))
        theirs = _job(client, other["id"], 5173, external)
        assert client.get("/preview/5173/").json() == {"site": "external"}
        ours = _job(client, project["id"], 5173, builtin)

        async def owners() -> list[int]:
            from plainboard.api.preview import _listening
            from plainboard.db import sessionmaker

            class Conn:
                app = client.app_ref

            async with sessionmaker()() as session:
                return await client.app_ref.state.preview_owners.project_of(session, 5173, lambda jobs: _listening(Conn, jobs))

        assert sorted(client.portal.call(owners)) == sorted([project["id"], other["id"]])
        end_job(client, theirs)
        client.app_ref.state.preview_owners._at = -60.0
        assert client.get("/preview/5173/").json() == {"site": "built-in"}
        end_job(client, ours)
        # The previews list of a project asks its own runner.
        assert client.get(f"/api/projects/{other['id']}/previews").status_code == 200


def test_a_project_moves_to_another_runner_only_while_nothing_of_it_runs(client, project):
    runner = client.app_ref.state.runner
    rid = _add_runner(client)["runner"]["id"]
    pid = project["id"]
    choices = client.get(f"/api/projects/{pid}/runner").json()
    assert choices["runner_id"] is None and [r["id"] for r in choices["runners"]] == [None, rid] and choices["busy"] == ""

    manager = agents_by_slug(client, pid)["manager"]
    runner.released = False
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "HOLD"}).status_code == 202
    wait_for(lambda: [r["status"] for r in runs_of(client, pid)] == ["running"])
    refused = client.put(f"/api/projects/{pid}/runner", json={"runner_id": rid})
    assert refused.status_code == 409 and "running turn" in refused.json()["detail"]
    assert client.get(f"/api/projects/{pid}/runner").json()["busy"]
    runner.released = True
    wait_for(lambda: idle(client, pid))

    assert client.put(f"/api/projects/{pid}/runner", json={"runner_id": 999}).status_code == 404
    assert client.put(f"/api/projects/{pid}/runner", json={"runner_id": rid}).status_code == 200
    assert _hub(client).runner_id(pid) == rid
    removed = client.delete(f"/api/runners/{rid}")
    assert removed.status_code == 400 and "Demo Project" in removed.json()["detail"]
    assert client.put(f"/api/projects/{pid}/runner", json={"runner_id": None}).status_code == 200
    assert _hub(client).runner_id(pid) is None
    assert client.delete(f"/api/runners/{rid}").status_code == 204
    assert client.get("/api/runners").json() == []


def test_the_manager_hears_once_when_its_runner_stays_away(client, project, monkeypatch):
    from plainboard.db import sessionmaker
    from plainboard.models import InboxItem
    from sqlalchemy import select

    created = _add_runner(client)
    rid, pid = created["runner"]["id"], project["id"]
    assert client.put(f"/api/projects/{pid}/runner", json={"runner_id": rid}).status_code == 200
    manager = agents_by_slug(client, pid)["manager"]
    runs = client.app_ref.state.runs

    async def no_turn(*_args) -> None:
        pass  # the notes are what is looked at here, not the turn that reads them

    monkeypatch.setattr(runs, "request_wake", no_turn)

    async def inbox() -> list[str]:
        async with sessionmaker()() as session:
            return list(await session.scalars(select(InboxItem.text).where(InboxItem.agent_id == manager["id"])))

    runs.runners.away_since[rid] = time.monotonic() - 60
    client.portal.call(runs.check_runners)
    assert client.portal.call(inbox) == []  # a minute is no reason yet
    runs.runners.away_since[rid] = time.monotonic() - 3600
    client.portal.call(runs.check_runners)
    client.portal.call(runs.check_runners)
    told = client.portal.call(inbox)
    assert len(told) == 1 and "has not been connected" in told[0]

    with _connect(client, created["token"]) as ws:
        ws.send_text(json.dumps({"t": "hello", "version": "", "protocol": 1}))
        wait_for(lambda: len(client.portal.call(inbox)) == 2)
    assert "connected again" in client.portal.call(inbox)[1]
