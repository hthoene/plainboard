"""Per-agent inbox: updates are collected and prepended to the agent's next turn."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import InboxItem, utcnow


async def push(session: AsyncSession, agent_id: int, text: str) -> None:
    session.add(InboxItem(agent_id=agent_id, text=text))
    await session.commit()


async def consume(session: AsyncSession, agent_id: int) -> list[str]:
    items = (
        await session.scalars(select(InboxItem).where(InboxItem.agent_id == agent_id, InboxItem.consumed_at.is_(None)).order_by(InboxItem.id))
    ).all()
    now = utcnow()
    for item in items:
        item.consumed_at = now
    await session.commit()
    return [item.text for item in items]


async def pending_count(session: AsyncSession, agent_id: int) -> int:
    items = await session.scalars(select(InboxItem.id).where(InboxItem.agent_id == agent_id, InboxItem.consumed_at.is_(None)))
    return len(items.all())
