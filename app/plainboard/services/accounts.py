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

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from plainboard_protocol import LimitWindow, ModelInfo, ProviderInfo, SignInAck, SignInFiles, files_digest
from sqlalchemy import func, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, Agent, Conversation, InboxItem, Project, Run, Runner, User, utcnow
from ..schemas import AccountCreate, AccountOut, AccountUpdate, ProjectAccountOut
from ..security import decrypt, encrypt
from . import limits as limits_service
from . import permissions
from . import users as user_service
from .errors import NotFound, ServiceError

AUTH_MODES = {"claude_code": ("oauth_token", "api_key", "runner_login"), "codex": ("chatgpt", "api_key")}
# Every other provider is an endpoint (ProviderInfo.endpoint): which ones exist is the runner's list, not the app's.
ENDPOINT_MODES = ("api_key",)
# A sign-in in the browser (Claude's runner_login, Codex's ChatGPT sign-in). It is never in the runner's home,
# which every agent there reads, and so works on every runner: its files are kept here, encrypted like a token,
# and go with each run of it into a private folder (runner signins.py), from which refreshed ones come back
# (``take_sign_ins``). The installation's from before (#84) are copied over once from the built-in runner's home
# (``Account.home_login``, ``take_over``); until then they work on the built-in runner only, as before.
LOGIN_MODES = ("runner_login", "chatgpt")
LABELS = {"claude_code": "Claude", "codex": "Codex"}
ENV_SECRETS = {("claude_code", "oauth_token"): "CLAUDE_CODE_OAUTH_TOKEN", ("claude_code", "api_key"): "ANTHROPIC_API_KEY", ("codex", "api_key"): "OPENAI_API_KEY"}
SWITCH_AT = 0.95  # an account this full hands new turns to one with more room; its rest is used last
# The prompt cache lives an hour (Claude Code's ENABLE_PROMPT_CACHING_1H): a conversation that had no
# turn for longer loses nothing by moving to the account with the most room.
CACHE_SECONDS = 3600
# A conversation whose cache is cold, or a new session, moves to another account only if that one
# is worth this much more: small differences come and go with every report.
MARGIN = 1.25
HOUR = 3600
WEEK_HOURS = 7 * 24


log = logging.getLogger(__name__)


def _check_mode(provider: str, mode: str) -> None:
    if mode not in AUTH_MODES.get(provider, ENDPOINT_MODES):
        raise ServiceError(f"Unknown sign-in for {provider}: {mode}")


def kept_login(account: Account) -> bool:
    """A browser sign-in kept here, not in the runner's home: a person's own, or the installation's."""
    return account.auth_mode in LOGIN_MODES and not account.home_login


@dataclass(frozen=True)
class Where:
    """The runner a turn, a summary or a question goes to, as far as the choice of a subscription goes."""

    runner_id: int | None  # None: the built-in one
    signins: bool  # it takes kept sign-ins (Credentials.login_files; a runner from before them does not)


def unusable(account: Account, where: Where | None) -> str:
    """Why ``account`` has no working sign-in on that runner: "not_signed_in", "home_only" (still in the built-in
    runner's home), "old_runner"; "" if it has, or nothing is known against it. ``where`` None: anywhere."""
    if account.auth_mode not in LOGIN_MODES:
        return ""
    if account.home_login:
        return "home_only" if where is not None and where.runner_id is not None else ""
    if not account.secret_enc:
        return "not_signed_in"
    return "old_runner" if where is not None and not where.signins else ""


def login_files(account: Account) -> dict[str, str]:
    """The files of a kept sign-in; none while it is not signed in."""
    try:
        files = json.loads(decrypt(account.secret_enc) or "{}")
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in files.items()} if isinstance(files, dict) else {}


def _models(names: list[str]) -> list[str]:
    """As typed: without blanks, each once, in the order given."""
    return list(dict.fromkeys(name.strip() for name in names if name.strip()))


# Providers whose subscriptions each list the models they may use (LimitsEvent.models): kept in Account.models,
# where an endpoint's account has the models its owner named.
LISTED = {"codex"}
MODELS_CHECK_SECONDS = 3600  # how often such a list is asked for again, also while the subscription is in use


