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
from .deps import Runner, Session

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountOut])
async def list_accounts(session: Session) -> list[AccountOut]:
    return [account_service.to_out(a) for a in await account_service.list_accounts(session)]


@router.post("", response_model=AccountOut, status_code=201)
async def create(body: AccountCreate, session: Session) -> AccountOut:
    return account_service.to_out(await account_service.create(session, body))


@router.patch("/{account_id}", response_model=AccountOut)
async def update(account_id: int, body: AccountUpdate, session: Session) -> AccountOut:
    return account_service.to_out(await account_service.update(session, await account_service.get(session, account_id), body))


@router.delete("/{account_id}", status_code=204)
async def delete(account_id: int, session: Session, runner: Runner) -> None:
    account = await account_service.get(session, account_id)
    if account.provider == "codex" and account.auth_mode == "chatgpt":
        try:  # don't leave the subscription signed in on the runner
            await runner.provider_account(account.provider, account.key, "logout")
        except RunnerError:
            pass
    await account_service.delete(session, account)


class CheckIn(BaseModel):
    model: str


@router.post("/{account_id}/check", response_model=CheckOut)
async def check(account_id: int, body: CheckIn, session: Session, runner: Runner) -> CheckOut:
    account = await account_service.get(session, account_id)
    spec = CompletionSpec(
        provider=account.provider,
        model=body.model,
        system_prompt="You are a connectivity check. Reply with exactly: OK",
        prompt="Reply with OK.",
        credentials=settings_service.credentials(await settings_service.get_all(session), account=account_service.fields(account)),
    )
    result = await runner.check_provider(spec)
    return CheckOut(ok=result.ok, detail=result.detail)


async def _signin(session: Session, runner: Runner, account_id: int, action: str | None = None) -> ProviderAccount:
    account = await account_service.get(session, account_id)
    try:
        return await runner.provider_account(account.provider, account.key, action)
    except RunnerError as exc:
        raise ServiceError(str(exc), 502) from exc


@router.get("/{account_id}/signin", response_model=ProviderAccount)
async def signin_state(account_id: int, session: Session, runner: Runner) -> ProviderAccount:
    return await _signin(session, runner, account_id)


@router.post("/{account_id}/signin", response_model=ProviderAccount)
async def signin(account_id: int, session: Session, runner: Runner) -> ProviderAccount:
    """Starts a device sign-in: the answer has the URL to open and the code to enter."""
    return await _signin(session, runner, account_id, "login")


@router.delete("/{account_id}/signin", response_model=ProviderAccount)
async def signout(account_id: int, session: Session, runner: Runner) -> ProviderAccount:
    return await _signin(session, runner, account_id, "logout")
