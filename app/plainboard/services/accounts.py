"""Provider accounts: several Claude or Codex subscriptions (or API keys) side by side, and the
endpoints of other providers (a local model server, a model host) with their address, key and models.

A run uses the agent's pinned account, otherwise the account it ran on last (the provider's prompt
cache is per account) while that cache is warm and nothing better is free, otherwise the best one in
this order: subscriptions below ``SWITCH_AT``, then the rest of nearly full subscriptions, then API
keys (they cost money per token), then used-up subscriptions. Among subscriptions alike, the one with
the most usage left in absolute terms: what is left of it times the size of its plan.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from typing import Any

from plainboard_protocol import ModelInfo, ProviderInfo
from sqlalchemy import func, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, Agent, Conversation, Project, Run, utcnow
from ..schemas import AccountCreate, AccountOut, AccountUpdate
from ..security import decrypt, encrypt
from . import limits as limits_service
from .errors import NotFound, ServiceError

AUTH_MODES = {"claude_code": ("oauth_token", "api_key", "runner_login"), "codex": ("chatgpt", "api_key")}
# Every other provider is an endpoint (ProviderInfo.endpoint): which ones exist is the runner's list, not the app's.
ENDPOINT_MODES = ("api_key",)
LABELS = {"claude_code": "Claude", "codex": "Codex"}
ENV_SECRETS = {("claude_code", "oauth_token"): "CLAUDE_CODE_OAUTH_TOKEN", ("claude_code", "api_key"): "ANTHROPIC_API_KEY", ("codex", "api_key"): "OPENAI_API_KEY"}
SWITCH_AT = 0.95  # an account this full hands new turns to one with more room; its rest is used last
# The prompt cache lives an hour (Claude Code's ENABLE_PROMPT_CACHING_1H): a conversation that had no
# turn for longer loses nothing by moving to the account with the most room.
CACHE_SECONDS = 3600


def _check_mode(provider: str, mode: str) -> None:
    if mode not in AUTH_MODES.get(provider, ENDPOINT_MODES):
        raise ServiceError(f"Unknown sign-in for {provider}: {mode}")


def _models(names: list[str]) -> list[str]:
    """As typed: without blanks, each once, in the order given."""
    return list(dict.fromkeys(name.strip() for name in names if name.strip()))


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
    query = select(Account).order_by(Account.provider, Account.id)  # a provider's accounts stand together, in the order they were added
    if provider:
        query = query.where(Account.provider == provider)
    return list((await session.scalars(query)).all())


async def get(session: AsyncSession, account_id: int) -> Account:
    account = await session.get(Account, account_id)
    if account is None:
        raise NotFound("Account not found")
    return account


async def create(session: AsyncSession, data: AccountCreate, name: str = "") -> Account:
    """``name``: what the provider is called, for the label of an account that got none."""
    _check_mode(data.provider, data.auth_mode)
    count = len(await list_accounts(session, data.provider))
    label = data.label.strip() or ((name or LABELS.get(data.provider) or data.provider) + (f" {count + 1}" if count else ""))
    account = Account(
        provider=data.provider,
        label=label[:100],
        key=uuid.uuid4().hex[:12],
        auth_mode=data.auth_mode,
        secret_enc=encrypt(data.secret),
        enabled=True,
        endpoint_url=data.endpoint_url.strip(),
        models=_models(data.models),
        context_tokens=data.context_tokens,
        plan_size=data.plan_size,
    )
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
    if data.endpoint_url is not None:
        account.endpoint_url = data.endpoint_url.strip()
    if data.models is not None:
        account.models = _models(data.models)
    if data.context_tokens is not None:
        account.context_tokens = data.context_tokens
    if data.plan_size is not None:
        account.plan_size = data.plan_size or None
    await session.commit()
    return account


async def delete(session: AsyncSession, account: Account) -> None:
    await session.execute(sql_update(Agent).where(Agent.account_id == account.id).values(account_id=None))
    await session.delete(account)
    await session.commit()


async def check_pin(session: AsyncSession, account_id: int | None, provider: str, project_id: int | None = None) -> None:
    if account_id is None:
        return
    if (await get(session, account_id)).provider != provider:
        raise ServiceError("The account belongs to another provider")
    project = await session.get(Project, project_id) if project_id else None
    if project is not None and project.account_ids and account_id not in project.account_ids:
        raise ServiceError("This project may not use that account")


async def limit_project(session: AsyncSession, project: Project, account_ids: list[int]) -> Project:
    """The only accounts the project's agents may run on from now on; none: any. What the project or
    one of its agents had chosen outside of them is chosen automatically again."""
    known = {a.id for a in await list_accounts(session)}
    if unknown := [i for i in account_ids if i not in known]:
        raise NotFound(f"Account not found: {unknown[0]}")
    project.account_ids = list(dict.fromkeys(account_ids))
    if project.account_ids:
        if project.account_id not in project.account_ids:
            project.account_id = None
        await session.execute(sql_update(Agent).where(Agent.project_id == project.id, Agent.account_id.is_not(None), Agent.account_id.not_in(project.account_ids)).values(account_id=None))
    await session.commit()
    return project


async def shut_out(session: AsyncSession, provider: str, agent: Agent) -> str:
    """Why no account was picked, if the project's limit is the reason: the installation has
    accounts of this provider, the project may use none of them. Else ""."""
    project = await session.get(Project, agent.project_id)
    if project is None or not project.account_ids:
        return ""
    accounts = [a for a in await list_accounts(session, provider) if a.enabled]
    if not accounts or any(a.id in project.account_ids for a in accounts):
        return ""
    return (
        f"This project may only use some of the installation's subscriptions, and none of them is for {agent.provider}. "
        "An admin can change that (Project settings, Team), or the agent can be given a provider the project has a subscription for."
    )


USED_UP = 3


def _rank(account: Account, used: dict[str, float], size: float = 1.0) -> tuple[int, float]:
    """Lower is better: subscriptions with room, the rest of nearly full ones, API keys, used-up
    subscriptions; within each, the most usage left in absolute terms."""
    if not has_plan(account):
        return 2, 0.0
    utilization = used.get(account.key, 0.0)
    return (0 if utilization < SWITCH_AT else 1 if utilization < 1 else USED_UP), -max(0.0, 1 - utilization) * size


def used_up_now(account: Account, used: dict[str, float]) -> bool:
    return _rank(account, used)[0] == USED_UP


def plan_sizes(accounts: list[Account], said: dict[str, float]) -> dict[int, float]:
    """How large each subscription's plan is: as the user gave it, else as the provider said. One
    nobody knows counts as large as the known ones on average (as all of them, if none is known):
    percentages are only comparable between plans of the same size."""
    sizes = {a.id: a.plan_size or said.get(a.key) for a in accounts if has_plan(a)}
    known = [size for size in sizes.values() if size]
    usual = sum(known) / len(known) if known else 1.0
    return {key: size or usual for key, size in sizes.items()}


async def _cache_warm(session: AsyncSession, agent: Agent | None) -> bool:
    """The agent had a turn within the prompt cache's lifetime."""
    if agent is None:
        return False
    last = await session.scalar(select(func.max(Run.finished_at)).where(Run.agent_id == agent.id))
    return last is not None and utcnow() - last < timedelta(seconds=CACHE_SECONDS)