def record_models(account: Account, models: list[ModelInfo] | None) -> bool:
    """Keep what a read of the limits found the subscription may use. Returns whether that changed."""
    if models is None or account.provider not in LISTED:
        return False
    ids = list(dict.fromkeys(m.id for m in models))
    if ids == (account.models or []):
        return False
    account.models = ids
    return True


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
            "project_ids": account.project_ids or [],
        }
    )
    if not manage:
        out = out.model_copy(update={"secret_set": False, "secret_from_env": False, "endpoint_url": "", "shared_with": [], "project_ids": [], "share": "private" if account.owner_id else out.share})
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
    _check_mode(data.provider, data.auth_mode)
    if data.auth_mode in LOGIN_MODES and data.secret:
        raise ServiceError("This subscription is signed in in the browser, not with a token.")
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
        _check_mode(account.provider, data.auth_mode)
        account.auth_mode = data.auth_mode
        account.home_login = False  # a sign-in of the new kind is made anew, and kept here
        if account.provider in LISTED:
            account.models = []  # another sign-in may offer other models: asked anew (record_models)
        if data.secret is None:
            account.secret_enc = None  # a token is no API key
    if data.secret and kept_login(account):
        raise ServiceError("This subscription is signed in in the browser, not with a token.")
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


async def release(session: AsyncSession, account: Account, gone: bool = False, why: str = "") -> list[Project]:
    """Projects and agents that chose the account but may no longer use it (it is deleted, or shared with
    fewer people) choose automatically again; each such project's manager is told once (``why``: what
    happened to it). Not committed."""
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
            what = "was deleted" if gone else why or "is no longer shared with this project's owner"
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
        keep = list(set(project.account_ids) | await permissions.contributed(session, project))  # what members gave it stays
        if project.account_id not in keep:
            project.account_id = None
        await session.execute(sql_update(Agent).where(Agent.project_id == project.id, Agent.account_id.is_not(None), Agent.account_id.not_in(keep)).values(account_id=None))
    await session.commit()
    return project


# --- what members give a project themselves (#93, permissions.contributed) ----------------------


async def given(session: AsyncSession, project: Project, user: User | None) -> list[ProjectAccountOut]:
    """The subscriptions people gave the project, and the viewer's own ones they could give it. No secret."""
    accounts = {a.id: a for a in await permissions.given(session, project)}
    if user is not None:
        accounts |= {a.id: a for a in await session.scalars(select(Account).where(Account.owner_id == user.id))}
    used = await permissions.account_ids(session, project) or set()
    names = await owner_names(session)
    blocked = set(project.blocked_account_ids or [])
    return [
        ProjectAccountOut(
            id=a.id,
            provider=a.provider,
            label=a.label,
            enabled=a.enabled,
            owner_id=a.owner_id,
            owner_name=names.get(a.owner_id or 0, ""),
            mine=user is not None and a.owner_id == user.id,
            given=project.id in (a.project_ids or []),
            blocked=a.id in blocked,
            used=a.id in used,
        )
        for a in sorted(accounts.values(), key=lambda a: (a.provider, a.id))
    ]


async def give(session: AsyncSession, project: Project, account: Account, user: User | None, on: bool) -> None:
    """A member gives the project their own subscription, or takes it back. Only one's own: the installation's
    and other people's are its owner's and an admin's call (``limit_project``, ``block``)."""
    if user is None or account.owner_id != user.id:
        raise ServiceError("Only your own subscriptions can be given to a project here.", 403)
    ids = [i for i in account.project_ids or [] if i != project.id]
    account.project_ids = [*ids, project.id] if on else ids
    if not on:
        await session.flush()
        await release(session, account, why="was taken back by the person it belongs to")
    await session.commit()


async def block(session: AsyncSession, project: Project, account: Account, blocked: bool) -> None:
    """The project's owner switches a subscription a member gave it off for it, or on again. Its secrets stay hidden."""
    if project.id not in (account.project_ids or []):
        raise ServiceError("Only a subscription a member gave this project can be switched off here.")
    ids = [i for i in project.blocked_account_ids or [] if i != account.id]
    project.blocked_account_ids = [*ids, account.id] if blocked else ids
    if blocked:
        await session.flush()
        await release(session, account, why="was switched off for this project")
    await session.commit()


async def leave(session: AsyncSession, project_id: int, user_id: int) -> None:
    """A person no longer works in the project (removed, or only watching now): what they gave it goes with them."""
    for account in list(await session.scalars(select(Account).where(Account.owner_id == user_id))):
        if project_id in (account.project_ids or []):
            account.project_ids = [i for i in account.project_ids or [] if i != project_id]
            await session.flush()
            await release(session, account, why="left with the person it belongs to")
    await session.commit()


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


