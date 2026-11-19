"""Which web servers the running jobs have started, read from /proc: no agent has to say it.

A job's shell starts a session of its own (jobs.py), so every process it starts carries that session,
and a listening socket one of them holds is the job's. Each such port is asked once whether a web
server answers there, and for the title of its page. A server that was given /preview-base/<port>/
as its base path (the app's preview offers that to apps that break behind /preview/<port>/) answers
only there: it is asked there too, and said to be such a one. Not found: a server that detaches
itself into a session of its own (a daemon), and one started as another user (sudo).
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import time
from urllib.parse import urljoin, urlsplit

import httpx
from plainboard_protocol import JobPort

from . import jobs

MIN_PORT = 1024  # as the app's preview: only ports an agent can open itself
PROBE_SECONDS = 2
MAX_HOPS = 3  # "/" often redirects to the page that has the title
MAX_PAGE = 64 * 1024
MAX_TITLE = 80
# How long an answer holds: a server still starting is asked again soon, a page with a title rarely.
NOTHING_SECONDS = 15
UNTITLED_SECONDS = 60
TITLED_SECONDS = 300
BASE_PREFIX = "/preview-base"  # as in the app (services/preview.py)
_LISTEN = "0A"
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

# (job id, port) -> ((the title, whether it has the base path), None if no web server answered; and when that was asked)
_seen: dict[tuple[str, int], tuple[tuple[str, bool] | None, float]] = {}


def _is_local(address: str) -> bool:
    """A listening address of /proc/net/tcp[6] (hex, each 32 bits in host byte order) that only this machine reaches."""
    if len(address) == 8:
        return address.upper().endswith("7F")  # 127.x.x.x
    return address == "0" * 24 + "01000000" or (address.upper().startswith("0" * 16 + "FFFF0000") and address.upper().endswith("7F"))


def _listening() -> dict[int, tuple[int, bool]]:
    """Socket inode -> (port, local only) of every listening TCP socket."""
    found: dict[int, tuple[int, bool]] = {}
    for name in ("tcp", "tcp6"):
        try:
            with open(f"/proc/net/{name}", encoding="ascii") as fh:
                lines = fh.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != _LISTEN:
                continue
            address, _, port = fields[1].rpartition(":")
            if inode := int(fields[9]):
                found[inode] = (int(port, 16), _is_local(address))
    return found


def _sockets(pid: int) -> set[int]:
    inodes = set()
    try:
        with os.scandir(f"/proc/{pid}/fd") as fds:
            for fd in fds:
                try:
                    target = os.readlink(fd.path)
                except OSError:
                    continue
                if target.startswith("socket:["):
                    inodes.add(int(target[8:-1]))
    except OSError:  # it ended meanwhile, or it runs as another user
        pass
    return inodes


def find(sessions: dict[int, str]) -> dict[str, dict[int, bool]]:
    """``sessions``: session id -> job id. Returns job id -> {port: local only}."""
    listening = _listening()
    found: dict[str, dict[int, bool]] = {}
    if not listening or not sessions:
        return found
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            job_id = sessions.get(os.getsid(int(entry)))
        except OSError:
            continue
        if job_id is None:
            continue
        for inode in _sockets(int(entry)) & listening.keys():
            port, local = listening[inode]
            if port >= MIN_PORT:
                ports = found.setdefault(job_id, {})
                ports[port] = ports.get(port, True) and local  # 0.0.0.0 and ::1 on one port: reachable
    return found


def _own(url: str, location: str | None, port: int) -> str | None:
    """Where a redirect leads, if that is still this server."""
    if not location:
        return None
    target = urljoin(url, location)
    parts = urlsplit(target)
    return target if parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1") and parts.port == port else None


async def _page(client: httpx.AsyncClient, port: int, path: str = "/") -> tuple[int, str | None]:
    """What answers at a path of the port: the status, and the page's title ("" without one, None: not a page)."""
    url = f"http://localhost:{port}{path}"  # as the page sees itself in the browser window
    for _ in range(MAX_HOPS):
        async with client.stream("GET", url, headers={"accept": "text/html"}) as resp:
            if resp.is_redirect and (to := _own(url, resp.headers.get("location"), port)):
                url = to
                continue
            if "html" not in resp.headers.get("content-type", ""):
                return resp.status_code, None
            body = b""
            try:
                async with asyncio.timeout(PROBE_SECONDS):
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) >= MAX_PAGE or b"</title>" in body.lower():
                            break
            except (TimeoutError, httpx.HTTPError):
                pass  # it answered as a web server; a page that never ends has no title for us
            match = _TITLE.search(body.decode(errors="replace"))
            return resp.status_code, " ".join(html.unescape(match[1]).split())[:MAX_TITLE] if match else ""
    return 508, None  # it only redirects


async def _probe(client: httpx.AsyncClient, port: int) -> tuple[str, bool] | None:
    """The title of the page the server starts with ("" without one), and whether that page is under its
    base path instead of at the root. None: no web server answers there."""
    try:
        status, title = await _page(client, port)
        if status >= 400:  # nothing at the root: a server with the base path answers there instead
            based_status, based_title = await _page(client, port, f"{BASE_PREFIX}/{port}/")
            if based_status < 400 or (based_title is not None and title is None):
                return based_title or "", True
        return title or "", False
    except (httpx.HTTPError, OSError, ValueError):
        return None


def _holds(answer: tuple[str, bool] | None) -> float:
    return NOTHING_SECONDS if answer is None else TITLED_SECONDS if answer[0] else UNTITLED_SECONDS


async def listening_of_jobs(ids: list[str]) -> dict[str, list[int]]:
    """The ports these jobs listen on (from MIN_PORT), without asking what answers there."""
    sessions = jobs.sessions(ids)
    found = await asyncio.to_thread(find, sessions) if sessions else {}
    return {job_id: sorted(ports) for job_id, ports in found.items()}


async def of_jobs(ids: list[str]) -> dict[str, list[JobPort]]:
    """The web servers of these jobs, as far as they run: job id -> its ports."""
    sessions = jobs.sessions(ids)
    found = await asyncio.to_thread(find, sessions) if sessions else {}
    now = time.monotonic()
    current = {(job_id, port) for job_id, ports in found.items() for port in ports}
    for key in [key for key in _seen if key not in current and (key[0] in ids or not jobs.is_running(key[0]))]:
        del _seen[key]
    due = [key for key in current if key not in _seen or now - _seen[key][1] > _holds(_seen[key][0])]
    if due:
        async with httpx.AsyncClient(timeout=PROBE_SECONDS, follow_redirects=False) as client:
            answers = await asyncio.gather(*(_probe(client, port) for _, port in due))
        _seen.update({key: (answer, now) for key, answer in zip(due, answers)})
    return {
        job_id: [
            JobPort(port=port, local=local, title=answer[0], base=answer[1])
            for port, local in sorted(ports.items())
            if (answer := _seen[(job_id, port)][0]) is not None
        ]
        for job_id, ports in found.items()
    }
