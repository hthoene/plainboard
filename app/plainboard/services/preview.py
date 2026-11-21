"""Preview: web servers that agents start in the runner, opened through PlainBoard at /preview/<port>/.

The previewed server believes it runs at the root of localhost:<port>. What it gets wrong because of
that is repaired here: redirects are rewritten, requests that a preview page sends to the root are
sent back under its prefix, and its cookies and headers are made to fit PlainBoard's address.

What cannot be repaired from outside is a page that reads its own address (client-side routing):
such an app is given its base path instead and opened at /preview-base/<port>/, which reaches it
unchanged.

A preview is code of an agent, so it must not run as PlainBoard: on PlainBoard's origin its scripts
could use the API as whoever opened it. With ``preview_origin`` set, previews are served only there
(a link on PlainBoard's address forwards with a short-lived token, which becomes a cookie of that
origin for this port); without it, every preview response gets the CSP sandbox, which gives the page
an origin of its own (no cookies of PlainBoard, no reading its API, no storage).

Who opens a port: it has to belong to a server job that runs (``Owners``), and the viewer needs a role
in that job's project, or be an admin, or hold a share of exactly that project's port."""

from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from plainboard_protocol import JobPort

from ..config import get_config
from ..models import Job, JobStatus, PreviewShare, utcnow
from ..schemas import PreviewOut
from ..security import SESSION_COOKIE, sign, unsign
from .errors import ServiceError
from .util import truncate

PREFIX = "/preview"
BASE_PREFIX = "/preview-base"  # for a server that was told this is its base path: the path is passed on as it is
PORT_COOKIE = "plainboard_preview"  # the last previewed port: WebSockets to the root have no other hint
UNLOCK_COOKIE_PREFIX = "pb_preview_key_"  # per port: the visitor gave the password of a shared preview
UNLOCK_MAX_AGE = 60 * 60 * 24 * 7
AUTH_COOKIE_PREFIX = "pb_preview_auth_"  # per port, on the preview origin: who came from PlainBoard to it
ENTER_MAX_AGE = 120  # the token in the link from PlainBoard to the preview origin
AUTH_MAX_AGE = 60 * 60 * 24 * 7  # and the cookie made of it (ends with the PlainBoard session anyway)
# Without a preview origin: the page runs in an origin of its own. No allow-same-origin, ever.
SANDBOX = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
OWNERS_SECONDS = 5.0  # how long the ports of the running servers are taken as known
_REFERER_PATH = re.compile(rf"^(?:{BASE_PREFIX}|{PREFIX})/(\d+)(?:/|$)")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]"}
# Per connection, or decided by this proxy.
_DROP_REQUEST = {"host", "connection", "keep-alive", "upgrade", "te", "trailer", "transfer-encoding", "proxy-authorization", "proxy-connection", "content-length"}
_DROP_RESPONSE = {"connection", "keep-alive", "upgrade", "te", "trailer", "transfer-encoding", "proxy-authenticate", "proxy-connection"}
# These would act on PlainBoard's whole address, not on the preview: forcing HTTPS, wiping its cookies and storage.
_DROP_RESPONSE |= {"strict-transport-security", "clear-site-data"}
# Referrer policies under which a page does not tell its own site which page asked.
_PATHLESS_REFERRER = {"no-referrer": "same-origin", "origin": "origin-when-cross-origin", "strict-origin": "strict-origin-when-cross-origin"}


def origin() -> str:
    """The separate origin previews are served from; empty: PlainBoard's own (sandboxed)."""
    return get_config().preview_origin.strip().rstrip("/")


def sandboxed() -> bool:
    return not origin() and get_config().preview_sandbox


def _own_cookie(name: str) -> bool:
    return name in (SESSION_COOKIE, PORT_COOKIE) or name.startswith((UNLOCK_COOKIE_PREFIX, AUTH_COOKIE_PREFIX))


def runner_host() -> str:
    return urlsplit(get_config().runner_url).hostname or "runner"


def check_port(port: int) -> int:
    """Only ports an agent can open itself, and never the runner's own API."""
    runner_port = urlsplit(get_config().runner_url).port or 80
    if not 1024 <= port <= 65535 or port == runner_port:
        raise ServiceError(f"Port {port} can't be previewed", 404)
    return port


def prefix(port: int, base: bool = False) -> str:
    return f"{BASE_PREFIX if base else PREFIX}/{port}"


def upstream_path(port: int, path: str, base: bool = False) -> str:
    """The path the previewed server gets: from its root, or with the prefix it has as its base path."""
    return f"{prefix(port, True)}/{path}" if base else f"/{path}"


