"""Reverse proxy to web servers in the runner (see services/preview.py). Thin: HTTP and WebSocket plumbing."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Form, Request, WebSocket
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.requests import HTTPConnection
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidHandshake

from ..config import get_config
from ..db import sessionmaker
from ..services import preview
from ..services.errors import ServiceError
from .deps import who_is
from .relay import relay

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


UNLOCK_PREFIX = "/api/public/preview/"  # under the public API: its visitors are not signed in


async def access(conn: HTTPConnection, port: int) -> Literal["ok", "password", "denied"]:
    """A signed-in user opens every preview; anyone else only a shared one. Ports belong to the
    runner, not to a project: which project a server is of is not known here."""
    who = await who_is(conn)
    if who is not None and who.user is not None:
        return "ok"
    async with sessionmaker()() as session:
        row = await preview.active_share(session, port)
    if row is None:
        return "denied"
    if row.access == "public" or preview.unlocked(row, conn.cookies.get(preview.unlock_cookie(port))):
        return "ok"
    return "password"


def new_client() -> httpx.AsyncClient:
    # No read timeout: previews stream (server-sent events, long polls).
    return httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5), follow_redirects=False, limits=httpx.Limits(max_connections=200))


def redirect_from_preview_page(request: Request) -> Response | None:
    """A preview page asked for a path at the root (absolute URL in its code): send it back under its prefix."""
    path = request.url.path
    if path.startswith((preview.PREFIX + "/", preview.BASE_PREFIX + "/", UNLOCK_PREFIX)):
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


def _secure(request: Request) -> bool:
    """Whether the browser reached PlainBoard over HTTPS, also behind a reverse proxy."""
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return (forwarded or request.url.scheme) == "https"


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
    allowed = await access(request, port)
    if allowed == "password":
        return HTMLResponse(preview.unlock_page(port, base=base), status_code=401)
    if allowed == "denied":
        return PlainTextResponse("This preview is private. Sign in to PlainBoard first.", status_code=401)
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
    for name, value in preview.response_headers(resp.headers.multi_items(), port, secure=_secure(request)):
        out.headers.append(name, value)
    # The last preview that was opened as a page, not the last one any open tab asked something of:
    # sockets to the root go by this, and two previews in two tabs would otherwise take turns.
    if request.headers.get("sec-fetch-dest") in (None, "document", "iframe") or preview.PORT_COOKIE not in request.cookies:
        out.set_cookie(preview.PORT_COOKIE, str(port), samesite="lax")
    return out


@router.post(UNLOCK_PREFIX + "{port}/unlock")
async def unlock(port: int, password: str = Form(...), base: str = Form("")) -> Response:
    async with sessionmaker()() as session:
        row = await preview.active_share(session, port)
    if row is None or row.access != "password" or not preview.password_ok(row, password):
        await asyncio.sleep(1)  # slows down guessing
        return HTMLResponse(preview.unlock_page(port, wrong=True, base=bool(base)), status_code=401)
    out = RedirectResponse(f"{preview.prefix(port, base=bool(base))}/", status_code=303)
    out.set_cookie(preview.unlock_cookie(port), preview.unlock_value(row), max_age=preview.UNLOCK_MAX_AGE, httponly=True, samesite="lax")
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
    if port is None:
        await websocket.close(code=4404)
        return
    await _pipe(websocket, port, path)


async def _pipe(websocket: WebSocket, port: int, path: str, base: bool = False) -> None:
    try:
        preview.check_port(port)
    except ServiceError:
        await websocket.close(code=4404)
        return
    if await access(websocket, port) != "ok":
        await websocket.close(code=4401)
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
