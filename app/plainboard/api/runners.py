"""External runners: Settings > Runners (admins), a project's runner (its owner), and the socket an
external runner connects to (``/api/runners/connect``, its token in the Authorization header)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, Request, WebSocket
from plainboard_protocol.tunnel import DEAD_SECONDS
from starlette.websockets import WebSocketDisconnect

from ..db import sessionmaker
from ..events import bus
from ..orchestration.tunnel import Tunnel
from ..schemas import ProjectRunnerIn, ProjectRunnerOut, RunnerCreated, RunnerIn, RunnerOut
from ..services import projects as project_service
from ..services import runners as runner_service
from .deps import Runs, Session
from .guard import is_https

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["runners"])
sockets = APIRouter()


def _url(request: Request) -> str:
    """PlainBoard's address as the browser reached it: where the runner connects to."""
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip() or request.headers.get("host", "localhost")
    return f"{'https' if is_https(request) else 'http'}://{host}"


@router.get("/runners", response_model=list[RunnerOut])
async def list_runners(session: Session, runs: Runs) -> list[RunnerOut]:
    return await runner_service.listing(session, runs.runners)


@router.post("/runners", response_model=RunnerCreated)
async def create_runner(body: RunnerIn, request: Request, session: Session, runs: Runs) -> RunnerCreated:
    runner, token = await runner_service.create(session, body.name)
    return await runner_service.created(session, runs.runners, runner, token, _url(request))


@router.patch("/runners/{runner_id}", response_model=RunnerOut)
async def rename_runner(runner_id: int, body: RunnerIn, session: Session, runs: Runs) -> RunnerOut:
    await runner_service.rename(session, await runner_service.get(session, runner_id), body.name)
    return next(r for r in await runner_service.listing(session, runs.runners) if r.id == runner_id)


@router.post("/runners/{runner_id}/token", response_model=RunnerCreated)
async def new_runner_token(runner_id: int, request: Request, session: Session, runs: Runs) -> RunnerCreated:
    """The old token stops working, and its connection ends."""
    runner = await runner_service.get(session, runner_id)
    token = await runner_service.new_secret(session, runner)
    runs.runners.forget(runner_id)
    return await runner_service.created(session, runs.runners, runner, token, _url(request))


@router.delete("/runners/{runner_id}", status_code=204)
async def delete_runner(runner_id: int, session: Session, runs: Runs) -> None:
    await runner_service.remove(session, await runner_service.get(session, runner_id))
    runs.runners.forget(runner_id)


@router.get("/projects/{project_id}/runner", response_model=ProjectRunnerOut)
async def project_runner(project_id: int, session: Session, runs: Runs) -> ProjectRunnerOut:
    return await runner_service.choices(session, runs.runners, await project_service.get(session, project_id))


@router.put("/projects/{project_id}/runner", response_model=ProjectRunnerOut)
async def set_project_runner(project_id: int, body: ProjectRunnerIn, session: Session, runs: Runs) -> ProjectRunnerOut:
    project = await runner_service.assign(session, runs.runners, await project_service.get(session, project_id), body.runner_id)
    bus.publish(project.id, "project.updated", project_service.to_out(project))
    runs.wake()
    return await runner_service.choices(session, runs.runners, project)


@sockets.websocket("/api/runners/connect")
async def connect(websocket: WebSocket) -> None:
    """The tunnel of an external runner (orchestration/tunnel.py). It says hello first, then answers
    what the app sends; a ping every 20 s, and a line silent for longer than ``DEAD_SECONDS`` is gone."""
    token = websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
    async with sessionmaker()() as session:
        runner = await runner_service.authenticate(session, token) if token else None
    if runner is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    runs = websocket.app.state.runs

    async def send(frame: str | bytes) -> None:
        if isinstance(frame, str):
            await websocket.send_text(frame)
        else:
            await websocket.send_bytes(frame)

    tunnel = Tunnel(send, runner.id)
    try:
        hello = json.loads(await asyncio.wait_for(websocket.receive_text(), 30))
        if not isinstance(hello, dict) or hello.get("t") != "hello":
            raise ValueError("no hello")
    except (TimeoutError, ValueError, WebSocketDisconnect, RuntimeError, KeyError):
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=4400)
        return
    try:
        await runs.runner_connected(runner.id, tunnel, hello)  # in here: whatever ends it, the connection is let go
        while not tunnel.closed:
            message = await asyncio.wait_for(websocket.receive(), DEAD_SECONDS)
            if message["type"] == "websocket.disconnect":
                break
            tunnel.feed(message["text"] if message.get("text") is not None else message["bytes"])
    except (TimeoutError, WebSocketDisconnect, RuntimeError, ValueError) as exc:
        log.info("Runner %s: connection ended (%s)", runner.id, type(exc).__name__)
    finally:
        tunnel.close()
        await runs.runner_gone(runner.id, tunnel)
        with contextlib.suppress(RuntimeError):
            await websocket.close()
