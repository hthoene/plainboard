"""Global user settings stored in the DB (secrets encrypted), with environment fallbacks."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from plainboard_protocol import Credentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Setting
from ..security import decrypt, encrypt


@dataclass(frozen=True)
class SettingDef:
    default: Any
    secret: bool = False
    env: str | None = None  # environment fallback
    managed: bool = False  # set by its own endpoint, not through the settings form


SETTINGS: dict[str, SettingDef] = {
    "github_token": SettingDef(None, secret=True, env="GITHUB_TOKEN"),
    # One SSH key for every repository with an SSH address that has no key of its own.
    "git_ssh_key": SettingDef(None, secret=True, managed=True),
    "git_ssh_public_key": SettingDef("", managed=True),
    "git_author_name": SettingDef("PlainBoard Agent"),
    "git_author_email": SettingDef("agent@plainboard.local"),
    "default_provider": SettingDef("claude_code"),
    "default_manager_model": SettingDef("claude-sonnet-5-5"),
    "default_worker_model": SettingDef("claude-sonnet-5-5"),
    "default_summary_model": SettingDef("claude-haiku-5-5"),
    # How long a model thinks before it acts, by what it does. The manager has many short turns; a
    # member's turn is the work itself, and a summary is what an agent knows afterwards.
    "default_manager_effort": SettingDef("medium"),
    "default_worker_effort": SettingDef("high"),
    "default_summary_effort": SettingDef("high"),
    "default_context_limit": SettingDef(350000),
    "default_keep_recent": SettingDef(6),
    # Members working at the same time in each project that has no number of its own (`projects.parallel_limit`).
    "max_parallel_runs": SettingDef(3),
    # Up to how many parallel agents a manager may set for its project (0: managers can't change it).
    "manager_parallel_limit": SettingDef(6),
    "timezone": SettingDef("Europe/Berlin"),
    "update_check": SettingDef(True),  # ask now and then whether a newer PlainBoard exists
    # The admin has gone through the basic settings once (the set-up in the browser). Until then the UI asks for them.
    "setup_done": SettingDef(False),
    # Automatic cleanup when the workspaces' volume runs short (services/cleanup.py): below the soft
    # share free what costs nothing to rebuild, below the hard one also dependencies, and the manager hears it.
    "cleanup": SettingDef(True),
    "cleanup_soft_free_percent": SettingDef(15),
    "cleanup_hard_free_percent": SettingDef(5),
    "cleanup_memory_free_percent": SettingDef(10),  # below: servers nobody opened for long are stopped
    "cleanup_last": SettingDef(None, managed=True),
    "cleanup_told_at": SettingDef(None, managed=True),
    "cleanup_servers_last": SettingDef(None, managed=True),  # servers the memory cleanup stopped last
    # Everything is paused (services/pause.py): who did it, since when, and which members that switched off.
    "pause": SettingDef(None, managed=True),
}


async def get_all(session: AsyncSession) -> dict[str, Any]:
    """Effective values (DB > env > default). Secrets are decrypted."""
    rows = {r.key: r for r in (await session.scalars(select(Setting))).all()}
    values: dict[str, Any] = {}
    for key, definition in SETTINGS.items():
        row = rows.get(key)
        value: Any = None
        if row is not None and row.value is not None:
            value = decrypt(row.value) if definition.secret else json.loads(row.value)
        if value in (None, "") and definition.env:
            value = os.environ.get(definition.env) or None
        values[key] = definition.default if value is None else value
    return values


async def get(session: AsyncSession, key: str) -> Any:
    return (await get_all(session))[key]


async def update(session: AsyncSession, values: dict[str, Any], *, managed: bool = False) -> None:
    for key, value in values.items():
        definition = SETTINGS.get(key)
        if definition is None:
            raise ValueError(f"Unknown setting: {key}")
        if definition.managed and not managed:
            continue  # the form sends back what it was given; these have their own endpoint
        row = await session.get(Setting, key) or Setting(key=key, secret=definition.secret)
        if definition.secret:
            row.value = encrypt(value) if value else None
        else:
            row.value = None if value is None else json.dumps(value)
        session.add(row)
    await session.commit()


async def set_ssh_key(session: AsyncSession, private_key: str | None) -> None:
    """Create the general SSH key pair (no key given), take the user's own key, or remove it ("")."""
    from .repositories import new_key_pair, read_private_key

    if private_key == "":
        private, public = None, ""
    elif private_key:
        private, public = read_private_key(private_key, "plainboard")
    else:
        private, public = new_key_pair("plainboard")
    await update(session, {"git_ssh_key": private, "git_ssh_public_key": public}, managed=True)


async def public_view(session: AsyncSession) -> dict[str, Any]:
    rows = {r.key: r for r in (await session.scalars(select(Setting))).all()}
    values = await get_all(session)
    out: dict[str, Any] = {}
    for key, definition in SETTINGS.items():
        if definition.secret:
            out[key] = bool(rows.get(key) and rows[key].value)
        else:
            out[key] = values[key]
    return out


def env_fallbacks() -> dict[str, bool]:
    return {key: bool(d.env and os.environ.get(d.env)) for key, d in SETTINGS.items() if d.secret}


def credentials(
    values: dict[str, Any], github_token_override: str | None = None, secrets: dict[str, str] | None = None, account: dict[str, Any] | None = None
) -> Credentials:
    """``account``: the provider account's part (``accounts.fields``); none for runs that need no model."""
    return Credentials(
        **(account or {}),
        github_token=github_token_override or values.get("github_token"),
        git_ssh_key=values.get("git_ssh_key"),
        git_author_name=values.get("git_author_name") or "PlainBoard Agent",
        git_author_email=values.get("git_author_email") or "agent@plainboard.local",
        secrets=secrets or {},
    )
