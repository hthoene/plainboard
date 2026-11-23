"""Provider accounts: several Claude or Codex subscriptions (or API keys) side by side, and the
endpoints of other providers (a local model server, a model host) with their address, key and models.

A run uses the agent's pinned account, otherwise the account it ran on last (the provider's prompt
cache is per account) while that cache is warm and nothing better is free, otherwise the best one in
this order: subscriptions below ``SWITCH_AT``, then the rest of nearly full subscriptions, then API
keys (they cost money per token), then used-up subscriptions. Among subscriptions alike, the one whose
usage would be lost soonest: what is left of it in absolute terms (what its fullest window leaves,
times the size of its plan) per hour until its longest window resets. A conversation moves off its
account only when another one is clearly better (``MARGIN``).
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import timedelta
from typing import Any

from plainboard_protocol import LimitWindow, ModelInfo, ProviderInfo
from sqlalchemy import func, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, Agent, Conversation, InboxItem, Project, Run, User, utcnow
from ..schemas import AccountCreate, AccountOut, AccountUpdate
from ..security import decrypt, encrypt
from . import limits as limits_service
from . import permissions
from . import users as user_service
from .errors import NotFound, ServiceError

AUTH_MODES = {
    "claude_code": ("oauth_token", "api_key", "runner_login"),
    "codex": ("chatgpt", "api_key"),
    "antigravity": ("google", "api_key"),
}
# Every other provider is an endpoint (ProviderInfo.endpoint): which ones exist is the runner's list, not the app's.
ENDPOINT_MODES = ("api_key",)
# What a person who is no admin may add: a token or key that travels with the run. A sign-in on the runner
# (runner_login, Codex's ChatGPT sign-in, Antigravity's Google sign-in) lives in the runner's home, which every agent on it can read: not one's own.
OWN_MODES = ("oauth_token", "api_key")
LABELS = {"claude_code": "Claude", "codex": "Codex", "antigravity": "Antigravity"}
ENV_SECRETS = {
    ("claude_code", "oauth_token"): "CLAUDE_CODE_OAUTH_TOKEN",
    ("claude_code", "api_key"): "ANTHROPIC_API_KEY",
    ("codex", "api_key"): "OPENAI_API_KEY",
    ("antigravity", "api_key"): "GEMINI_API_KEY",
}
SWITCH_AT = 0.95  # an account this full hands new turns to one with more room; its rest is used last
# The prompt cache lives an hour (Claude Code's ENABLE_PROMPT_CACHING_1H): a conversation that had no
# turn for longer loses nothing by moving to the account with the most room.
CACHE_SECONDS = 3600
# A conversation whose cache is cold, or a new session, moves to another account only if that one
# is worth this much more: small differences come and go with every report.
MARGIN = 1.25
HOUR = 3600
WEEK_HOURS = 7 * 24


def _check_mode(provider: str, mode: str, own: bool = False) -> None:
    if mode not in AUTH_MODES.get(provider, ENDPOINT_MODES):
        raise ServiceError(f"Unknown sign-in for {provider}: {mode}")
    if own and mode not in OWN_MODES:
        raise ServiceError("Your own subscriptions take a token or an API key; a sign-in on the runner is for the installation's (an admin's).")


def _models(names: list[str]) -> list[str]:
    """As typed: without blanks, each once, in the order given."""
    return list(dict.fromkeys(name.strip() for name in names if name.strip()))


def has_plan(account: Account) -> bool:
    """Subscriptions have plan limits, API keys do not."""
    return account.auth_mode != "api_key"


def _env(account: Account) -> str | None:
    """The installation's secret from the environment; a person's own subscription never falls back to it."""
    env = ENV_SECRETS.get((account.provider, account.auth_mode)) if account.owner_id is None else None
    return os.environ.get(env) or None if env else None


def secret(account: Account) -> str | None:
    return decrypt(account.secret_enc) or _env(account)


def to_out(account: Account, viewer: User | None = None, names: dict[int, str] | None = None) -> AccountOut:
    """No secret, ever. Who may not manage it sees its name, provider and state only. ``viewer`` None: an admin's view."""
    manage = viewer is None or permissions.may_manage(viewer, account)
    out = AccountOut.model_validate(account).model_copy(
        update={
            "secret_set": bool(account.secret_enc),
            "secret_from_env": bool(not account.secret_enc and _env(account)),
            "owner_name": (names or {}).get(account.owner_id, "") if account.owner_id else "",
            "shared_with": account.shared_with or [],
            "can_manage": manage,
        }
    )
    if not manage:
        out = out.model_copy(update={"secret_set": False, "secret_from_env": False, "endpoint_url": "", "shared_with": [], "share": "private" if account.owner_id else out.share})
    return out


async def owner_names(session: AsyncSession) -> dict[int, str]:
    return dict((await session.execute(select(User.id, User.name).where(User.id.in_(select(Account.owner_id))))).all())  # type: ignore[arg-type]


