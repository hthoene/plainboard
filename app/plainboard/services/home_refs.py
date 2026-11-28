"""Which agents work with which folders of the shared home: the paths their tool calls and jobs name (#192).

Agents leave work in the home (``~/scratch/<ticket>``, ``~/dt-jobs/...``) that nothing else ties to a project. Their
tool calls (the input only, never a result: a listing names everything; not the cleanup tools, which name
others' folders) and job commands of the last ``DAYS`` are read
once, then only what is new, and kept in memory: home path (one or two levels, ``scratch``, ``scratch/k483``) → agent →
when it was last named. Nothing is written.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Text, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Conversation, Job, Message, utcnow

DAYS = 30
BATCH = 2000
# ~/x, $HOME/x, ${HOME}/x, /home/<user>/x, /root/x, with the folder in it; not inside another path.
PATH = re.compile(r"(?<![\w.~/$-])(?:~|\$HOME|\$\{HOME\}|/home/[A-Za-z0-9_.-]+|/root)/([A-Za-z0-9_.+-]+)(?:/([A-Za-z0-9_.+-]+))?")
HINTS = ("%~/%", "%$HOME%", "%${HOME}%", "%/home/%", "%/root/%")


def named(text: str) -> set[str]:
    """The home paths (one and two levels below the home) a text names; hidden ones only in ``~/.cache``."""
    found: set[str] = set()
    for top, inner in PATH.findall(text):
        if top in (".", ".."):
            continue
        if top.startswith("."):
            if top == ".cache" and inner and not inner.startswith("."):
                found.add(f".cache/{inner}")
            continue
        found.add(top)
        if inner and inner not in (".", ".."):
            found.add(f"{top}/{inner}")
    return found


@dataclass
class Refs:
    message_id: int = 0
    job_id: int = 0
    seen: dict[str, dict[int, datetime]] = field(default_factory=dict)  # home path → agent id → last named
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def add(self, rels: set[str], agent_id: int, at: datetime) -> None:
        for rel in rels:
            agents = self.seen.setdefault(rel, {})
            if agents.get(agent_id) is None or agents[agent_id] < at:
                agents[agent_id] = at

    def agents(self, rel: str, now: datetime | None = None) -> set[int]:
        since = (now or utcnow()) - timedelta(days=DAYS)
        return {a for a, at in self.seen.get(rel, {}).items() if at >= since}


_refs = Refs()


def _named_rows(rows: list[Any]) -> list[tuple[set[str], int, datetime]]:
    """(paths named, agent, when) of tool calls (their input) or jobs (their command); in a thread, it is regex work."""
    out = []
    for _, value, agent_id, at in rows:
        text = value.get("input") if isinstance(value, dict) else value
        if isinstance(text, str) and text and (rels := named(text)):
            out.append((rels, agent_id, at))
    return out


async def scan(session: AsyncSession, refs: Refs | None = None) -> Refs:
    """Read what is new since the last scan (the first time: the last ``DAYS``)."""
    refs = refs or _refs
    async with refs.lock:
        since = utcnow() - timedelta(days=DAYS)
        hint = or_(*(cast(Message.meta, Text).like(h) for h in HINTS))
        while True:
            rows = (
                await session.execute(
                    select(Message.id, Message.meta, Conversation.agent_id, Message.created_at)
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .where(Message.id > refs.message_id, Message.kind == "tool", Message.created_at >= since, ~Message.content.like("%_cleanup"), hint)
                    .order_by(Message.id)
                    .limit(BATCH)
                )
            ).all()
            if not rows:
                break
            for rels, agent_id, at in await asyncio.to_thread(_named_rows, rows):
                refs.add(rels, agent_id, at)
            refs.message_id = rows[-1][0]
            if len(rows) < BATCH:
                break
        while True:
            jobs = (
                await session.execute(
                    select(Job.id, Job.command, Job.agent_id, Job.created_at).where(Job.id > refs.job_id, Job.created_at >= since).order_by(Job.id).limit(BATCH)
                )
            ).all()
            for rels, agent_id, at in _named_rows(jobs):
                refs.add(rels, agent_id, at)
            if jobs:
                refs.job_id = jobs[-1][0]
            if len(jobs) < BATCH:
                break
    return refs
