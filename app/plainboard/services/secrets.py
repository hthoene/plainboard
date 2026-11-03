"""Secrets for agents: environment variables in their runs, jobs and setup script (e.g. CLI tokens).

Global secrets apply to every project; a project secret with the same name replaces the global one.
Values are encrypted at rest and never leave the app except towards the runner.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Secret
from ..schemas import SecretCreate, SecretOut, SecretUpdate
from ..security import decrypt, encrypt
from .errors import NotFound, ServiceError

# Variables the platform sets itself: a secret must not replace them.
RESERVED = {"PATH", "HOME", "USER", "SHELL", "PWD", "LANG", "TERM", "GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY"}
RESERVED_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "GIT_", "PLAINBOARD_")
MIN_REDACT_LENGTH = 6  # shorter values would hide ordinary text


def _check_name(name: str) -> None:
    if name in RESERVED or name.startswith(RESERVED_PREFIXES):
        raise ServiceError(f"{name} is set by PlainBoard and can't be a secret")


async def list_secrets(session: AsyncSession, project_id: int | None) -> list[Secret]:
    """The secrets of exactly this scope (None = global)."""
    scope = Secret.project_id.is_(None) if project_id is None else Secret.project_id == project_id
    return list((await session.scalars(select(Secret).where(scope).order_by(Secret.name))).all())


async def get(session: AsyncSession, secret_id: int) -> Secret:
    secret = await session.get(Secret, secret_id)
    if secret is None:
        raise NotFound("Secret not found")
    return secret


async def create(session: AsyncSession, project_id: int | None, data: SecretCreate) -> Secret:
    _check_name(data.name)
    if any(s.name == data.name for s in await list_secrets(session, project_id)):
        raise ServiceError(f"Secret {data.name} exists already", 409)
    secret = Secret(project_id=project_id, name=data.name, value_enc=encrypt(data.value), description=data.description)
    session.add(secret)
    await session.commit()
    _publish(secret)
    return secret


async def update(session: AsyncSession, secret: Secret, data: SecretUpdate) -> Secret:
    if data.value:
        secret.value_enc = encrypt(data.value)
    if data.description is not None:
        secret.description = data.description
    await session.commit()
    _publish(secret)
    return secret


async def delete(session: AsyncSession, secret: Secret) -> None:
    await session.delete(secret)
    await session.commit()
    _publish(secret)


def _publish(secret: Secret) -> None:
    if secret.project_id is not None:
        bus.publish(secret.project_id, "secret.changed", {"id": secret.id})


def to_out(secret: Secret) -> SecretOut:
    return SecretOut.model_validate(secret)


async def effective(session: AsyncSession, project_id: int) -> list[Secret]:
    """Global and project secrets for a project; the project wins on equal names."""
    rows = (await session.scalars(select(Secret).where(or_(Secret.project_id.is_(None), Secret.project_id == project_id)))).all()
    by_name: dict[str, Secret] = {}
    for secret in sorted(rows, key=lambda s: s.project_id is not None):
        by_name[secret.name] = secret
    return sorted(by_name.values(), key=lambda s: s.name)


def env(secrets: list[Secret]) -> dict[str, str]:
    return {s.name: value for s in secrets if (value := decrypt(s.value_enc))}


def redactor(values: dict[str, str]) -> Callable[[str], str]:
    """Replaces secret values in text (tool output, messages) with their name."""
    pairs = sorted(((v, k) for k, v in values.items() if len(v) >= MIN_REDACT_LENGTH), key=lambda p: -len(p[0]))
    if not pairs:
        return lambda text: text
    pattern = re.compile("|".join(re.escape(v) for v, _ in pairs))
    names = dict(pairs)
    return lambda text: pattern.sub(lambda m: f"[secret {names[m.group(0)]}]", text) if text else text
