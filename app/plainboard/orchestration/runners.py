"""Which runner each project uses: the built-in one (``runner_url``), or an external runner that
connected out to PlainBoard (``api/runners.py``) and is reached through its tunnel.

Everything of a project goes to its runner: turns, jobs, trains of its repositories, previews, its
browser window, its files. What is the installation's (providers, plan limits, sign-ins, the update
check, the storage page) stays on the built-in runner. A project's requests never go to another
project's runner, so its credentials and secrets never do either.

The project → runner map is kept in memory (loaded at the start, changed by ``services/runners.assign``),
so routing a call costs no query."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from websockets.asyncio.client import connect

from ..models import Project
from .runner_client import RunnerClient
from .tunnel import Tunnel, TunnelTransport

log = logging.getLogger(__name__)


class Runners:
    def __init__(self, builtin: Any) -> None:
        self.builtin = builtin
        self._of_project: dict[int, int] = {}  # project id -> external runner id
        self._clients: dict[int, Any] = {}
        self._previews: dict[int, httpx.AsyncClient] = {}
        self.tunnels: dict[int, Tunnel] = {}
        self.hello: dict[int, dict] = {}  # what each connected runner said when it connected
        self.away_since: dict[int, float] = {}  # runners that were connected and are not (monotonic)
        self.on_connect: Callable[[int], None] | None = None  # the dispatcher starts what waited
        self.make_client: Callable[[int], Any] = self._tunnel_client

    def _tunnel_client(self, runner_id: int) -> RunnerClient:
        return RunnerClient("http://runner", transport=TunnelTransport(lambda: self.tunnels.get(runner_id)))

    async def load(self, session: AsyncSession) -> None:
        self._of_project = {pid: rid for pid, rid in (await session.execute(select(Project.id, Project.runner_id).where(Project.runner_id.is_not(None)))).all()}

    def assign(self, project_id: int, runner_id: int | None) -> None:
        if runner_id is None:
            self._of_project.pop(project_id, None)
        else:
            self._of_project[project_id] = runner_id

    def runner_id(self, project_id: int | None) -> int | None:
        return self._of_project.get(project_id) if project_id is not None else None

    def projects_of(self, runner_id: int | None) -> set[int] | None:
        """The projects on an external runner; None for the built-in one (all others)."""
        return None if runner_id is None else {p for p, r in self._of_project.items() if r == runner_id}

    def client(self, runner_id: int | None) -> Any:
        if runner_id is None:
            return self.builtin
        if runner_id not in self._clients:
            self._clients[runner_id] = self.make_client(runner_id)
        return self._clients[runner_id]

    def of(self, project_id: int | None) -> Any:
        """The runner of a project."""
        return self.client(self.runner_id(project_id))

    def in_use(self) -> list[int | None]:
        """The built-in runner and every external runner a project uses."""
        return [None, *sorted(set(self._of_project.values()))]

    def connected(self, runner_id: int | None) -> bool:
        return runner_id is None or ((tunnel := self.tunnels.get(runner_id)) is not None and not tunnel.closed)

    def ready(self, project_id: int) -> bool:
        """Turns of the project can start: its runner is there. Else they wait in the queue."""
        return self.connected(self.runner_id(project_id))

    def attach(self, runner_id: int, tunnel: Tunnel, hello: dict) -> Tunnel | None:
        """A runner connected; returns the connection it replaces (an older one of the same runner)."""
        old = self.tunnels.get(runner_id)
        self.tunnels[runner_id] = tunnel
        self.hello[runner_id] = hello
        self.away_since.pop(runner_id, None)
        if self.on_connect:
            self.on_connect(runner_id)
        return old

    def detach(self, runner_id: int, tunnel: Tunnel) -> bool:
        """A connection ended. False if a newer one of the runner had replaced it already."""
        if self.tunnels.get(runner_id) is not tunnel:
            return False
        del self.tunnels[runner_id]
        self.away_since[runner_id] = time.monotonic()
        return True

    def forget(self, runner_id: int) -> None:
        """A runner was removed."""
        if (tunnel := self.tunnels.pop(runner_id, None)) is not None:
            tunnel.close()
        self._clients.pop(runner_id, None)
        self.hello.pop(runner_id, None)
        self.away_since.pop(runner_id, None)
        self._previews.pop(runner_id, None)

    # --- what is not a RunnerClient call: previews and sockets ------------------------------------

    def preview_client(self, runner_id: int | None, builtin: httpx.AsyncClient) -> httpx.AsyncClient:
        if runner_id is None:
            return builtin
        if runner_id not in self._previews:
            self._previews[runner_id] = httpx.AsyncClient(
                transport=TunnelTransport(lambda: self.tunnels.get(runner_id)), timeout=httpx.Timeout(None, connect=5), follow_redirects=False
            )
        return self._previews[runner_id]

    async def socket(self, runner_id: int | None, port: int | None, path: str, *, url: str, host: str | None = None, **options: Any) -> Any:
        """A WebSocket on a runner: ``url`` for the built-in one (``host`` where to connect), ``port``
        and ``path`` on an external one (None: its own API). Raises OSError, TimeoutError or InvalidHandshake."""
        if runner_id is None:
            if host is not None:
                options.update(host=host, port=port)
            return await connect(url, **options)
        tunnel = self.tunnels.get(runner_id)
        if tunnel is None or tunnel.closed:
            raise OSError("The runner is not connected")
        headers = [(k, v) for k, v in options.get("additional_headers") or []]
        if options.get("origin"):
            headers.append(("origin", options["origin"]))
        return await tunnel.open_socket(port, path, headers, options.get("subprotocols"), timeout=options.get("open_timeout") or 30)

    async def aclose(self) -> None:
        for client in self._previews.values():
            await client.aclose()
        for client in self._clients.values():
            if hasattr(client, "aclose"):
                await client.aclose()
