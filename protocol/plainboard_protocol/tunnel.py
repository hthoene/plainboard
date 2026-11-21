"""The tunnel of an external runner: HTTP requests and WebSockets of the app, carried over the one
WebSocket the runner opened to the app (``/api/runners/connect``), so the runner's server needs no
inbound port, TLS or DNS of its own.

Control messages are JSON text frames, each with the stream it is about (``id``, chosen by the app):

    app -> runner   {"t": "req", "id", "method", "port", "path", "headers"}  then body data, then {"t": "end", "id"}
                    {"t": "ws", "id", "port", "path", "headers", "subprotocols"}
                    {"t": "cancel", "id"}                 the app no longer wants the response
                    {"t": "close", "id", "code"}          a socket ends
    runner -> app   {"t": "hello", "version", "protocol", "instance"} first, once (instance: new with every start)
                    {"t": "res", "id", "status", "headers"} then body data, then {"t": "end", "id"}
                    {"t": "ws_ok", "id", "subprotocol"}
                    {"t": "error", "id", "message", "connect"}  ``connect``: nothing was reached
                    {"t": "close", "id", "code"}
                    {"t": "ping"}                         every PING_SECONDS, both ways, so either side notices a dead line

Bodies and socket messages are binary frames: a header of stream id and kind, then the bytes.
``port`` None is the runner's own API; otherwise a port on the runner's machine (a preview)."""

from __future__ import annotations

import struct

PROTOCOL = 1
# Both sides send {"t": "ping"} this often, well under the idle timeouts of proxies (Cloudflare ~100 s),
# and take a line from which nothing came for DEAD_SECONDS as gone (a half-open connection).
PING_SECONDS = 15
DEAD_SECONDS = 45
CHUNK = 64 * 1024

# How the app ends a connection, so the runner knows how soon to come back.
CLOSE_REFUSED = 4401  # unknown or revoked token: retry slowly
CLOSE_REPLACED = 4409  # another connection with the same token took over: retry slowly
CLOSE_PROTOCOL = 4426  # the runner speaks another protocol: update it

DATA, TEXT, BYTES = 0, 1, 2  # body bytes; a socket's text message; a socket's binary message
_HEADER = struct.Struct("!IB")


def pack(stream_id: int, kind: int, payload: bytes) -> bytes:
    return _HEADER.pack(stream_id, kind) + payload


def unpack(frame: bytes) -> tuple[int, int, bytes]:
    stream_id, kind = _HEADER.unpack_from(frame)
    return stream_id, kind, frame[_HEADER.size :]


# Per connection: never passed through the tunnel (each side's HTTP client sets its own).
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-connection"}