def target(port: int, path: str, query: str, base: bool = False) -> str:
    return f"http://{runner_host()}:{port}{upstream_path(port, path, base)}" + (f"?{query}" if query else "")


def request_headers(headers: list[tuple[str, str]], port: int) -> list[tuple[str, str]]:
    """Headers for the previewed server: it sees itself as localhost (dev servers check that), and
    never the PlainBoard login."""
    out = [("host", f"localhost:{port}")]
    for name, value in headers:
        key = name.lower()
        if key in _DROP_REQUEST:
            continue
        if key == "cookie":
            kept = [c for c in value.split(";") if not _own_cookie(c.strip().split("=", 1)[0])]
            if not kept:
                continue
            value = ";".join(kept).strip()
        elif key == "origin":
            value = f"http://localhost:{port}"
        elif key == "referer":
            parts = urlsplit(value)
            value = f"http://localhost:{port}" + (parts.path.removeprefix(prefix(port)) or "/") + (f"?{parts.query}" if parts.query else "")
        out.append((name, value))
    return out


def response_headers(headers: list[tuple[str, str]], port: int, secure: bool = True, sandbox: bool = False) -> list[tuple[str, str]]:
    """Headers for the browser. ``secure``: the browser reached PlainBoard over HTTPS. ``sandbox``: the
    page runs in an origin of its own, so every request it sends is cross-origin to the browser: the
    Referer keeps its path only under a policy that sends it everywhere (the root redirect needs it)."""
    out = [("Content-Security-Policy", SANDBOX), ("Referrer-Policy", "no-referrer-when-downgrade")] if sandbox else []
    for name, value in headers:
        key = name.lower()
        if key in _DROP_RESPONSE or (sandbox and key == "referrer-policy"):
            continue
        if key == "location":
            value = rewrite_location(value, port)
        elif key == "referrer-policy":
            value = repair_referrer_policy(value)
        elif key == "set-cookie":
            repaired = repair_cookie(value, secure)
            if repaired is None:
                continue
            value = repaired
        out.append((name, value))
    return out


def repair_referrer_policy(value: str) -> str:
    """Requests of a preview page to the root are recognised by their Referer. A policy that hides
    the page from its own site would break that, so it is replaced by the nearest one that does not
    (what other sites get to see stays as strict)."""
    tokens = [token.strip().lower() for token in value.split(",") if token.strip()]
    return _PATHLESS_REFERRER.get(tokens[-1], value) if tokens else value


def repair_cookie(value: str, secure: bool) -> str | None:
    """A cookie of the previewed server, as the browser accepts it at PlainBoard's address. The server
    sets it for localhost: a Domain would not match, and Secure is refused without HTTPS. None: it
    would replace one of PlainBoard's own cookies."""
    first, *attributes = [part.strip() for part in value.split(";")]
    name = first.split("=", 1)[0].strip()
    if _own_cookie(name):
        return None
    kept = []
    for attribute in attributes:
        key, _, setting = attribute.partition("=")
        key = key.strip().lower()
        if key == "domain" or (key == "secure" and not secure):
            continue
        if key == "samesite" and setting.strip().lower() == "none" and not secure:
            attribute = "SameSite=Lax"  # None needs Secure
        kept.append(attribute)
    return "; ".join([first, *kept])


def rewrite_location(location: str, port: int) -> str:
    """A redirect of the previewed server, as a path under its prefix."""
    parts = urlsplit(location)
    own = not parts.netloc or (parts.hostname in _LOCAL_HOSTS | {runner_host()} and parts.port == port)
    if not own or not parts.path.startswith("/") or parts.path.startswith((prefix(port) + "/", prefix(port, True) + "/")):
        return location
    return prefix(port) + parts.path + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")


def port_of_referer(referer: str | None, host: str | None) -> int | None:
    """The preview a request comes from, if a preview page on this site sent it."""
    if not referer:
        return None
    parts = urlsplit(referer)
    match = _REFERER_PATH.match(parts.path)
    if not match or parts.netloc != host:
        return None
    return int(match[1])


