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


SETTINGS: dict[str, SettingDef] = {
    "claude_auth_mode": SettingDef(None),  # oauth_token | api_key | runner_login; None = auto
    "claude_oauth_token": SettingDef(None, secret=True, env="CLAUDE_CODE_OAUTH_TOKEN"),
    "anthropic_api_key": SettingDef(None, secret=True, env="ANTHROPIC_API_KEY"),
    "openai_api_key": SettingDef(None, secret=True, env="OPENAI_API_KEY"),
    "github_token": SettingDef(None, secret=True, env="GITHUB_TOKEN"),
    "git_author_name": SettingDef("PlainBoard Agent"),
    "git_author_email": SettingDef("agent@plainboard.local"),
    "default_provider": SettingDef("claude_code"),
    "default_manager_model": SettingDef("claude-sonnet-5-5"),
    "default_worker_model": SettingDef("claude-sonnet-5-5"),
    "default_summary_model": SettingDef("claude-haiku-5-5"),
    "default_effort": SettingDef("medium"),
    "default_context_limit": SettingDef(400000),
    "default_keep_recent": SettingDef(6),
    "max_parallel_runs": SettingDef(3),
    "timezone": SettingDef("Europe/Berlin"),
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


async def update(session: AsyncSession, values: dict[str, Any]) -> None:
    for key, value in values.items():
        definition = SETTINGS.get(key)
        if definition is None:
            raise ValueError(f"Unknown setting: {key}")
        row = await session.get(Setting, key) or Setting(key=key, secret=definition.secret)
        if definition.secret:
            row.value = encrypt(value) if value else None
        else:
            row.value = None if value is None else json.dumps(value)
        session.add(row)
    await session.commit()


async def public_view(session: AsyncSession) -> dict[str, Any]:
    rows = {r.key: r for r in (await session.scalars(select(Setting))).all()}
    values = await get_all(session)
    out: dict[str, Any] = {}
    for key, definition in SETTINGS.items():
        if definition.secret:
            out[key] = bool(rows.get(key) and rows[key].value)
        else:
            out[key] = values[key]
    out["claude_auth_mode_effective"] = effective_auth_mode(values)
    return out


def env_fallbacks() -> dict[str, bool]:
    return {key: bool(d.env and os.environ.get(d.env)) for key, d in SETTINGS.items() if d.secret}


def effective_auth_mode(values: dict[str, Any]) -> str:
    mode = values.get("claude_auth_mode")
    if mode:
        return mode
    if values.get("anthropic_api_key"):
        return "api_key"
    if values.get("claude_oauth_token"):
        return "oauth_token"
    return "runner_login"


def credentials(values: dict[str, Any], github_token_override: str | None = None) -> Credentials:
    return Credentials(
        claude_auth_mode=effective_auth_mode(values),
        claude_oauth_token=values.get("claude_oauth_token"),
        anthropic_api_key=values.get("anthropic_api_key"),
        openai_api_key=values.get("openai_api_key"),
        github_token=github_token_override or values.get("github_token"),
        git_author_name=values.get("git_author_name") or "PlainBoard Agent",
        git_author_email=values.get("git_author_email") or "agent@plainboard.local",
    )
