from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import sessionmaker
from ..orchestration.run_manager import RunManager
from ..orchestration.runner_client import RunnerClient


async def _session() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session


def _runs(request: Request) -> RunManager:
    return request.app.state.runs


def _runner(request: Request) -> RunnerClient:
    return request.app.state.runner


Session = Annotated[AsyncSession, Depends(_session)]
Runs = Annotated[RunManager, Depends(_runs)]
Runner = Annotated[RunnerClient, Depends(_runner)]