def port_of_cookie(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def unreachable(port: int) -> str:
    return (
        f"Nothing answers on port {port} in the agents' environment.\n\n"
        "The server may not be running (anymore), or it only listens on 127.0.0.1: it has to listen on 0.0.0.0. "
        "On a project with a runner of its own, that runner may not be connected right now."
    )


# --- what runs: the servers a project's agents have started and left running --------------------


# When a port, or a project's browser window, was last opened, for the memory cleanup. In memory: after
# a restart everything counts as opened at the start, so nothing is stopped for not being seen before it.
_STARTED = time.monotonic()
_opened: dict[int, float] = {}
_browsed: dict[int, float] = {}


def note_opened(port: int) -> None:
    _opened[port] = time.monotonic()


def note_browsed(project_id: int) -> None:
    _browsed[project_id] = time.monotonic()


def unseen_for(seconds: float, ports: list[int], project_id: int) -> bool:
    """Nobody opened any of these ports, nor the project's browser window, for that long."""
    last = max([_STARTED, _browsed.get(project_id, 0), *(_opened.get(p, 0) for p in ports)])
    return time.monotonic() - last >= seconds


def servers(jobs: list[Job]) -> list[Job]:
    """The running jobs that are servers: started to keep running, so no assignment waits for them.
    A test run that opens a port for a minute is not a preview."""
    return [job for job in jobs if job.assignment_id is None]


def _previewed(port: int) -> bool:
    try:
        check_port(port)
    except ServiceError:
        return False
    return True


async def running(session: AsyncSession, project_id: int, jobs: list[Job], ports: dict[str, list[JobPort]]) -> list[PreviewOut]:
    """``ports``: what the runner found for these jobs (its job id -> the ports a web server answers on).

    A job that answers on a port the app reaches keeps its ports on 127.0.0.1 to itself: they are what
    stands behind it (the backend behind a gateway, a dev server behind a proxy), often on another port
    after every start. Opened directly such a page breaks as soon as that part is replaced."""
    shares = {share.port: share for share in await shares_for_project(session, project_id)}
    out = []
    for job in jobs:
        answering = [found for found in ports.get(job.runner_id, []) if _previewed(found.port)]
        reachable = any(not found.local for found in answering)
        for found in answering:
            if found.local and reachable:
                continue
            share = shares.get(found.port)
            out.append(
                PreviewOut(
                    port=found.port,
                    title=found.title,
                    local=found.local,
                    base=found.base,
                    job_id=job.id,
                    agent_id=job.agent_id,
                    command=truncate(job.command, 200),
                    started_at=job.created_at,
                    access=share.access if share else "private",
                    shared_until=share.expires_at if share else None,
                )
            )
    return sorted(out, key=lambda p: p.port)


class Owners:
    """Which project's server listens on a port: the running server jobs and what their runners find
    them listening on. Ports can repeat across runners, so a port may have several (``project_of``
    gives them all). Asked again after ``OWNERS_SECONDS``, sooner for a port not known yet (a server
    just started), and at once when a job a port was known for no longer runs (the port was given up)."""

    def __init__(self) -> None:
        self._ports: dict[int, list[tuple[int, int]]] = {}  # port -> (job id, project id) of each
        self._at = -OWNERS_SECONDS

    async def project_of(self, session: AsyncSession, port: int, listening: Callable[[list[Job]], Awaitable[dict[str, list[int]]]]) -> list[int]:
        """``listening``: for these jobs, the ports each listens on (by the runner's job id)."""
        age = time.monotonic() - self._at
        known = self._ports.get(port)
        if known and age < OWNERS_SECONDS:
            jobs = [await session.get(Job, job_id) for job_id, _ in known]
            if all(job is not None and job.status == JobStatus.RUNNING for job in jobs):
                return [project_id for _, project_id in known]
        elif not known and age < 1.0:
            return []
        jobs = servers(list(await session.scalars(select(Job).where(Job.status == JobStatus.RUNNING))))
        by_runner = {job.runner_id: job for job in jobs}
        found = await listening(jobs) if jobs else {}
        ports: dict[int, list[tuple[int, int]]] = {}
        for rid, listened in found.items():
            if (job := by_runner.get(rid)) is not None:
                for p in listened:
                    if _previewed(p):
                        ports.setdefault(p, []).append((job.id, job.project_id))
        self._ports, self._at = ports, time.monotonic()
        return [project_id for _, project_id in self._ports.get(port, [])]


# --- from PlainBoard to the preview origin -------------------------------------------------------


def enter_token(user_id: int, version: int, session_id: str, port: int) -> str:
    return sign({"u": user_id, "v": version, "s": session_id, "p": port}, "preview-enter")


def _read(value: str | None, purpose: str, max_age: int, port: int) -> tuple[int, int, str] | None:
    data = unsign(value, purpose, max_age)
    try:
        return (int(data["u"]), int(data["v"]), str(data["s"])) if data and data.get("p") == port else None
    except (KeyError, TypeError, ValueError):
        return None


def read_enter(token: str | None, port: int) -> tuple[int, int, str] | None:
    """The user (id, session version, session) a link to the preview origin was made for."""
    return _read(token, "preview-enter", ENTER_MAX_AGE, port)


def auth_cookie(port: int) -> str:
    return f"{AUTH_COOKIE_PREFIX}{port}"


def auth_value(user_id: int, version: int, session_id: str, port: int) -> str:
    return sign({"u": user_id, "v": version, "s": session_id, "p": port}, "preview-auth")


def read_auth(value: str | None, port: int) -> tuple[int, int, str] | None:
    return _read(value, "preview-auth", AUTH_MAX_AGE, port)


def enter_next(port: int, next_path: str) -> str:
    """Where the preview origin sends the visitor on: only into this port's preview."""
    if next_path.startswith((prefix(port) + "/", prefix(port, True) + "/")) and "//" not in next_path and "\\" not in next_path:
        return next_path
    return prefix(port) + "/"


# --- sharing: without a share only the signed-in user opens a preview ---------------------------


def _hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 200_000).hex()}"


