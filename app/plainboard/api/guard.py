"""What every request passes before a route: which address it came to (PlainBoard or its preview
origin), whether a page of another site sent it (CSRF), how large its body may be, and HSTS.

Origins are compared by host (and port): behind a reverse proxy the app does not see the scheme the
browser used. The host is the request's Host, or the X-Forwarded-Host a proxy set; a page of another
site can set neither on a request the browser sends for it."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import get_config
from ..services import preview
from ..services.errors import ServiceError

SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")
# Authenticated by a token in the request, never by the login cookie; and the previewed servers' own business.
NO_CSRF_CHECK = ("/api/internal/", "/api/public/", preview.PREFIX + "/", preview.BASE_PREFIX + "/")
# What the preview origin serves: previews, and how one gets in (a token from PlainBoard, a share's password).
PREVIEW_HOST_PATHS = (preview.PREFIX + "/", preview.BASE_PREFIX + "/", "/api/public/preview/")
OWN_SOCKETS = re.compile(r"^/api/(projects/\d+/(ws|browser)|runners/connect)$")
IMPORT_PATH = "/api/projects/import"
HSTS = (b"strict-transport-security", b"max-age=31536000")


def _netloc(value: str | None, scheme: str = "") -> str | None:
    """host[:port] in lower case, without the scheme's default port. None: not an address."""
    if not value:
        return None
    value = value.strip().lower()
    if "://" in value:
        parts = urlsplit(value)
        scheme, value = parts.scheme, parts.netloc
    if not value or value == "null":
        return None
    for default_scheme, port in (("http", ":80"), ("https", ":443")):
        if scheme == default_scheme and value.endswith(port):
            value = value[: -len(port)]
    return value


def _hosts(headers: Headers) -> set[str]:
    found = {_netloc(headers.get("host")), _netloc(headers.get("x-forwarded-host", "").split(",")[0])}
    return {host for host in found if host}


def _allowed() -> set[str]:
    return {host for item in get_config().allowed_origins.split(",") if (host := _netloc(item))}


def preview_host() -> str | None:
    return _netloc(preview.origin())


def on_preview_host(headers: Headers) -> bool:
    """The request came to the preview origin. Host or X-Forwarded-Host decide alike: either may say so,
    and the stricter answer wins."""
    host = preview_host()
    return host is not None and host in _hosts(headers)


def is_https(conn: HTTPConnection) -> bool:
    """The browser reached PlainBoard over HTTPS, also behind a reverse proxy (X-Forwarded-Proto)."""
    forwarded = conn.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return (forwarded or conn.url.scheme) in ("https", "wss")


def secure_cookies(conn: HTTPConnection) -> bool:
    forced = get_config().secure_cookies.strip().lower()
    if forced in ("true", "1", "yes"):
        return True
    if forced in ("false", "0", "no"):
        return False
    return is_https(conn)


def origin_ok(headers: Headers, *extra: str) -> bool:
    """A browser's request (a socket, a form) comes from a page of PlainBoard itself, or of an origin
    that was allowed. Without Origin and Referer it is no browser's: nothing to check."""
    origin = headers.get("origin")
    if origin is None:
        referer = headers.get("referer")
        if not referer:
            return True
        origin = referer
    host = _netloc(origin)
    return host is not None and host in _hosts(headers) | _allowed() | {h for e in extra if (h := _netloc(e))}


def _body_limit(path: str) -> int:
    config = get_config()
    return (config.max_import_mb if path == IMPORT_PATH else config.max_upload_mb * 10 + 1) * 1024 * 1024


class Guard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        path = scope["path"]
        preview_side = on_preview_host(conn.headers)
        if scope["type"] == "websocket":
            if preview_side and OWN_SOCKETS.match(path):
                await send({"type": "websocket.close", "code": 4404})
                return
            await self.app(scope, receive, send)
            return

        if preview_side and not path.startswith(PREVIEW_HOST_PATHS):
            # Nothing of PlainBoard lives here: a path at the root is one a preview page asked for.
            port = preview.port_of_referer(conn.headers.get("referer"), conn.headers.get("host")) or preview.port_of_cookie(conn.cookies.get(preview.PORT_COOKIE))
            query = scope.get("query_string", b"").decode()
            response = (
                RedirectResponse(preview.prefix(port) + path + (f"?{query}" if query else ""), status_code=307)
                if port is not None
                else PlainTextResponse("Not found", status_code=404)
            )
            await response(scope, receive, send)
            return
        if scope["method"] not in SAFE_METHODS and not path.startswith(NO_CSRF_CHECK) and not origin_ok(conn.headers):
            await JSONResponse({"detail": "This request came from a page of another site."}, status_code=403)(scope, receive, send)
            return

        limit = _body_limit(path)
        length = conn.headers.get("content-length", "")
        if length.isdigit() and int(length) > limit:
            await JSONResponse({"detail": f"Too large (max {limit // (1024 * 1024)} MB)"}, status_code=413)(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise ServiceError(f"Too large (max {limit // (1024 * 1024)} MB)", 413)
            return message

        hsts = get_config().hsts and is_https(conn)

        async def send_with_headers(message: Message) -> None:
            if hsts and message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(name.lower() == HSTS[0] for name, _ in headers):
                    headers.append(HSTS)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, limited_receive, send_with_headers)
