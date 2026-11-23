from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import select

from ..models import Assignment, Run, TaskStatus
from ..schemas import (
    AssignmentOut,
    AttachmentOut,
    CommentCreate,
    CommentOut,
    DecisionAnswer,
    DecisionOut,
    FlowFinding,
    FlowHistory,
    FlowLive,
    FlowTicketBranch,
    RunOut,
    StatsOut,
    TaskCreate,
    TaskDetail,
    TaskOut,
    TaskUpdate,
    UserActionDone,
)
from ..services import board as board_service
from ..services import decisions as decision_service
from ..services import flow as flow_service
from ..services import flow_check as flow_check_service
from ..services import projects as project_service
from ..services import stats as stats_service
from ..services import trace as trace_service
from ..services import users as user_service
from ..services.errors import NotFound, ServiceError
from .deps import Runs, Session, Who

router = APIRouter(prefix="/api", tags=["board"])


@router.get("/projects/{project_id}/tasks", response_model=list[TaskOut])
async def list_tasks(project_id: int, session: Session, q: str | None = None, archived: bool = False) -> list[TaskOut]:
    return await board_service.task_outs(session, await board_service.list_tasks(session, project_id, query=q, archived=archived))


@router.post("/projects/{project_id}/tasks/archive")
async def archive_done_tasks(project_id: int, session: Session) -> dict[str, int]:
    """Takes all done tickets off the board now."""
    return {"archived": await board_service.archive_tasks(session, project_id)}


@router.post("/projects/{project_id}/tasks", response_model=TaskOut, status_code=201)
async def create_task(project_id: int, body: TaskCreate, session: Session) -> TaskOut:
    task = await board_service.create_task(session, project_id, body)
    (out,) = await board_service.task_outs(session, [task])
    return out


@router.get("/tasks/{task_id}", response_model=TaskDetail)
async def get_task(task_id: int, session: Session) -> TaskDetail:
    return await board_service.task_detail(session, await board_service.get_task(session, task_id))


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(task_id: int, body: TaskUpdate, session: Session, runs: Runs, who: Who) -> TaskOut:
    task, previous = await board_service.update_task(session, await board_service.get_task(session, task_id), body)
    # Moving a ticket to "In Progress" on the board hands it to the manager.
    if task.status == TaskStatus.IN_PROGRESS and previous != TaskStatus.IN_PROGRESS:
        try:
            await runs.hand_ticket(session, task, await user_service.author(session, who.user, task.project_id))
        except ServiceError:
            pass
    (out,) = await board_service.task_outs(session, [task])
    return out


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: int, session: Session) -> None:
    await board_service.delete_task(session, await board_service.get_task(session, task_id))


@router.post("/tasks/{task_id}/start", response_model=RunOut)
async def start_task(task_id: int, session: Session, runs: Runs, who: Who) -> RunOut:
    """Hand the ticket to the manager, who plans and delegates it."""
    task = await board_service.get_task(session, task_id)
    run = await runs.hand_ticket(session, task, await user_service.author(session, who.user, task.project_id))
    return RunOut.model_validate(run)


@router.post("/tasks/{task_id}/user-action/done", response_model=TaskOut)
async def user_action_done(task_id: int, body: UserActionDone, session: Session, runs: Runs, who: Who) -> TaskOut:
    task = await board_service.get_task(session, task_id)
    author = await user_service.author(session, who.user, task.project_id)
    action = await board_service.finish_user_action(session, task, body.note, author)
    await runs.user_action_done(session, task, action, body.note, author)
    (out,) = await board_service.task_outs(session, [task])
    return out


@router.get("/projects/{project_id}/stats", response_model=StatsOut)
async def stats(project_id: int, session: Session, days: int = Query(default=30, ge=1, le=365)) -> StatsOut:
    return await stats_service.overview(session, project_id, days)


@router.get("/projects/{project_id}/flow-check", response_model=list[FlowFinding])
async def flow_check(project_id: int, session: Session, hours: int = Query(default=24, ge=1, le=336)) -> list[FlowFinding]:
    """What PlainBoard's own checks found that slows the team down, heaviest first."""
    await project_service.get(session, project_id)
    return [FlowFinding(key=f.key, title=f.title, detail=f.detail, action=f.action) for f in await flow_check_service.findings(session, project_id, hours)]


@router.get("/projects/{project_id}/flow", response_model=FlowLive)
async def flow_live(project_id: int, session: Session) -> FlowLive:
    """What works where, is checked and lands right now."""
    return await flow_service.live(session, project_id)


@router.get("/projects/{project_id}/flow/history", response_model=FlowHistory)
async def flow_history(project_id: int, session: Session, repository_id: int | None = None, before: datetime | None = None, limit: int = Query(default=30, ge=1, le=100)) -> FlowHistory:
    """Trains, branch sweeps and stuck branches, newest first; ``before`` is the ``next_before`` of the page before."""
    if before is not None and before.tzinfo is not None:
        before = before.astimezone(UTC).replace(tzinfo=None)
    return await flow_service.history(session, project_id, repository_id=repository_id, before=before, limit=limit)


@router.get("/tasks/{task_id}/flow", response_model=list[FlowTicketBranch])
async def task_flow(task_id: int, session: Session) -> list[FlowTicketBranch]:
    """The way the ticket's branches take to the base branch."""
    await board_service.get_task(session, task_id)
    return await flow_service.of_ticket(session, task_id)


