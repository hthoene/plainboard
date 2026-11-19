"""Signed-in browsers (``models.UserSession``): the login cookie names one, signing out deletes it."""

from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User, UserSession, utcnow
from ..security import SESSION_MAX_AGE, make_session_cookie, read_session_cookie


async def start(session: AsyncSession, user: User) -> str:
    """A new session for the user; returns the value of its login cookie."""
    now = utcnow()
    await session.execute(delete(UserSession).where(UserSession.expires_at <= now))
    row = UserSession(id=secrets.token_urlsafe(32), user_id=user.id, created_at=now, expires_at=now + timedelta(seconds=SESSION_MAX_AGE))
    session.add(row)
    await session.commit()
    return make_session_cookie(user.id, user.session_version, row.id)


async def user_of(session: AsyncSession, user_id: int, session_id: str, version: int | None = None) -> User | None:
    """The user of a session that is still open (and, given a version, not signed out everywhere since)."""
    row = await session.get(UserSession, session_id)
    if row is None or row.user_id != user_id or row.expires_at <= utcnow():
        return None
    user = await session.get(User, user_id)
    if user is None or not user.enabled or (version is not None and user.session_version != version):
        return None
    return user


async def end(session: AsyncSession, cookie: str | None) -> None:
    if signed := read_session_cookie(cookie):
        await session.execute(delete(UserSession).where(UserSession.id == signed[2]))
        await session.commit()
