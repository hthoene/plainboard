"""Reverse proxy to web servers in the runner (see services/preview.py). Thin: HTTP and WebSocket plumbing."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Form, Request, WebSocket
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.requests import HTTPConnection
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidHandshake

from ..config import get_config
from ..db import sessionmaker
from ..orchestration.runner_client import RunnerError
from ..security import SESSION_COOKIE
from ..services import access as access_service
from ..services import preview
from ..services import sessions as session_service
from ..services import users as user_service
from ..services.errors import ServiceError
from . import guard
from .relay import relay

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


UNLOCK_PREFIX = "/api/public/preview/"  # under the public API: its visitors are not signed in


Access = Literal["ok", "shared", "password", "denied", "unknown"]


async def _listening(conn: HTTPConnection, ids: list[str]) -> dict[str, list[int]]:
    try:
        return await conn.app.state.runner.job_listening(ids)
    except RunnerError:
        return {}  # the runner is restarting: no port is known, none opens


async def access(conn: HTTPConnection, port: int) -> Access:
    """``ok``: the viewer has a role in the project whose running server listens on the port, is an
    admin, or gave the password of its share; ``shared``: it is shared publicly (no login involved);
    ``unknown``: no server of an agent runs there. On the preview origin the viewer is who came
    there from PlainBoard (its cookie for this port), on PlainBoard's own the signed-in user."""
    async with sessionmaker()() as session:
        project_id = await conn.app.state.preview_owners.project_of(session, port, lambda ids: _listening(conn, ids))
        if project_id is None:
            return "unknown"
        if guard.on_preview_host(conn.headers):
            entered = preview.read_auth(conn.cookies.get(preview.auth_cookie(port)), port)
            user = await session_service.user_of(session, entered[0], entered[2], entered[1]) if entered else None
        else:
            who = await access_service.principal(session, conn.cookies.get(SESSION_COOKIE))
            user = who.user if who else None
        if user is not None and await user_service.role_in(session, user, project_id) is not None:
            return "ok"
        row = await preview.active_share(session, port)
    if row is None or row.project_id != project_id:  # a share of the project that ran here before is none
        return "denied"
    if row.access == "public":
        return "shared"
    if preview.unlocked(row, conn.cookies.get(preview.unlock_cookie(port))):
        return "ok"
    return "password"


def new_client() -> httpx.AsyncClient:
    # No read timeout: previews stream (server-sent events, long polls).
    return httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5), follow_redirects=False, limits=httpx.Limits(max_connections=200))


def redirect_from_preview_page(request: Request) -> Response | None:
    """A preview page asked for a path at the root (absolute URL in its code): send it back under its
    prefix. A convenience only: whatever it sends there, the page runs sandboxed or on an origin of its
    own (where api/guard.py does this), so it reaches nothing of PlainBoard either way."""
    path = request.url.path
    if preview.origin() or path.startswith((preview.PREFIX + "/", preview.BASE_PREFIX + "/", UNLOCK_PREFIX)):
        return None
    port = preview.port_of_referer(request.headers.get("referer"), request.headers.get("host"))
    if port is None:
        port = _port_without_referer(request)
    if port is None:
        return None
    query = request.url.query
    return RedirectResponse(preview.prefix(port) + path + (f"?{query}" if query else ""), status_code=307)


def _port_without_referer(request: Request) -> int | None:
    """A preview page can hide which page asked (a referrer policy in its HTML, a link marked
    noreferrer). Then the last opened preview gets what would be of no use to PlainBoard itself:
    a part of a page (not a page) that is neither PlainBoard's API nor one of its files."""
    headers = request.headers
    referer = headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        if parts.netloc != headers.get("host") or parts.path not in ("", "/"):
            return None
    path = request.url.path
    if headers.get("sec-fetch-site") != "same-origin" or headers.get("sec-fetch-dest") in (None, "document") or path == "/" or path.startswith("/api/"):
        return None
    static_dir = get_config().static_dir.resolve()
    candidate = (static_dir / path.lstrip("/")).resolve()
    if candidate.is_file() and static_dir in candidate.parents:
        return None
    port = preview.port_of_cookie(request.cookies.get(preview.PORT_COOKIE))
    try:
        return preview.check_port(port) if port is not None else None
    except ServiceError:
        return None


