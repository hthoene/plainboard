"""Internal API for the runner: tool calls and file uploads, authenticated by per-run tokens."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Header, HTTPException, UploadFile
from plainboard_protocol import ToolCallResult

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
            session, task, file.filename or "file", file.content_type or "", await file.read(), agent_id=ctx.agent_id
        )
    except ServiceError as exc:
        return ToolCallResult(text=str(exc), is_error=True)
    return ToolCallResult(text=f"Attached {attachment.filename} to ticket #{task.number} (attachment id {attachment.id}).")
