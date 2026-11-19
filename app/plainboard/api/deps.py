from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from starlette.requests import HTTPConnection
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import sessionmaker
from ..orchestration.run_manager import RunManager
from ..orchestration.runner_client import RunnerClient
from ..security import SESSION_COOKIE
from ..services import access
from ..services.access import Need, Principal
from ..services.errors import ServiceError
from .guard import origin_ok


async def _session() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session


def _runs(request: Request) -> RunManager:
    return request.app.state.runs


def _runner(request: Request) -> RunnerClient:
    return request.app.state.runner


async def who_is(conn: HTTPConnection) -> Principal | None:
    """Who a request or a socket comes from."""
    async with sessionmaker()() as session:
        return await access.principal(session, conn.cookies.get(SESSION_COOKIE), conn.headers.get("authorization"))


async def authorize(request: Request) -> None:
    """In front of every route: who asks, and whether the route's rule (services/access.py) lets them."""
    async with sessionmaker()() as session:
        who = request.state.principal = await access.principal(session, request.cookies.get(SESSION_COOKIE), request.headers.get("authorization"))
        await access.check(session, who, request.method, request.scope["route"].path, request.path_params)


async def socket_role(conn: HTTPConnection, project_id: int, need: Need) -> bool:
    """Whether a socket's user has this role in the project (sockets have no route rule), and the socket
    was opened by a page of PlainBoard: the login cookie would come along from any page of the same site."""
    if not origin_ok(conn.headers):
        return False
    async with sessionmaker()() as session:
        who = await access.principal(session, conn.cookies.get(SESSION_COOKIE))
        if who is None or who.user is None:
            return False
        try:
            await access.require_role(session, who.user, project_id, need)
        except ServiceError:
            return False
    return True


def _who(request: Request) -> Principal:
    who = getattr(request.state, "principal", None)
    if who is None:
        raise ServiceError("Not authenticated", 401)
    return who


Session = Annotated[AsyncSession, Depends(_session)]
Who = Annotated[Principal, Depends(_who)]
Runs = Annotated[RunManager, Depends(_runs)]
Runner = Annotated[RunnerClient, Depends(_runner)]
