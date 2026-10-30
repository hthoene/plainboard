"""Agent conversations, runs and context controls."""

from __future__ import annotations

from fastapi import APIRouter, File, UploadFile
from sqlalchemy import select

from ..models import Run, RunStatus
from ..schemas import AttachmentOut, ChatMessageIn, ConversationOut, MessageOut, RunOut
from ..services import agents as agent_service
from ..services import board as board_service
from ..services.errors import NotFound, ServiceError
from .deps import Runs, Session

router = APIRouter(prefix="/api", tags=["chat"])


@router.get("/agents/{agent_id}/conversation", response_model=ConversationOut)
async def get_conversation(agent_id: int, session: Session, before_id: int | None = None, limit: int = 60) -> ConversationOut:
    agent = await agent_service.get(session, agent_id)
    conv = await agent_service.conversation_for(session, agent_id)
    limit = max(1, min(limit, 200))
    rows = await agent_service.messages(session, conv, before_id=before_id, limit=limit + 1)
    out = ConversationOut.model_validate(conv)
    out.context_limit_tokens = agent.context_limit_tokens
    out.has_summary = bool(conv.summary)
    out.has_more = len(rows) > limit
    out.messages = [MessageOut.model_validate(m) for m in reversed(rows[:limit])]
    return out


@router.post("/agents/{agent_id}/messages", response_model=RunOut, status_code=202)
async def send_message(agent_id: int, body: ChatMessageIn, session: Session, runs: Runs) -> RunOut:
    agent = await agent_service.get(session, agent_id)
    return RunOut.model_validate(await runs.chat(session, agent, body.text, body.attachment_ids))


@router.post("/agents/{agent_id}/attachments", response_model=list[AttachmentOut], status_code=201)
async def upload(agent_id: int, session: Session, files: list[UploadFile] = File(...)) -> list[AttachmentOut]:
    """Store files for a chat message; send their ids with the message."""
    agent = await agent_service.get(session, agent_id)
    out = []
    for file in files:
        attachment = await board_service.store_attachment(session, agent.project_id, file.filename or "file", file.content_type or "", await file.read())
        out.append(AttachmentOut.model_validate(attachment))
    return out


@router.post("/agents/{agent_id}/compress")
async def compress(agent_id: int, session: Session, runs: Runs) -> dict[str, bool]:
    agent = await agent_service.get(session, agent_id)
    if await session.scalar(select(Run.id).where(Run.agent_id == agent_id, Run.status == RunStatus.RUNNING)):
        raise ServiceError("Agent is working; try again when it is idle")
    conv = await agent_service.conversation_for(session, agent_id)
    return {"compressed": await runs.context.compress(session, agent, conv)}


@router.post("/agents/{agent_id}/reset-session")
async def reset_session(agent_id: int, session: Session) -> dict[str, bool]:
    """Start a fresh provider session next turn (memory summary + recent messages are kept)."""
    agent = await agent_service.get(session, agent_id)
    conv = await agent_service.conversation_for(session, agent_id)
    conv.provider_session_id = None
    conv.context_tokens = 0
    await session.commit()
    await agent_service.publish(session, agent)
    return {"reset": True}


@router.get("/projects/{project_id}/runs", response_model=list[RunOut])
async def list_runs(project_id: int, session: Session, agent_id: int | None = None, active: bool = False, limit: int = 50) -> list[RunOut]:
    q = select(Run).where(Run.project_id == project_id)
    if agent_id:
        q = q.where(Run.agent_id == agent_id)
    if active:
        q = q.where(Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)))
    rows = (await session.scalars(q.order_by(Run.id.desc()).limit(min(limit, 200)))).all()
    return [RunOut.model_validate(r) for r in rows]


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int, session: Session, runs: Runs) -> dict[str, bool]:
    if await session.get(Run, run_id) is None:
        raise NotFound("Run not found")
    return {"cancelled": await runs.cancel_run(run_id)}
