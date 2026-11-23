"""Health, provider catalog and global settings."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query, Request
from pydantic import BaseModel

from ..config import get_config
from ..models import TaskStatus
from ..orchestration.prompts import PINNED_BUDGET_CHARS
from ..schemas import AccessKeyOut, CheckOut, LimitsOut, PauseOut, PublicApiKeyIn, SettingsOut, SettingsUpdate, UsageOut
from ..services import access_keys as access_key_service
from ..services import accounts as account_service
from ..services import db_read as db_read_service
from ..services import limits as limits_service
from ..services import pause as pause_service
from ..services import settings as settings_service
from ..services import usage as usage_service
from ..services import version as version_service
from ..services.errors import ServiceError
from ..orchestration.runner_client import RunnerError
from .deps import Runner, Runs, Session, Who

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


RUNNER_DOWN_AFTER_SECONDS = 30  # one slow answer of the runner is not "not reachable"
_runner_seen: dict[str, Any] = {"providers": [], "failing_since": None}


@router.get("/meta")
async def meta(runner: Runner, session: Session) -> dict[str, Any]:
    try:
        every = await runner.providers()
        _runner_seen.update(providers=every, failing_since=None)
        runner_ok = True
    except RunnerError:
        since = _runner_seen["failing_since"] = _runner_seen["failing_since"] or time.monotonic()
        runner_ok = time.monotonic() - since < RUNNER_DOWN_AFTER_SECONDS
        every = _runner_seen["providers"] if runner_ok else []
    providers = [p.model_dump() for p in await account_service.catalog(session, every)] if every else []
    return {
        "providers": providers,
        # Providers that are an endpoint, set up or not: what an account can be added for.
        "endpoints": [p.model_dump() for p in every if p.endpoint is not None],
        "runner_ok": runner_ok,
        "statuses": [s.value for s in TaskStatus],
        "pinned_note_budget_chars": PINNED_BUDGET_CHARS,
        # The running commit and how far it is behind the newest PlainBoard; None: not known or not wanted.
        "version": version_service.current() if await settings_service.get(session, "update_check") else None,
        "database": "postgresql" if get_config().postgres else "sqlite",
    }


@router.get("/pause", response_model=PauseOut)
async def get_pause(session: Session) -> PauseOut:
    """Whether everything is paused, and what still moves."""
    return await pause_service.status(session)


@router.post("/pause", response_model=PauseOut)
async def pause_everything(session: Session, runs: Runs, who: Who) -> PauseOut:
    """Bring all projects to rest, e.g. before an update. Again: also what started meanwhile."""
    await runs.pause_all(session, who.name)
    return await pause_service.status(session)


@router.delete("/pause", response_model=PauseOut)
async def resume_everything(session: Session, runs: Runs) -> PauseOut:
    await runs.resume_all(session)
    return await pause_service.status(session)


@router.get("/access-keys", response_model=list[AccessKeyOut])
async def list_access_keys(session: Session) -> list[AccessKeyOut]:
    return [AccessKeyOut.model_validate(k) for k in await access_key_service.list_all(session)]


@router.post("/access-keys", response_model=AccessKeyOut, status_code=201)
async def create_access_key(body: PublicApiKeyIn, session: Session) -> AccessKeyOut:
    """A key that may read the whole API and change nothing. The answer carries the key itself, this once."""
    key, secret = await access_key_service.create(session, body.label)
    out = AccessKeyOut.model_validate(key)
    out.key = secret
    return out


@router.delete("/access-keys/{key_id}", status_code=204)
async def revoke_access_key(key_id: int, session: Session) -> None:
    await access_key_service.delete(session, key_id)


@router.get("/inspect/schema")
async def inspect_schema() -> list[dict[str, Any]]:
    """The database's tables, their sizes and readable columns."""
    return await db_read_service.schema()


@router.get("/inspect/query")
async def inspect_query(sql: str, limit: int = Query(default=200, ge=1), cell_chars: int = Query(default=2000, ge=0)) -> dict[str, Any]:
    """One SELECT on the database, read-only. Credentials read as NULL."""
    return await db_read_service.query(sql, limit, cell_chars)


@router.get("/limits", response_model=LimitsOut)
async def limits(session: Session, runs: Runs, who: Who) -> LimitsOut:
    runs.refresh_limits()
    out = await limits_service.current(session)
    if who.user is not None and not who.user.is_admin:  # what the user and their projects may use
        seen = {a.id for a in await account_service.visible(session, who.user)}
        out.accounts = [a for a in out.accounts if a.account_id in seen]
    return out


@router.get("/usage", response_model=UsageOut)
async def usage(session: Session, runs: Runs) -> UsageOut:
    """Disk and memory, in total, per project and agent; folder sizes as last measured."""
    return await usage_service.overview(session, runs)


@router.post("/usage/measure", response_model=UsageOut)
async def measure_usage(session: Session, runs: Runs) -> UsageOut:
    """Measure the folders again, in the background."""
    return await usage_service.overview(session, runs, measure=True)


@router.get("/settings", response_model=SettingsOut)
async def get_settings(session: Session) -> SettingsOut:
    return SettingsOut(
        values=await settings_service.public_view(session),
        secrets=[k for k, d in settings_service.SETTINGS.items() if d.secret],
        env_fallbacks=settings_service.env_fallbacks(),
    )


@router.put("/settings", response_model=SettingsOut)
async def put_settings(body: SettingsUpdate, session: Session, runs: Runs, request: Request, background: BackgroundTasks) -> SettingsOut:
    try:
        await settings_service.update(session, body.values)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc
    runs.recheck()  # more parallel agents: waiting turns may start now
    if "update_check" in body.values:
        background.add_task(request.app.state.updates.tick)  # switched on: don't wait hours for the first answer
    return await get_settings(session)


class SshKeyIn(BaseModel):
    private_key: str | None = None  # empty: PlainBoard creates a key pair


@router.post("/settings/git-ssh-key", response_model=SettingsOut)
async def set_git_ssh_key(body: SshKeyIn, session: Session) -> SettingsOut:
    """Create (or take over) the SSH key for all repositories with an SSH address. Replaces the old one."""
    await settings_service.set_ssh_key(session, (body.private_key or "").strip() or None)
    return await get_settings(session)


@router.delete("/settings/git-ssh-key", response_model=SettingsOut)
async def delete_git_ssh_key(session: Session) -> SettingsOut:
    await settings_service.set_ssh_key(session, "")
    return await get_settings(session)


class GithubCheckIn(BaseModel):
    token: str | None = None


@router.post("/settings/check/github", response_model=CheckOut)
async def check_github(body: GithubCheckIn, session: Session, runner: Runner, who: Who) -> CheckOut:
    if not body.token and not who.is_admin:
        return CheckOut(ok=False, detail="Enter a token to test.")
    values = await settings_service.get_all(session)
    result = await runner.check_github(settings_service.credentials(values, body.token))
    return CheckOut(ok=result.ok, detail=result.detail)