async def _to_preview_origin(request: Request, port: int, path: str, base: bool) -> Response:
    """PlainBoard's own address serves no preview when there is a preview origin: it forwards there,
    with a token for a signed-in user (whether they may open it is decided there, on every request)."""
    query = request.url.query
    here = f"{preview.prefix(port, base)}/{path}" + (f"?{query}" if query else "")
    async with sessionmaker()() as session:
        who = await access_service.principal(session, request.cookies.get(SESSION_COOKIE))
    if who is None or who.user is None:
        return RedirectResponse(preview.origin() + here, status_code=307)
    token = preview.enter_token(who.user.id, who.user.session_version, who.session_id, port)
    return RedirectResponse(f"{preview.origin()}{UNLOCK_PREFIX}{port}/enter?" + urlencode({"t": token, "next": here}), status_code=303)


@router.api_route("/preview/{port}", methods=METHODS)
async def preview_root(port: int) -> Response:
    return RedirectResponse(f"{preview.prefix(preview.check_port(port))}/", status_code=307)


@router.api_route("/preview-base/{port}", methods=METHODS)
async def preview_base_root(port: int) -> Response:
    return RedirectResponse(f"{preview.prefix(preview.check_port(port), base=True)}/", status_code=307)


@router.api_route("/preview/{port}/{path:path}", methods=METHODS)
async def preview_http(request: Request, port: int, path: str) -> Response:
    return await _proxy(request, port, path, base=False)


@router.api_route("/preview-base/{port}/{path:path}", methods=METHODS)
async def preview_base_http(request: Request, port: int, path: str) -> Response:
    """For a server that has /preview-base/<port>/ as its base path: it gets the path as the browser sent it."""
    return await _proxy(request, port, path, base=True)


async def _proxy(request: Request, port: int, path: str, base: bool) -> Response:
    preview.check_port(port)
    if preview.origin() and not guard.on_preview_host(request.headers):
        return await _to_preview_origin(request, port, path, base)
    allowed = await access(request, port)
    if allowed == "unknown":
        return PlainTextResponse(preview.unreachable(port), status_code=404)
    if allowed == "password":
        return HTMLResponse(preview.unlock_page(port, base=base), status_code=401)
    if allowed == "denied":
        return PlainTextResponse("This preview is private. Open it from PlainBoard, signed in with a role in its project.", status_code=401)
    preview.note_opened(port)
    client: httpx.AsyncClient = request.app.state.preview_client
    body = await request.body()
    upstream = client.build_request(
        request.method,
        preview.target(port, path, request.url.query, base=base),
        headers=preview.request_headers(list(request.headers.items()), port),
        content=body or None,
    )
    try:
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError:
        return PlainTextResponse(preview.unreachable(port), status_code=502)
    out = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, background=BackgroundTask(resp.aclose))
    secure = guard.secure_cookies(request)
    for name, value in preview.response_headers(resp.headers.multi_items(), port, secure=secure, sandbox=preview.sandboxed()):
        out.headers.append(name, value)
    # The last preview that was opened as a page, not the last one any open tab asked something of:
    # sockets to the root go by this, and two previews in two tabs would otherwise take turns.
    if request.headers.get("sec-fetch-dest") in (None, "document", "iframe") or preview.PORT_COOKIE not in request.cookies:
        out.set_cookie(preview.PORT_COOKIE, str(port), samesite="lax", secure=secure)
    return out


