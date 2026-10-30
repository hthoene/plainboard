"""Decisions: the manager asks the user on a ticket; the user picks an option or answers freely."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Attachment, Decision, DecisionStatus, Task, utcnow
from ..schemas import DecisionOption, DecisionOut
from . import board as board_service
from .errors import NotFound, ServiceError


async def get(session: AsyncSession, decision_id: int) -> Decision:
    decision = await session.get(Decision, decision_id)
    if decision is None:
        raise NotFound("Decision not found")
    return decision


async def get_in_project(session: AsyncSession, project_id: int, decision_id: int) -> Decision:
    decision = await get(session, decision_id)
    if decision.project_id != project_id:
        raise NotFound("Decision not found")
    return decision


async def _publish(session: AsyncSession, decision: Decision, task: Task) -> None:
    bus.publish(decision.project_id, "decision.upserted", DecisionOut.model_validate(decision))
    await board_service.publish_task(session, task)


async def create(
    session: AsyncSession,
    task: Task,
    question: str,
    *,
    details: str = "",
    options: list[DecisionOption] | None = None,
    attachment_ids: list[int] | None = None,
    agent_id: int | None = None,
) -> Decision:
    options = options or []
    attachment_ids = attachment_ids or []
    wanted = set(attachment_ids) | {i for option in options for i in option.attachment_ids}
    if wanted:
        known = set((await session.scalars(select(Attachment.id).where(Attachment.task_id == task.id, Attachment.id.in_(wanted)))).all())
        if missing := sorted(wanted - known):
            raise ServiceError(f"Attachments not on ticket #{task.number}: {', '.join(map(str, missing))}")
    decision = Decision(
        project_id=task.project_id,
        task_id=task.id,
        requested_by_agent_id=agent_id,
        question=question,
        details=details,
        options=[option.model_dump() for option in options],
        attachment_ids=attachment_ids,
    )
    session.add(decision)
    await session.commit()
    await _publish(session, decision, task)
    return decision


def answer_summary(decision: Decision) -> str:
    """The answer in words, for comments and agent updates."""
    parts = []
    if decision.answer_option is not None:
        parts.append(decision.options[decision.answer_option]["label"])
    if decision.answer_text:
        parts.append(decision.answer_text)
    return " — ".join(parts)


async def answer(
    session: AsyncSession, decision: Decision, *, option_index: int | None = None, text: str = "", by: str = "user", agent_id: int | None = None
) -> Decision:
    text = text.strip()
    if decision.status != DecisionStatus.OPEN:
        raise ServiceError("This decision is already closed", 409)
    if option_index is None and not text:
        raise ServiceError("Pick an option or write an answer")
    if option_index is not None and option_index >= len(decision.options):
        raise ServiceError("Unknown option")
    decision.status = DecisionStatus.ANSWERED
    decision.answer_option = option_index
    decision.answer_text = text
    decision.answered_by = by
    decision.answered_at = utcnow()
    await session.commit()
    task = await board_service.get_task(session, decision.task_id)
    # The comment keeps the outcome in the ticket history that agents read.
    await board_service.add_comment(session, task, f"**Decision:** {decision.question}\n\n{answer_summary(decision)}", author_kind=by, agent_id=agent_id)
    await _publish(session, decision, task)
    return decision


async def withdraw(session: AsyncSession, decision: Decision) -> Decision:
    if decision.status != DecisionStatus.OPEN:
        raise ServiceError("This decision is already closed", 409)
    decision.status = DecisionStatus.WITHDRAWN
    await session.commit()
    await _publish(session, decision, await board_service.get_task(session, decision.task_id))
    return decision
