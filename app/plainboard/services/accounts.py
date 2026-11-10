"""Provider accounts: several Claude or Codex subscriptions (or API keys) side by side.

A run uses the agent's pinned account, otherwise the account it ran on last (the provider's prompt
cache is per account) as long as nothing better is free, otherwise the best one in this order:
subscriptions below ``SWITCH_AT``, then the rest of nearly full subscriptions, then API keys (they
cost money per token), then used-up subscriptions.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from sqlalchemy import select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, Agent, Conversation
from ..schemas import AccountCreate, AccountOut, AccountUpdate
from ..security import decrypt, encrypt
from . import limits as limits_service
from .errors import NotFound, ServiceError

AUTH_MODES = {"claude_code": ("oauth_token", "api_key", "runner_login"), "codex": ("chatgpt", "api_key")}
LABELS = {"claude_code": "Claude", "codex": "Codex"}
ENV_SECRETS = {("claude_code", "oauth_token"): "CLAUDE_CODE_OAUTH_TOKEN", ("claude_code", "api_key"): "ANTHROPIC_API_KEY", ("codex", "api_key"): "OPENAI_API_KEY"}
SWITCH_AT = 0.95  # an account this full hands new turns to one with more room; its rest is used last


def _check_mode(provider: str, mode: str) -> None:
    if provider not in AUTH_MODES:
        raise ServiceError(f"{provider} has no accounts")
    if mode not in AUTH_MODES[provider]:
        raise ServiceError(f"Unknown sign-in for {provider}: {mode}")


def has_plan(account: Account) -> bool:
    """Subscriptions have plan limits, API keys do not."""
    return account.auth_mode != "api_key"


def secret(account: Account) -> str | None:
    env = ENV_SECRETS.get((account.provider, account.auth_mode))
    return decrypt(account.secret_enc) or (os.environ.get(env) or None if env else None)


def to_out(account: Account) -> AccountOut:
    env = ENV_SECRETS.get((account.provider, account.auth_mode))
    return AccountOut.model_validate(account).model_copy(
        update={"secret_set": bool(account.secret_enc), "secret_from_env": bool(not account.secret_enc and env and os.environ.get(env))}
    )


async def list_accounts(session: AsyncSession, provider: str | None = None) -> list[Account]:
    query = select(Account).order_by(Account.id)
    if provider:
        query = query.where(Account.provider == provider)
    return list((await session.scalars(query)).all())


async def get(session: AsyncSession, account_id: int) -> Account:
    account = await session.get(Account, account_id)
    if account is None:
        raise NotFound("Account not found")
    return account


async def create(session: AsyncSession, data: AccountCreate) -> Account:
    _check_mode(data.provider, data.auth_mode)
    count = len(await list_accounts(session, data.provider))
    label = data.label.strip() or (LABELS[data.provider] + (f" {count + 1}" if count else ""))
    account = Account(provider=data.provider, label=label, key=uuid.uuid4().hex[:12], auth_mode=data.auth_mode, secret_enc=encrypt(data.secret), enabled=True)
    session.add(account)
    await session.commit()
    return account


async def update(session: AsyncSession, account: Account, data: AccountUpdate) -> Account:
    if data.auth_mode is not None and data.auth_mode != account.auth_mode:
        _check_mode(account.provider, data.auth_mode)
        account.auth_mode = data.auth_mode
        if data.secret is None:
            account.secret_enc = None  # a token is no API key
    if data.secret is not None:
        account.secret_enc = encrypt(data.secret)
    if data.label is not None and data.label.strip():
        account.label = data.label.strip()
    if data.enabled is not None:
        account.enabled = data.enabled
    await session.commit()
    return account


async def delete(session: AsyncSession, account: Account) -> None:
    await session.execute(sql_update(Agent).where(Agent.account_id == account.id).values(account_id=None))
    await session.delete(account)
    await session.commit()


async def check_pin(session: AsyncSession, account_id: int | None, provider: str) -> None:
    if account_id is not None and (await get(session, account_id)).provider != provider:
        raise ServiceError("The account belongs to another provider")


USED_UP = 3


def _rank(account: Account, used: dict[str, float]) -> tuple[int, float]:
    """Lower is better: subscriptions with room, the rest of nearly full ones, API keys, used-up subscriptions."""
    if not has_plan(account):
        return 2, 0.0
    utilization = used.get(account.key, 0.0)
    return (0 if utilization < SWITCH_AT else 1 if utilization < 1 else USED_UP), utilization


async def pick(session: AsyncSession, provider: str, agent: Agent | None = None, conv: Conversation | None = None) -> Account | None:
    """The account for an agent's next turn; None if the provider has no (enabled) account."""
    accounts = [a for a in await list_accounts(session, provider) if a.enabled]
    if not accounts:
        return None
    if agent and agent.account_id and (pinned := next((a for a in accounts if a.id == agent.account_id), None)):
        return pinned
    used = await limits_service.usage(session)
    best = min(accounts, key=lambda a: _rank(a, used))
    last = next((a for a in accounts if conv and a.key == conv.account_key), None)
    if last and _rank(last, used)[0] == _rank(best, used)[0] != USED_UP:
        return last
    return best


def fields(account: Account | None) -> dict[str, Any]:
    """The account's part of the run's ``Credentials``."""
    if account is None:
        return {}
    value = secret(account)
    if account.provider == "claude_code":
        return {"claude_auth_mode": account.auth_mode, "claude_oauth_token" if account.auth_mode == "oauth_token" else "anthropic_api_key": value}
    if account.provider == "codex":
        return {"codex_auth_mode": account.auth_mode, "codex_account": account.key, "openai_api_key": value}
    return {}