async def _candidates(session: AsyncSession, provider: str, agent: Agent | None, model: str | None = None, where: Where | None = None) -> list[Account]:
    """The accounts the agent's turn may run on: the one it or its project chose, else all it may use. ``where``:
    only those with a working sign-in on that runner; a chosen one without counts as not chosen."""
    accounts = [a for a in await list_accounts(session, provider) if a.enabled and (where is None or not unusable(a, where))]
    project = await session.get(Project, agent.project_id) if agent else None
    if (allowed := await permissions.account_ids(session, project)) is not None:  # a project limited to some accounts never gets another one
        accounts = [a for a in accounts if a.id in allowed]
    if not accounts:
        return []
    # Endpoints of one provider differ in what they offer (two model servers, say): one that names the model.
    # A subscription whose list comes from the provider (LISTED) and is not known yet may offer it as well.
    model = model or (agent.model if agent else None)
    accounts = [a for a in accounts if model and (model in (a.models or []) or (a.provider in LISTED and not a.models))] or accounts
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
    session: AsyncSession,
    provider: str,
    agent: Agent | None = None,
    conv: Conversation | None = None,
    model: str | None = None,
    fresh: bool = False,
    where: Where | None = None,
) -> Account | None:
    """The account for an agent's next turn; None if the provider has no (enabled) account.
    ``model``: what the turn runs on, the agent's own model if not given. ``fresh``: the turn starts
    a new session (a new piece of work, a compressed context), so there is no cache to keep. ``where``:
    the runner it goes to; an account without a working sign-in there is never picked (``not_signed_in``)."""
    accounts = await _candidates(session, provider, agent, model, where)
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


async def not_signed_in(session: AsyncSession, provider: str, agent: Agent, where: Where) -> str:
    """Why no account was picked, if the reason is the runner: the agent may use accounts of the provider, but
    none of them has a working sign-in there. Else ""."""
    accounts = [a for a in await _candidates(session, provider, agent) if unusable(a, where)]
    if not accounts or await _candidates(session, provider, agent, where=where):
        return ""
    name = "the built-in runner"
    if where.runner_id is not None:
        runner = await session.get(Runner, where.runner_id)
        name = f"runner {runner.name}" if runner else "its runner"
    label = LABELS.get(provider, provider)
    why = []
    for account in accounts:
        reason = unusable(account, where)
        if reason == "not_signed_in":
            why.append(f"'{account.label}' is not signed in (Settings, Subscriptions: sign in)")
        elif reason == "home_only":
            why.append(f"'{account.label}' is signed in on the built-in runner only, until PlainBoard has taken that sign-in over")
        else:
            why.append(f"'{account.label}' needs a newer version of {name}, which it gets with its update")
    return f"{label} is not signed in on {name}: " + "; ".join(why) + ". No turn was started without a sign-in."


async def other_with_room(session: AsyncSession, provider: str, agent: Agent, conv: Conversation | None, used_up: str, where: Where | None = None) -> Account | None:
    """Another account for a turn the account ``used_up`` (by key) was turned away on; None if
    there is none that is not used up as well."""
    account = await pick(session, provider, agent, conv, where=where)
    if account is None or account.key == used_up or used_up_now(account, await limits_service.windows(session)):
        return None
    return account


def fields(account: Account | None) -> dict[str, Any]:
    """The account's part of the run's ``Credentials``."""
    if account is None:
        return {}
    if kept_login(account):  # its files only, never a token field: a runner that does not know "own_login" refuses the run
        own = {"claude_auth_mode" if account.provider == "claude_code" else "codex_auth_mode": "own_login", "login_account": account.key, "login_files": login_files(account)}
        own |= {"claude_oauth_token": None, "anthropic_api_key": None, "openai_api_key": None}
        return own | ({"codex_account": account.key} if account.provider == "codex" else {})
    value = secret(account)
    if account.provider == "claude_code":  # login_files: none of another account's, after a switch (run_manager._switch_account)
        return {"claude_auth_mode": account.auth_mode, "claude_oauth_token" if account.auth_mode == "oauth_token" else "anthropic_api_key": value, "login_files": None}
    if account.provider == "codex":
        return {"codex_auth_mode": account.auth_mode, "codex_account": account.key, "openai_api_key": value, "login_files": None}
    return {"endpoint_url": account.endpoint_url, "endpoint_key": value, "endpoint_context": account.context_tokens}


