"""Internal API for the runner: tool calls and file uploads, authenticated by per-run tokens."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from plainboard_protocol import ToolCallResult

from ..services import agents as agent_service
from ..services import board as board_service
from ..services.errors import ServiceError
from .deps import Runs, Session

router = APIRouter(prefix="/api/internal", tags=["internal"], include_in_schema=False)


def _context(runs: Runs, run_id: int, authorization: str | None):
    token = (authorization or "").removeprefix("Bearer ").strip()
    ctx = runs.context_for_token(token, run_id) if token else None
    if ctx is None:
        raise HTTPException(401, "Invalid or expired run token")
    return ctx


@router.post("/runs/{run_id}/tools/{name}", response_model=ToolCallResult)
async def call_tool(run_id: int, name: str, args: dict[str, Any], runs: Runs, authorization: str | None = Header(default=None)) -> ToolCallResult:
    ctx = _context(runs, run_id, authorization)
    return await runs.execute_tool(ctx, name, args)


@router.post("/runs/{run_id}/attachments", response_model=ToolCallResult)
async def upload_attachment(
    run_id: int,
    runs: Runs,
    session: Session,
    file: UploadFile,
    task_number: int = Form(...),
    authorization: str | None = Header(default=None),
) -> ToolCallResult:
    ctx = _context(runs, run_id, authorization)
    try:
        task = await board_service.get_task_by_number(session, ctx.project_id, task_number)
        attachment = await board_service.add_attachment(
            session, task, file.filename or "file", file.content_type or "", file.file, agent_id=ctx.agent_id
        )
    except ServiceError as exc:
        return ToolCallResult(text=str(exc), is_error=True)
    return ToolCallResult(text=f"Attached {attachment.filename} to ticket #{task.number} (attachment id {attachment.id}).")


@router.post("/runs/{run_id}/chat-files", response_model=ToolCallResult)
async def send_chat_files(
    run_id: int,
    runs: Runs,
    session: Session,
    files: list[UploadFile] = File(default=[]),
    attachment_ids: str = Form(default=""),
    authorization: str | None = Header(default=None),
) -> ToolCallResult:
    """The send_files tool: existing attachments and new files from the agent's workspace, into its chat."""
    ctx = _context(runs, run_id, authorization)
    try:
        ids = [int(i) for i in attachment_ids.split(",") if i.strip()]
        shown = await board_service.project_attachments(session, ctx.project_id, ids)
        for file in files:
            shown.append(
                await board_service.store_attachment(session, ctx.project_id, file.filename or "file", file.content_type or "", file.file, agent_id=ctx.agent_id)
            )
        if not shown:
            return ToolCallResult(text="Nothing to send: give paths or attachment_ids.", is_error=True)
        await agent_service.send_files(session, await agent_service.get(session, ctx.agent_id), shown, run_id=ctx.run_id)
    except (ServiceError, ValueError) as exc:
        return ToolCallResult(text=str(exc), is_error=True)
    return ToolCallResult(text=f"Shown in your chat: {', '.join(f'{a.filename} (attachment {a.id})' for a in shown)}.")