async def pick(
    session: AsyncSession, provider: str, agent: Agent | None = None, conv: Conversation | None = None, model: str | None = None, fresh: bool = False
) -> Account | None:
    """The account for an agent's next turn; None if the provider has no (enabled) account.
    ``model``: what the turn runs on, the agent's own model if not given. ``fresh``: the turn starts
    a new session (a new piece of work, a compressed context), so there is no cache to keep."""
    accounts = [a for a in await list_accounts(session, provider) if a.enabled]
    project = await session.get(Project, agent.project_id) if agent else None
    if project is not None and project.account_ids:  # a project limited to some accounts never gets another one
        accounts = [a for a in accounts if a.id in project.account_ids]
    if not accounts:
        return None
    # Endpoints of one provider differ in what they offer (two model servers, say): one that names the model.
    model = model or (agent.model if agent else None)
    accounts = [a for a in accounts if model and model in (a.models or [])] or accounts
    # The agent's own subscription, else its project's. One of another provider does not count.
    wanted = agent.account_id if agent else None
    if wanted is None and project is not None:
        wanted = project.account_id
    if wanted and (pinned := next((a for a in accounts if a.id == wanted), None)):
        return pinned
    used = await limits_service.usage(session)
    sizes = plan_sizes(accounts, await limits_service.plan_sizes(session))

    def rank(account: Account) -> tuple[int, float]:
        return _rank(account, used, sizes.get(account.id, 1.0))

    best = min(accounts, key=rank)
    last = next((a for a in accounts if conv and a.key == conv.account_key), None)
    # Moving costs the prompt cache: a conversation stays unless its account is (nearly) used up or the cache is cold anyway.
    if last and not fresh and rank(last)[0] == rank(best)[0] != USED_UP and await _cache_warm(session, agent):
        return last
    return best


async def other_with_room(session: AsyncSession, provider: str, agent: Agent, conv: Conversation | None, used_up: str) -> Account | None:
    """Another account for a turn the account ``used_up`` (by key) was turned away on; None if
    there is none that is not used up as well."""
    account = await pick(session, provider, agent, conv)
    if account is None or account.key == used_up or used_up_now(account, await limits_service.usage(session)):
        return None
    return account


def fields(account: Account | None) -> dict[str, Any]:
    """The account's part of the run's ``Credentials``."""
    if account is None:
        return {}
    value = secret(account)
    if account.provider == "claude_code":
        return {"claude_auth_mode": account.auth_mode, "claude_oauth_token" if account.auth_mode == "oauth_token" else "anthropic_api_key": value}
    if account.provider == "codex":
        return {"codex_auth_mode": account.auth_mode, "codex_account": account.key, "openai_api_key": value}
    return {"endpoint_url": account.endpoint_url, "endpoint_key": value, "endpoint_context": account.context_tokens}


async def catalog(session: AsyncSession, providers: list[ProviderInfo]) -> list[ProviderInfo]:
    """The providers agents can run on, from the runner's list. A provider that is an endpoint counts
    once an account of it offers a model: what its accounts name, else its own usual models."""
    accounts = [a for a in await list_accounts(session) if a.enabled]
    out = []
    for provider in providers:
        if provider.endpoint is None:
            out.append(provider)
            continue
        mine = [a for a in accounts if a.provider == provider.name]
        if not mine:
            continue
        known = {m.id: m for m in provider.models}
        named = list(dict.fromkeys(name for a in mine for name in a.models or []))
        models = [known.get(name) or ModelInfo(id=name, label=name) for name in named] or provider.models
        if models:
            out.append(provider.model_copy(update={"models": models}))
    return out
