from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import get_config
from ..events import bus
from ..security import SESSION_COOKIE, session_cookie_valid

router = APIRouter()


@router.websocket("/api/projects/{project_id}/ws")
async def project_events(websocket: WebSocket, project_id: int) -> None:
    if get_config().password and not session_cookie_valid(websocket.cookies.get(SESSION_COOKIE)):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    queue = bus.subscribe(project_id)

    async def pump() -> None:
        while True:
            await websocket.send_json(await queue.get())

    sender = asyncio.create_task(pump())
    try:
        while True:  # keep the socket open; client may send pings
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        bus.unsubscribe(project_id, queue)
