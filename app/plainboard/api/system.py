"""Health, provider catalog and global settings."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from plainboard_protocol import CompletionSpec
from pydantic import BaseModel

from ..models import TaskStatus
from ..orchestration.prompts import PINNED_BUDGET_CHARS
from ..schemas import CheckOut, LimitsOut, SettingsOut, SettingsUpdate
from ..services import limits as limits_service
from ..services import settings as settings_service
from ..services.errors import ServiceError
from ..orchestration.runner_client import RunnerError
from .deps import Runner, Session

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/meta")
async def meta(runner: Runner) -> dict[str, Any]:
    try:
        providers = [p.model_dump() for p in await runner.providers()]
        runner_ok = True
    except RunnerError:
        providers, runner_ok = [], False
    return {
        "providers": providers,
        "runner_ok": runner_ok,
        "statuses": [s.value for s in TaskStatus],
        "pinned_note_budget_chars": PINNED_BUDGET_CHARS,
    }


@router.get("/limits", response_model=LimitsOut)
async def limits(session: Session) -> LimitsOut:
    return await limits_service.current(session)


@router.get("/settings", response_model=SettingsOut)
async def get_settings(session: Session) -> SettingsOut:
    return SettingsOut(
        values=await settings_service.public_view(session),
        secrets=[k for k, d in settings_service.SETTINGS.items() if d.secret],
        env_fallbacks=settings_service.env_fallbacks(),
    )


@router.put("/settings", response_model=SettingsOut)
async def put_settings(body: SettingsUpdate, session: Session) -> SettingsOut:
    try:
        await settings_service.update(session, body.values)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc
    return await get_settings(session)


class ProviderCheckIn(BaseModel):
    provider: str = "claude_code"
    model: str | None = None


@router.post("/settings/check/provider", response_model=CheckOut)
async def check_provider(body: ProviderCheckIn, session: Session, runner: Runner) -> CheckOut:
    values = await settings_service.get_all(session)
    spec = CompletionSpec(
        provider=body.provider,
        model=body.model or values["default_summary_model"],
        system_prompt="You are a connectivity check. Reply with exactly: OK",
        prompt="Reply with OK.",
        credentials=settings_service.credentials(values),
    )
    result = await runner.check_provider(spec)
    return CheckOut(ok=result.ok, detail=result.detail)


class GithubCheckIn(BaseModel):
    token: str | None = None


@router.post("/settings/check/github", response_model=CheckOut)
async def check_github(body: GithubCheckIn, session: Session, runner: Runner) -> CheckOut:
    values = await settings_service.get_all(session)
    result = await runner.check_github(settings_service.credentials(values, body.token))
    return CheckOut(ok=result.ok, detail=result.detail)
