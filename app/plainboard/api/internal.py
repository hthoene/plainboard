"""Internal API for the runner: tool calls and file uploads, authenticated by per-run tokens."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from plainboard_protocol import TOOL_PENDING_HEADER, ToolCallResult

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


# A tool call that takes longer is answered "pending" and asked for again: a proxy between an external runner
# and the app (Cloudflare: 100 s) ends a longer request, and the tool's result would reach nobody.
TOOL_ANSWER_SECONDS = 60.0
KEEP_UNASKED_SECONDS = 900  # a result its turn never asked for again
_calls: dict[str, tuple[int, asyncio.Task[ToolCallResult]]] = {}  # call id -> (run, the call at work)


async def _answer(call_id: str) -> ToolCallResult:
    task = _calls[call_id][1]
    await asyncio.wait({task}, timeout=TOOL_ANSWER_SECONDS)
    if not task.done():
        return ToolCallResult(text="", pending=call_id)
    del _calls[call_id]
    return task.result()


def _drop(call_id: str, task: asyncio.Task) -> None:
    if call_id in _calls and _calls[call_id][1] is task:
        del _calls[call_id]


@router.post("/runs/{run_id}/tools/{name}", response_model=ToolCallResult)
async def call_tool(run_id: int, name: str, args: dict[str, Any], runs: Runs, request: Request, authorization: str | None = Header(default=None)) -> ToolCallResult:
    ctx = _context(runs, run_id, authorization)
    if not request.headers.get(TOOL_PENDING_HEADER):  # a runner older than that: it waits for the whole call
        return await runs.execute_tool(ctx, name, args)
    call_id = secrets.token_hex(8)
    task = asyncio.create_task(runs.execute_tool(ctx, name, args), name=f"tool-{name}-{run_id}")
    task.add_done_callback(lambda t: asyncio.get_running_loop().call_later(KEEP_UNASKED_SECONDS, _drop, call_id, t))
    _calls[call_id] = (run_id, task)
    return await _answer(call_id)


@router.post("/runs/{run_id}/tools/pending/{call_id}", response_model=ToolCallResult)
async def pending_tool_call(run_id: int, call_id: str, runs: Runs, authorization: str | None = Header(default=None)) -> ToolCallResult:
    _context(runs, run_id, authorization)
    if _calls.get(call_id, (None,))[0] != run_id:
        return ToolCallResult(text="This tool call's result is gone.", is_error=True)
    return await _answer(call_id)


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
