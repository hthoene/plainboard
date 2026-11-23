"""Provider accounts: CRUD, a connectivity check, and the device sign-in of subscriptions on the runner."""

from __future__ import annotations

from fastapi import APIRouter
from plainboard_protocol import CompletionSpec, ProviderAccount
from pydantic import BaseModel

from ..orchestration.runner_client import RunnerError
from ..schemas import AccountCreate, AccountOut, AccountUpdate, CheckOut
from ..services import accounts as account_service
from ..services import settings as settings_service
from ..services.errors import ServiceError
from .deps import Runner, Session, Who

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountOut])
async def list_accounts(session: Session, who: Who) -> list[AccountOut]:
    """An admin: every account. Anyone else: what they and their projects may use, the secrets of none."""
    accounts = await account_service.visible(session, who.user) if who.user else await account_service.list_accounts(session)
    names = await account_service.owner_names(session)
    return [account_service.to_out(a, who.user, names) for a in accounts]


@router.post("", response_model=AccountOut, status_code=201)
async def create(body: AccountCreate, session: Session, runner: Runner, who: Who) -> AccountOut:
    name = ""
    if body.provider not in account_service.AUTH_MODES:  # an endpoint: which ones exist is the runner's list
        try:
            found = next((p for p in await runner.providers() if p.name == body.provider and p.endpoint is not None), None)
        except RunnerError as exc:
            raise ServiceError(str(exc), 502) from exc
        if found is None:
            raise ServiceError(f"Unknown provider '{body.provider}'")
        name = found.label
    account = await account_service.create(session, body, name, who.user)
    return account_service.to_out(account, who.user, await account_service.owner_names(session))


@router.patch("/{account_id}", response_model=AccountOut)
async def update(account_id: int, body: AccountUpdate, session: Session, who: Who) -> AccountOut:
    account = await account_service.update(session, await account_service.managed(session, account_id, who.user), body)
    return account_service.to_out(account, who.user, await account_service.owner_names(session))


@router.delete("/{account_id}", status_code=204)
async def delete(account_id: int, session: Session, runner: Runner, who: Who) -> None:
    account = await account_service.managed(session, account_id, who.user)
    if (account.provider == "codex" and account.auth_mode == "chatgpt") or (account.provider == "antigravity" and account.auth_mode == "google"):
        try:  # don't leave the subscription signed in on the runner
            await runner.provider_account(account.provider, account.key, "logout")
        except RunnerError:
            pass
    await account_service.delete(session, account)


class CheckIn(BaseModel):
    model: str


@router.post("/{account_id}/check", response_model=CheckOut)
async def check(account_id: int, body: CheckIn, session: Session, runner: Runner, who: Who) -> CheckOut:
    account = await account_service.managed(session, account_id, who.user)
    spec = CompletionSpec(
        provider=account.provider,
        model=body.model,
        system_prompt="You are a connectivity check. Reply with exactly: OK",
        prompt="Reply with OK.",
        credentials=settings_service.credentials(await settings_service.get_all(session), account=account_service.fields(account)),
    )
    result = await runner.check_provider(spec)
    return CheckOut(ok=result.ok, detail=result.detail)


class ModelsOut(BaseModel):
    models: list[str]


@router.post("/{account_id}/models", response_model=ModelsOut)
async def offered_models(account_id: int, session: Session, runner: Runner, who: Who) -> ModelsOut:
    """What the account's endpoint says it offers, to choose from. Nothing is stored."""
    account = await account_service.managed(session, account_id, who.user)
    credentials = settings_service.credentials(await settings_service.get_all(session), account=account_service.fields(account))
    try:
        return ModelsOut(models=await runner.provider_models(account.provider, credentials))
    except RunnerError as exc:
        raise ServiceError(str(exc), 502) from exc


class SignInIn(BaseModel):
    code: str = ""


async def _signin(session: Session, runner: Runner, account_id: int, action: str | None = None, code: str = "") -> ProviderAccount:
    account = await account_service.get(session, account_id)
    try:
        result = await runner.provider_account(account.provider, account.key, action, code=code)
    except RunnerError as exc:
        raise ServiceError(str(exc), 502) from exc
    if action in ("login", "logout") and (code or action == "logout"):
        runner.forget_providers()
    return result


@router.get("/{account_id}/signin", response_model=ProviderAccount)
async def signin_state(account_id: int, session: Session, runner: Runner) -> ProviderAccount:
    return await _signin(session, runner, account_id)


@router.post("/{account_id}/signin", response_model=ProviderAccount)
async def signin(account_id: int, session: Session, runner: Runner, body: SignInIn | None = None) -> ProviderAccount:
    """Starts a runner sign-in (returns URL and optional device code), or submits the pasted code/URL."""
    return await _signin(session, runner, account_id, "login", code=body.code if body else "")


@router.delete("/{account_id}/signin", response_model=ProviderAccount)
async def signout(account_id: int, session: Session, runner: Runner) -> ProviderAccount:
    return await _signin(session, runner, account_id, "logout")