@router.get("/projects/{project_id}/work-report")
async def work_report(project_id: int, session: Session, days: int = Query(default=14, ge=1, le=365)) -> JSONResponse:
    """A file to hand to whoever looks for overhead: numbers and tool names, no texts."""
    report = await stats_service.work_report(session, project_id, days)
    return JSONResponse(report, headers={"Content-Disposition": f'attachment; filename="plainboard-report-{project_id}.json"'})


@router.get("/projects/{project_id}/work-log")
async def work_log(project_id: int, session: Session, days: int = Query(default=7, ge=1, le=365)) -> StreamingResponse:
    """Everything of the period as it happened, with all texts (gzip'ed JSON lines): to see why a flow costs what it costs."""
    await project_service.get(session, project_id)
    headers = {"Content-Disposition": f'attachment; filename="plainboard-log-{project_id}.jsonl.gz"'}
    return StreamingResponse(trace_service.stream(project_id, days), media_type="application/gzip", headers=headers)


@router.get("/tasks/{task_id}/runs", response_model=list[RunOut])
async def task_runs(task_id: int, session: Session) -> list[RunOut]:
    rows = (await session.scalars(select(Run).where(Run.task_id == task_id).order_by(Run.id.desc()).limit(50))).all()
    return [RunOut.model_validate(r) for r in rows]


# --- comments & attachments -----------------------------------------------------------------


@router.get("/tasks/{task_id}/comments", response_model=list[CommentOut])
async def list_comments(task_id: int, session: Session, before: int | None = None, limit: int = Query(default=50, ge=1, le=200)) -> list[CommentOut]:
    """Comments older than id `before` (or the newest), oldest first. The ticket view pages with it."""
    task = await board_service.get_task(session, task_id)
    return [CommentOut.model_validate(c) for c in await board_service.recent_comments(session, task.id, limit, before=before)]


@router.post("/tasks/{task_id}/comments", response_model=CommentOut, status_code=201)
async def add_comment(task_id: int, body: CommentCreate, session: Session, runs: Runs, who: Who) -> CommentOut:
    task = await board_service.get_task(session, task_id)
    author = await user_service.author(session, who.user, task.project_id)
    comment = await board_service.add_comment(session, task, body.body, author_name=author, by=who.user)
    out = CommentOut.model_validate(comment)
    await runs.comment_added(session, task, comment)
    return out


@router.post("/tasks/{task_id}/attachments", response_model=list[AttachmentOut], status_code=201)
async def upload(task_id: int, session: Session, files: list[UploadFile] = File(...)) -> list[AttachmentOut]:
    task = await board_service.get_task(session, task_id)
    out = []
    for file in files:
        attachment = await board_service.add_attachment(session, task, file.filename or "file", file.content_type or "", file.file)
        out.append(AttachmentOut.model_validate(attachment))
    return out


@router.post("/decisions/{decision_id}/answer", response_model=DecisionOut)
async def answer_decision(decision_id: int, body: DecisionAnswer, session: Session, runs: Runs, who: Who) -> DecisionOut:
    decision = await decision_service.get(session, decision_id)
    author = await user_service.author(session, who.user, decision.project_id)
    await decision_service.answer(session, decision, option_index=body.option_index, text=body.text, author_name=author)
    await runs.decision_answered(session, decision, author)
    return DecisionOut.model_validate(decision)


@router.get("/attachments/{attachment_id}")
async def download(attachment_id: int, session: Session, inline: bool = True) -> FileResponse:
    attachment = await board_service.get_attachment(session, attachment_id)
    path = board_service.storage_path(attachment)
    if attachment.purged_at is not None:
        raise ServiceError(board_service.purged_text(attachment), 410)
    if not path.exists():
        raise NotFound("File missing on disk")
    # Inline only what is passive by its content (pictures, PDF, plain text); never a page or an SVG on this origin.
    shown = board_service.inline_type(path, attachment.filename) if inline else None
    headers = {"X-Content-Type-Options": "nosniff"}
    if shown != "application/pdf":  # the browsers' PDF viewers do not open under the sandbox; a PDF runs no script on the origin
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    if shown:
        return FileResponse(path, media_type=shown, filename=attachment.filename, content_disposition_type="inline", headers=headers)
    return FileResponse(path, media_type=attachment.content_type, filename=attachment.filename, content_disposition_type="attachment", headers=headers)


@router.delete("/attachments/{attachment_id}", status_code=204)
async def delete_attachment(attachment_id: int, session: Session) -> None:
    await board_service.delete_attachment(session, await board_service.get_attachment(session, attachment_id))


# --- assignments ----------------------------------------------------------------------------


@router.get("/projects/{project_id}/assignments", response_model=list[AssignmentOut])
async def list_assignments(project_id: int, session: Session, agent_id: int | None = None, limit: int = 50) -> list[AssignmentOut]:
    q = select(Assignment).where(Assignment.project_id == project_id)
    if agent_id:
        q = q.where(Assignment.agent_id == agent_id)
    rows = (await session.scalars(q.order_by(Assignment.id.desc()).limit(min(limit, 200)))).all()
    return [AssignmentOut.model_validate(a) for a in rows]


@router.post("/assignments/{assignment_id}/cancel")
async def cancel_assignment(assignment_id: int, session: Session, runs: Runs) -> dict[str, bool]:
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        raise NotFound("Assignment not found")
    return {"cancelled": bool(assignment.run_id and await runs.cancel_run(assignment.run_id))}