async def visible(session: AsyncSession, user: User) -> list[Account]:
    """The accounts a person sees: an admin all; anyone else what their projects may use (their own, what
    is shared with them, what an admin allowed them) and what a project they work in runs on."""
    accounts = await list_accounts(session)
    if user.is_admin:
        return accounts
    seen = {a.id for a in accounts if permissions.may_use(user, a)}
    for project_id in await user_service.roles(session, user):
        project = await session.get(Project, project_id)
        seen |= await permissions.account_ids(session, project) or set()
    return [a for a in accounts if a.id in seen]


async def managed(session: AsyncSession, account_id: int, user: User | None) -> Account:
    """The account, if ``user`` may change it (None: a read key, which changes nothing anyway). One the
    person cannot even see does not exist for them."""
    account = await get(session, account_id)
    if user is None or permissions.may_manage(user, account):
        return account
    if account.id not in {a.id for a in await visible(session, user)}:
        raise NotFound("Account not found")
    raise ServiceError("Only its owner or an admin can change this subscription.", 403)


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


async def create(session: AsyncSession, data: AccountCreate, name: str = "", user: User | None = None) -> Account:
    """``name``: what the provider is called, for the label of an account that got none. ``user``: who
    adds it; an admin's is the installation's, anyone else's their own (``Account.owner_id``)."""
    own = user is not None and not user.is_admin
    _check_mode(data.provider, data.auth_mode, own)
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
        owner_id=user.id if own else None,
        share="private",
    )
    session.add(account)
    await session.commit()
    return account


async def update(session: AsyncSession, account: Account, data: AccountUpdate) -> Account:
    """Whoever changes it may manage it (``managed``). Shared with fewer people: projects that may no longer
    use it choose automatically again (``release``)."""
    if data.auth_mode is not None and data.auth_mode != account.auth_mode:
        _check_mode(account.provider, data.auth_mode, account.owner_id is not None)
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
    if account.owner_id is not None and (data.share is not None or data.shared_with is not None):
        if data.shared_with is not None:
            ids = [i for i in dict.fromkeys(data.shared_with) if i != account.owner_id]
            known = set(await session.scalars(select(User.id).where(User.id.in_(ids))))
            if unknown := [i for i in ids if i not in known]:
                raise NotFound(f"User not found: {unknown[0]}")
            account.shared_with = ids
        if data.share is not None:
            account.share = data.share
        await session.flush()
        await release(session, account)
    await session.commit()
    return account


async def release(session: AsyncSession, account: Account, gone: bool = False) -> list[Project]:
    """Projects and agents that chose the account but may no longer use it (it is deleted, or shared with
    fewer people) choose automatically again; each such project's manager is told once. Not committed."""
    pinned = set(await session.scalars(select(Project.id).where(Project.account_id == account.id)))
    pinned |= set(await session.scalars(select(Agent.project_id).where(Agent.account_id == account.id)))
    out = []
    for project_id in sorted(pinned):
        project = await session.get(Project, project_id)
        if project is None or (not gone and account.id in (await permissions.account_ids(session, project) or set())):
            continue
        names = (["the project"] if project.account_id == account.id else []) + list(
            await session.scalars(select(Agent.name).where(Agent.project_id == project.id, Agent.account_id == account.id).order_by(Agent.id))
        )
        if project.account_id == account.id:
            project.account_id = None
        await session.execute(sql_update(Agent).where(Agent.project_id == project.id, Agent.account_id == account.id).values(account_id=None))
        manager = await session.scalar(select(Agent).where(Agent.project_id == project.id, Agent.is_manager.is_(True)))
        if manager is not None:
            what = "was deleted" if gone else "is no longer shared with this project's owner"
            who = f" ({', '.join(names)})"
            session.add(InboxItem(agent_id=manager.id, text=f"The subscription '{account.label}' {what}. What ran on it{who} now chooses a subscription automatically."))
        out.append(project)
    return out


async def delete(session: AsyncSession, account: Account) -> None:
    await release(session, account, gone=True)
    await session.execute(sql_update(Agent).where(Agent.account_id == account.id).values(account_id=None))
    await session.delete(account)
    await session.commit()


async def delete_owned(session: AsyncSession, user: User) -> None:
    """A person is deleted: so are their own subscriptions, and what ran on them chooses again."""
    for account in list(await session.scalars(select(Account).where(Account.owner_id == user.id))):
        await release(session, account, gone=True)
        await session.execute(sql_update(Agent).where(Agent.account_id == account.id).values(account_id=None))
        await session.delete(account)


async def check_pin(session: AsyncSession, account_id: int | None, provider: str, project_id: int | None = None) -> None:
    if account_id is None:
        return
    if (await get(session, account_id)).provider != provider:
        raise ServiceError("The account belongs to another provider")
    if project_id is None:
        return
    await permissions.check_account(session, await session.get(Project, project_id) if project_id else None, account_id)


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
    allowed = await permissions.account_ids(session, await session.get(Project, agent.project_id))
    if allowed is None:
        return ""
    accounts = [a for a in await list_accounts(session, provider) if a.enabled and a.owner_id is None]  # people's own ones are theirs
    if not accounts or any(a.id in allowed for a in accounts):
        return ""
    return (
        f"This project may only use some of the installation's subscriptions, and none of them is for {agent.provider}. "
        "An admin can change that (Project settings, Team, or what the project's owner may use in Settings, Users), or the agent can be given a provider the project has a subscription for."
    )