async def share(
    session: AsyncSession, project_id: int, port: int, access: str, *, password: str | None = None, days: int = 7, agent_id: int | None = None
) -> tuple[PreviewShare | None, str | None]:
    """Set who may open a preview without the login. Returns the share (None: private again) and the
    password if one was generated."""
    check_port(port)
    existing = await session.scalar(select(PreviewShare).where(PreviewShare.port == port))
    if existing and existing.project_id != project_id and existing.expires_at > utcnow():
        raise ServiceError(f"Port {port} is shared by another project")
    if existing:
        await session.delete(existing)
        await session.flush()
    if access == "private":
        await session.commit()
        return None, None
    generated = None
    if access == "password" and not password:
        password = generated = secrets.token_urlsafe(9)
    row = PreviewShare(
        project_id=project_id,
        port=port,
        access=access,
        password_hash=_hash(password) if access == "password" else None,
        created_by_agent_id=agent_id,
        expires_at=utcnow() + timedelta(days=days),
    )
    session.add(row)
    await session.commit()
    return row, generated


async def active_share(session: AsyncSession, port: int) -> PreviewShare | None:
    return await session.scalar(select(PreviewShare).where(PreviewShare.port == port, PreviewShare.expires_at > utcnow()))


async def shares_for_project(session: AsyncSession, project_id: int) -> list[PreviewShare]:
    await session.execute(delete(PreviewShare).where(PreviewShare.expires_at <= utcnow()))
    await session.commit()
    return list(await session.scalars(select(PreviewShare).where(PreviewShare.project_id == project_id).order_by(PreviewShare.port)))


def _salt(row: PreviewShare) -> str:
    return (row.password_hash or "$").split("$", 1)[0]


def password_ok(row: PreviewShare, given: str) -> bool:
    return bool(row.password_hash) and hmac.compare_digest(_hash(given, _salt(row)), row.password_hash)


def unlock_cookie(port: int) -> str:
    return f"{UNLOCK_COOKIE_PREFIX}{port}"


def unlock_value(row: PreviewShare) -> str:
    return sign({"share": row.id, "salt": _salt(row)}, "preview")


def unlocked(row: PreviewShare, cookie: str | None) -> bool:
    """A new share or password on the same port needs the password again (its salt is new)."""
    data = unsign(cookie, "preview", UNLOCK_MAX_AGE)
    return bool(data) and data.get("share") == row.id and data.get("salt") == _salt(row)


def unlock_page(port: int, wrong: bool = False, base: bool = False) -> str:
    """``base``: the visitor asked for the preview under its base path and goes back there."""
    error = '<p class="e">Wrong password.</p>' if wrong else ""
    back = '<input type="hidden" name="base" value="1">' if base else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Protected preview</title>
<style>body{{font:16px system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#f4f4f5;color:#18181b}}
form{{background:#fff;padding:28px;border-radius:14px;box-shadow:0 1px 3px #0002;width:min(88vw,320px)}}h1{{font-size:18px;margin:0 0 6px}}
p{{margin:0 0 16px;color:#52525b;font-size:14px}}.e{{color:#b91c1c}}input,button{{font:inherit;width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px}}
input{{border:1px solid #d4d4d8;margin-bottom:12px}}button{{border:0;background:#4f46e5;color:#fff;cursor:pointer}}
@media(prefers-color-scheme:dark){{body{{background:#18181b;color:#fafafa}}form{{background:#27272a}}p{{color:#a1a1aa}}input{{background:#18181b;color:#fafafa;border-color:#3f3f46}}}}</style></head>
<body><form method="post" action="/api/public/preview/{html.escape(str(port))}/unlock">{back}<h1>Protected preview</h1><p>Enter the password you were given.</p>{error}
<input type="password" name="password" autofocus required autocomplete="current-password" aria-label="Password"><button>Open</button></form></body></html>"""