@router.get(UNLOCK_PREFIX + "{port}/enter")
async def enter(request: Request, port: int, t: str = "", next: str = "") -> Response:
    """On the preview origin: the link from PlainBoard, whose token becomes this origin's cookie for the port."""
    entered = preview.read_enter(t, port) if guard.on_preview_host(request.headers) else None
    if entered is None:
        return PlainTextResponse("This link to the preview has expired. Open the preview from PlainBoard again.", status_code=401)
    out = RedirectResponse(preview.enter_next(port, next), status_code=303)
    out.set_cookie(
        preview.auth_cookie(port), preview.auth_value(*entered, port), max_age=preview.AUTH_MAX_AGE, httponly=True, samesite="lax", secure=guard.secure_cookies(request)
    )
    return out


@router.post(UNLOCK_PREFIX + "{port}/unlock")
async def unlock(request: Request, port: int, password: str = Form(...), base: str = Form("")) -> Response:
    async with sessionmaker()() as session:
        row = await preview.active_share(session, port)
    if row is None or row.access != "password" or not preview.password_ok(row, password):
        await asyncio.sleep(1)  # slows down guessing
        return HTMLResponse(preview.unlock_page(port, wrong=True, base=bool(base)), status_code=401)
    out = RedirectResponse(f"{preview.prefix(port, base=bool(base))}/", status_code=303)
    out.set_cookie(
        preview.unlock_cookie(port), preview.unlock_value(row), max_age=preview.UNLOCK_MAX_AGE, httponly=True, samesite="lax", secure=guard.secure_cookies(request)
    )
    return out


@router.websocket("/preview/{port}/{path:path}")
async def preview_ws(websocket: WebSocket, port: int, path: str) -> None:
    await _pipe(websocket, port, path)


@router.websocket("/preview-base/{port}/{path:path}")
async def preview_base_ws(websocket: WebSocket, port: int, path: str) -> None:
    await _pipe(websocket, port, path, base=True)


@router.websocket("/{path:path}")
async def preview_ws_at_root(websocket: WebSocket, path: str) -> None:
    """A socket to the root (hot reload, a socket of the app itself, also under /api/) carries no hint
    but the cookie of the last preview. PlainBoard's own sockets have their routes and never get here."""
    port = preview.port_of_cookie(websocket.cookies.get(preview.PORT_COOKIE))
    if port is None or (preview.origin() and not guard.on_preview_host(websocket.headers)):
        await websocket.close(code=4404)
        return
    await _pipe(websocket, port, path)


async def _pipe(websocket: WebSocket, port: int, path: str, base: bool = False) -> None:
    try:
        preview.check_port(port)
    except ServiceError:
        await websocket.close(code=4404)
        return
    if preview.origin() and not guard.on_preview_host(websocket.headers):
        await websocket.close(code=4404)  # the pages are on the preview origin, and so are their sockets
        return
    allowed = await access(websocket, port)
    if allowed not in ("ok", "shared"):
        await websocket.close(code=4401)
        return
    # A socket opened with a cookie (a role, a share's password) only from a page of the preview itself.
    if allowed == "ok" and not guard.origin_ok(websocket.headers, *([preview.origin()] if preview.origin() else [])):
        await websocket.close(code=4403)
        return
    headers = [(k, v) for k, v in preview.request_headers(list(websocket.headers.items()), port) if k.lower() in ("cookie", "user-agent", "accept-language")]
    try:
        upstream = await connect(
            f"ws://localhost:{port}{preview.upstream_path(port, path, base)}" + (f"?{websocket.url.query}" if websocket.url.query else ""),
            host=preview.runner_host(),  # where to connect; the URL is what the server sees
            port=port,
            origin=f"http://localhost:{port}",
            subprotocols=websocket.scope.get("subprotocols") or None,
            additional_headers=headers,
            open_timeout=5,
            max_size=None,
        )
    except (OSError, TimeoutError, InvalidHandshake):
        await websocket.close(code=1013)
        return
    preview.note_opened(port)
    async with upstream:
        await websocket.accept(subprotocol=upstream.subprotocol)
        try:
            await relay(websocket, upstream, f"Preview socket on port {port}")
        finally:
            preview.note_opened(port)  # watched until now
