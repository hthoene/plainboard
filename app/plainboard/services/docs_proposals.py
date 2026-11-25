"""Larger restructurings of a doc site (#156): its doc agent proposes, the user decides.

Without a model, the docs watcher (orchestration/docs_watcher.py) asks a site's doc agent to check its structure once
the site grew (``due``: ``FIRST_PAGES`` pages the first time, later a quarter and ``GROWTH_MIN`` pages more or ``EDITS``
page edits since the last check, at most every ``EVERY``). Mostly it fits and nothing happens. A proposal
(propose_doc_restructure) becomes a ticket with a decision for the user: restructure, or keep it as it is. Approved,
PlainBoard orders the doc agent to do it (RunManager.decision_answered); declined, the same outline or the same pages
are refused when proposed again. The site's switch ``restructure_check`` turns it all off.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Agent, Assignment, Decision, DecisionStatus, DocPage, DocPageVersion, DocProposal, DocSite, Task
from ..schemas import DecisionOption, TaskCreate
from . import board as board_service
from . import decisions as decision_service
from .errors import ServiceError
from .util import truncate

FIRST_PAGES = 15  # a site is first checked at this many pages
GROWTH = 0.25  # later, once it has a quarter more pages than at the last check ...
GROWTH_MIN = 5  # ... and at least this many more,
EDITS = 40  # or once its pages were edited this often since then
EVERY = timedelta(days=7)  # at most this often
DECLINED_NAMED = 5
APPROVE, KEEP = 0, 1
OPEN, APPROVED, DECLINED, ANSWERED = "open", "approved", "declined", "answered"  # answered: in the user's own words


def fingerprint(outline: str) -> str:
    """The outline without its form (case, bullets, numbering, spacing): the same proposal in other dress."""
    lines = (re.sub(r"^[\s\-*+#>|`]*(\d+[.)]\s*)*", "", line).strip().lower() for line in outline.splitlines())
    return hashlib.sha256("\n".join(" ".join(line.split()) for line in lines if line).encode()).hexdigest()[:32]


async def pages_of(session: AsyncSession, site_id: int) -> int:
    """Its pages in the working version."""
    return int(await session.scalar(select(func.count()).select_from(DocPage).where(DocPage.site_id == site_id, DocPage.until_seq.is_(None))) or 0)


async def edits_since(session: AsyncSession, site_id: int, since: datetime) -> int:
    q = select(func.count()).select_from(DocPageVersion).join(DocPage, DocPage.id == DocPageVersion.page_id).where(DocPage.site_id == site_id, DocPageVersion.created_at > since)
    return int(await session.scalar(q) or 0)


async def waiting(session: AsyncSession, site_id: int) -> DocProposal | None:
    """Its proposal the user has not decided on yet (a withdrawn question counts no more)."""
    q = (
        select(DocProposal)
        .join(Decision, Decision.id == DocProposal.decision_id)
        .where(DocProposal.site_id == site_id, DocProposal.status == OPEN, Decision.status == DecisionStatus.OPEN)
        .limit(1)
    )
    return await session.scalar(q)


async def declined(session: AsyncSession, site_id: int) -> list[DocProposal]:
    return list(await session.scalars(select(DocProposal).where(DocProposal.site_id == site_id, DocProposal.status == DECLINED).order_by(DocProposal.id.desc())))


async def due(session: AsyncSession, site: DocSite, now: datetime) -> tuple[int, str] | None:
    """Whether its doc agent should check the structure now: (pages, why), else None."""
    if not site.restructure_check or (site.structure_checked_at and now - site.structure_checked_at < EVERY):
        return None
    if await waiting(session, site.id) is not None:
        return None
    pages = await pages_of(session, site.id)
    if site.structure_checked_at is None:
        return (pages, f"it has {pages} pages") if pages >= FIRST_PAGES else None
    edits = await edits_since(session, site.id, site.structure_checked_at)
    grown = pages >= max(site.structure_pages + GROWTH_MIN, math.ceil(site.structure_pages * (1 + GROWTH)))
    if not grown and edits < EDITS:
        return None
    return pages, f"it has {pages} pages ({site.structure_pages} at the last check on {site.structure_checked_at:%Y-%m-%d}) and {edits} page edits since"


def check_text(why: str, refused: list[DocProposal]) -> str:
    text = (
        f"Your doc site grew since its structure was last looked at: {why}. Check once whether a larger restructuring would "
        "clearly help its readers: a new outline, merging or splitting pages, new sections, or for a large site areas and groups "
        "of its top-level pages (doc_outline). Look at the tree (list_docs, level by "
        "level) and at contents only where needed, not every page. Most of the time it fits: then change nothing and say so in "
        "one line. Only when the benefit is clear and substantial, propose it with propose_doc_restructure (reason, outline now "
        "and proposed, the pages concerned). Never restructure yourself: the user decides, and PlainBoard orders it when approved."
    )
    if refused:
        text += "\nThe user declined these, don't propose them again:\n" + "\n".join(f"- {p.title}: {truncate(' '.join(p.after.split()), 200)}" for p in refused[:DECLINED_NAMED])
    return text


async def propose(
    session: AsyncSession, site: DocSite, agent: Agent, *, title: str, reason: str, before: str, after: str, pages: list[int], labels: tuple[str, str]
) -> tuple[DocProposal, Task]:
    if (open_ := await waiting(session, site.id)) is not None:
        task = await session.get(Task, open_.task_id) if open_.task_id else None
        raise ServiceError(f"A proposal is already waiting for the user's decision{f' (ticket #{task.number})' if task else ''}.")
    fp = fingerprint(after)
    keys = list(dict.fromkeys(pages))
    for old in await declined(session, site.id):
        if old.fingerprint == fp or (keys and set(old.pages or []) == set(keys)):
            task = await session.get(Task, old.task_id) if old.task_id else None
            raise ServiceError(f"The user declined this restructuring{f' on ticket #{task.number}' if task else ''}: don't propose it again.")
    rows = {p.page_key: p for p in await session.scalars(select(DocPage).where(DocPage.site_id == site.id, DocPage.until_seq.is_(None), DocPage.page_key.in_(keys)))} if keys else {}
    if unknown := [k for k in keys if k not in rows]:
        raise ServiceError(f"Not pages of {site.slug}: {', '.join(map(str, unknown[:20]))}.")
    concerned = "\n".join(f"- {rows[k].title} (/{rows[k].path}, doc-page:{k})" for k in keys[:100])
    details = f"{reason.strip()}\n\n**Outline now**\n```\n{before.strip()}\n```\n\n**Outline proposed**\n```\n{after.strip()}\n```" + (
        f"\n\n**Pages concerned**\n{concerned}" if concerned else ""
    )
    note = (
        f"\n\n_Proposed by the doc agent of the doc site {site.name} (/docs/{site.slug}/). PlainBoard orders the restructuring "
        "when the user approves it on this ticket; declined, it is not proposed again._"
    )
    task = await board_service.create_task(session, site.project_id, TaskCreate(title=truncate(title.strip(), 300), description=details + note, labels=["docs"]), by_agent_id=agent.id)
    decision = await decision_service.create(
        session, task, f"{title.strip()} ({site.name})", details=details,
        options=[DecisionOption(label=truncate(labels[0], 200)), DecisionOption(label=truncate(labels[1], 200))], agent_id=agent.id,
    )  # fmt: skip
    proposal = DocProposal(
        project_id=site.project_id, site_id=site.id, task_id=task.id, decision_id=decision.id, title=truncate(title.strip(), 200), reason=reason.strip(),
        before=before.strip(), after=after.strip(), pages=keys, fingerprint=fp,
    )  # fmt: skip
    session.add(proposal)
    await session.commit()
    return proposal, task


async def of_decision(session: AsyncSession, decision_id: int) -> DocProposal | None:
    return await session.scalar(select(DocProposal).where(DocProposal.decision_id == decision_id).limit(1))


async def settle(session: AsyncSession, proposal: DocProposal, decision: Decision, task: Task) -> str | None:
    """The user's answer kept with the proposal; returns the doc agent's instructions (None: declined, nothing to do)."""
    if decision.answer_option == KEEP:
        proposal.status = DECLINED
        await session.commit()
        return None
    proposal.status = APPROVED if decision.answer_option == APPROVE else ANSWERED
    await session.commit()
    said = f"The user answered in their own words: {decision.answer_text.strip()}\nDo what they asked; if it means no, change nothing and say so." if proposal.status == ANSWERED else "The user approved it."
    if proposal.status == APPROVED and decision.answer_text.strip():
        said += f" Their note: {decision.answer_text.strip()}"
    return (
        f"Restructure your doc site as you proposed on ticket #{task.number} ({proposal.title}). {said}\n\n"
        f"Outline proposed:\n```\n{proposal.after}\n```\n"
        "- Move pages with write_doc_page move/parent/position rather than writing them anew: a page keeps its id, so its "
        "doc-page: links stay valid, and its slug where you can (old addresses of moved pages still find it by its slug).\n"
        "- Merge: move the content into the page that stays, link that one where the other was linked (list_docs query), then "
        "delete the other (write_doc_page delete). Split: a new page for the part, linked from the old one.\n"
        "- Areas and groups of the navigation: doc_outline, then write_doc_page area/group for each top-level page.\n"
        "- Keep each page's code paths, keep the doc spec, update your site overview in your notes, and report briefly what changed."
    )


async def may_delete(session: AsyncSession, site_id: int, assignment_id: int | None) -> bool:
    """A doc agent deletes pages only in a restructuring the user approved: its assignment is on that proposal's ticket."""
    if not assignment_id:
        return False
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None or assignment.task_id is None:
        return False
    q = select(DocProposal.id).where(DocProposal.site_id == site_id, DocProposal.task_id == assignment.task_id, DocProposal.status.in_((APPROVED, ANSWERED))).limit(1)
    return await session.scalar(q) is not None
