"""The browser window: a browser in the runner, shown in PlainBoard (runner: browser.py).

A page of an agent's server runs there at localhost:<port>, where it believes it is, so pages work
that break behind /preview/<port>/. Thin: checks who asks and passes the socket on; the app does not
read what goes through it. Only for the signed-in user: it is a browser inside the agents' environment.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidHandshake

from ..db import sessionmaker
from ..services.access import Need
from .deps import socket_role
from ..services import preview as preview_service
from ..services import projects
from ..services.errors import ServiceError
from .relay import relay

router = APIRouter()


@router.websocket("/api/projects/{project_id}/browser")
async def project_browser(websocket: WebSocket, project_id: int) -> None:
    """One browser per project: two windows on the same project show the same page."""
    if not await socket_role(websocket, project_id, Need.MEMBER):
        await websocket.close(code=4401)
        return
    async with sessionmaker()() as session:
        try:
            await projects.get(session, project_id)
        except ServiceError:
            await websocket.close(code=4404)
            return
    await websocket.accept()
    try:
        upstream = await connect(websocket.app.state.runner.browser_url(f"project-{project_id}"), max_size=None, open_timeout=30, ping_interval=None, compression=None)  # frames are JPEG: compressing them again only costs time
    except (OSError, TimeoutError, InvalidHandshake):
        await websocket.send_json({"t": "error", "message": "The runner can't be reached, or it has no browser yet (rebuild it)."})
        await websocket.close()
        return
    preview_service.note_browsed(project_id)
    async with upstream:
        try:
            await relay(websocket, upstream, f"Browser of project {project_id}")
        finally:
            preview_service.note_browsed(project_id)  # watched until now