async def catalog(session: AsyncSession, providers: list[ProviderInfo]) -> list[ProviderInfo]:
    """The providers agents can run on, from the runner's list. A provider that is an endpoint counts
    once an account of it offers a model: what its accounts name, else its own usual models."""
    accounts = [a for a in await list_accounts(session) if a.enabled]
    out = []
    for provider in providers:
        if provider.endpoint is None:  # with what any subscription's own list adds (LISTED)
            known = {m.id for m in provider.models}
            added = list(dict.fromkeys(name for a in accounts if a.provider == provider.name for name in a.models or [] if name not in known))
            out.append(provider.model_copy(update={"models": [*provider.models, *(ModelInfo(id=name, label=name) for name in added)]}) if added else provider)
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


async def kept_logins(session: AsyncSession) -> list[Account]:
    """The accounts whose browser sign-in is kept here."""
    query = select(Account).where(Account.auth_mode.in_(LOGIN_MODES), Account.home_login.is_(False)).order_by(Account.id)
    return list((await session.scalars(query)).all())


async def any_kept_login(session: AsyncSession) -> bool:
    return await session.scalar(select(Account.id).where(Account.auth_mode.in_(LOGIN_MODES), Account.home_login.is_(False)).limit(1)) is not None


async def any_home_login(session: AsyncSession) -> bool:
    return await session.scalar(select(Account.id).where(Account.home_login.is_(True)).limit(1)) is not None


async def take_over(session: AsyncSession, runner: Any) -> int:
    """Copy the installation's sign-ins from the built-in runner's home (``runner``), from before #84: from then on
    they are kept here and work on every runner. The runner leaves its files where they are and uses them no
    more (runner signins.py), so only this copy is refreshed: two copies that are refreshed each on their own
    would outdate one another. One in use waits for the next look. Claude's home has one sign-in for all its
    runner_login accounts: the first of them gets it, the others are signed out. Returns how many were taken."""
    accounts = list(await session.scalars(select(Account).where(Account.home_login.is_(True)).order_by(Account.id)))
    taken, claude = 0, ""  # Claude's home sign-in: "given" to an account, "waiting" for the next look
    for account in accounts:
        if account.auth_mode not in LOGIN_MODES:
            account.home_login = False
            continue
        if account.provider == "claude_code" and claude:
            continue
        try:
            got = await runner.take_over(account.provider, account.key)
        except Exception as exc:  # in use, away, or a runner from before this
            log.info("The sign-in of account %s is taken over later: %s", account.key, str(exc)[:200])
            claude = "waiting" if account.provider == "claude_code" else claude
            continue
        account.home_login, account.secret_enc = False, encrypt(json.dumps(got.files)) if got.files else None
        if account.provider == "claude_code":
            claude = "given"
            for other in accounts:  # one sign-in, one account: never two copies of it; in the same commit
                if other is not account and other.provider == "claude_code" and other.home_login:
                    other.home_login, other.secret_enc = False, None
        taken += bool(got.files)
        await session.commit()  # each at once: the runner no longer uses the home's copy
        log.info("Account %s: the sign-in from the built-in runner's home is kept by PlainBoard now (%s)", account.key, "signed in" if got.files else "not signed in there")
    await session.commit()
    return taken


async def take_sign_ins(session: AsyncSession, runner: Any) -> list[SignInFiles]:
    """Keep what a runner holds of kept sign-ins: a finished sign-in, or files a run refreshed. One derived from an
    older version than the one kept here is dropped: the newest wins. Returns what was kept, each with the digest
    of the version it replaced as ``base`` (for the other runners, ``RunManager.keep_sign_ins``)."""
    try:
        waiting = await runner.sign_ins()
    except Exception as exc:  # away, or a runner from before own sign-ins
        log.debug("No sign-ins from the runner: %s", exc)
        return []
    if not waiting:
        return []
    kept: list[SignInFiles] = []
    acks = []
    for entry in waiting:
        account = await session.scalar(select(Account).where(Account.key == entry.account))
        if account is not None and kept_login(account) and account.provider == entry.provider and entry.files:
            before = files_digest(login_files(account) or None)
            if not entry.base or entry.base == before:
                account.secret_enc = encrypt(json.dumps(entry.files))
                kept.append(SignInFiles(provider=entry.provider, account=entry.account, files=entry.files, base=before, digest=files_digest(entry.files)))
        acks.append(SignInAck(account=entry.account, digest=entry.digest))
    await session.commit()
    try:
        await runner.ack_sign_ins(acks)
    except Exception as exc:
        log.info("Sign-ins kept, the runner did not take the acknowledgement: %s", exc)
    return kept