USED_UP = 3


def _hours_left(windows: list[LimitWindow], now: float) -> float:
    """Hours until the account's longest window resets: what is left of it then is lost. A window
    without a reset time has not started, so all of it is ahead. At least one hour."""
    ends = [w.resets_at if w.resets_at else now + (5 if "five_hour" in w.name else WEEK_HOURS) * HOUR for w in windows]
    return max(1.0, (max(ends, default=now + WEEK_HOURS * HOUR) - now) / HOUR)


def _rank(account: Account, windows: list[LimitWindow], size: float = 1.0, now: float | None = None) -> tuple[int, float]:
    """Lower is better: subscriptions with room, the rest of nearly full ones, API keys, used-up
    subscriptions; within each, the most usage left in absolute terms per hour until it is lost."""
    if not has_plan(account):
        return 2, 0.0
    utilization = limits_service.fullest(windows)
    room = max(0.0, 1 - utilization) * size
    return (0 if utilization < SWITCH_AT else 1 if utilization < 1 else USED_UP), -room / _hours_left(windows, time.time() if now is None else now)


def used_up_now(account: Account, windows: dict[str, list[LimitWindow]]) -> bool:
    return _rank(account, windows.get(account.key, []))[0] == USED_UP


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


async def _candidates(session: AsyncSession, provider: str, agent: Agent | None, model: str | None = None) -> list[Account]:
    """The accounts the agent's turn may run on: the one it or its project chose, else all it may use."""
    accounts = [a for a in await list_accounts(session, provider) if a.enabled]
    project = await session.get(Project, agent.project_id) if agent else None
    if (allowed := await permissions.account_ids(session, project)) is not None:  # a project limited to some accounts never gets another one
        accounts = [a for a in accounts if a.id in allowed]
    if not accounts:
        return []
    # Endpoints of one provider differ in what they offer (two model servers, say): one that names the model.
    model = model or (agent.model if agent else None)
    accounts = [a for a in accounts if model and model in (a.models or [])] or accounts
    # The agent's own subscription, else its project's. One of another provider does not count.
    wanted = agent.account_id if agent else None
    if wanted is None and project is not None:
        wanted = project.account_id
    if wanted and (pinned := next((a for a in accounts if a.id == wanted), None)):
        return [pinned]
    return accounts


async def used_up_until(session: AsyncSession, agent: Agent) -> int | None:
    """When the first of the subscriptions the agent's turns may run on is free again (unix
    seconds), if every one of them is used up; None if one has room, or a reset time is unknown."""
    accounts = await _candidates(session, agent.provider, agent)
    found = await limits_service.windows(session)
    ends = []
    for account in accounts:
        if not used_up_now(account, found):
            return None
        resets = [w.resets_at for w in found.get(account.key, []) if w.status == "rejected" or w.utilization >= 1]
        if not resets or not all(resets):
            return None
        ends.append(max(resets))  # type: ignore[type-var]
    return min(ends) if ends else None


async def pick(
    session: AsyncSession, provider: str, agent: Agent | None = None, conv: Conversation | None = None, model: str | None = None, fresh: bool = False
) -> Account | None:
    """The account for an agent's next turn; None if the provider has no (enabled) account.
    ``model``: what the turn runs on, the agent's own model if not given. ``fresh``: the turn starts
    a new session (a new piece of work, a compressed context), so there is no cache to keep."""
    accounts = await _candidates(session, provider, agent, model)
    if len(accounts) <= 1:
        return accounts[0] if accounts else None
    found = await limits_service.windows(session)
    sizes = plan_sizes(accounts, await limits_service.plan_sizes(session))
    now = time.time()

    def rank(account: Account) -> tuple[int, float]:
        return _rank(account, found.get(account.key, []), sizes.get(account.id, 1.0), now)

    best = min(accounts, key=rank)
    last = next((a for a in accounts if conv and a.key == conv.account_key), None)
    if last is None or rank(last)[0] != rank(best)[0] or rank(best)[0] == USED_UP:
        return best
    # Moving costs the prompt cache: a conversation stays unless its account is (nearly) used up or the cache is cold anyway.
    if not fresh and await _cache_warm(session, agent):
        return last
    # Without a cache to keep it moves only for clearly more: small differences come and go with every report.
    return best if -rank(best)[1] > -rank(last)[1] * MARGIN else last


async def other_with_room(session: AsyncSession, provider: str, agent: Agent, conv: Conversation | None, used_up: str) -> Account | None:
    """Another account for a turn the account ``used_up`` (by key) was turned away on; None if
    there is none that is not used up as well."""
    account = await pick(session, provider, agent, conv)
    if account is None or account.key == used_up or used_up_now(account, await limits_service.windows(session)):
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
    if account.provider == "antigravity":
        return {"antigravity_auth_mode": account.auth_mode, "antigravity_account": account.key, "gemini_api_key": value}
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
