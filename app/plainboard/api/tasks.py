from __future__ import annotations

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from ..models import Assignment, Run, TaskStatus
from ..schemas import (
    AssignmentOut,
    AttachmentOut,
    CommentCreate,
    CommentOut,
    DecisionAnswer,
    DecisionOut,
    RunOut,
    TaskCreate,
    TaskDetail,
    TaskOut,
    TaskUpdate,
)
from ..services import board as board_service
from ..services import decisions as decision_service
from ..services.errors import NotFound, ServiceError
from .deps import Runs, Session

router = APIRouter(prefix="/api", tags=["board"])


@router.get("/projects/{project_id}/tasks", response_model=list[TaskOut])
async def list_tasks(project_id: int, session: Session, q: str | None = None) -> list[TaskOut]:
    return await board_service.task_outs(session, await board_service.list_tasks(session, project_id, query=q))


@router.post("/projects/{project_id}/tasks", response_model=TaskOut, status_code=201)
async def create_task(project_id: int, body: TaskCreate, session: Session) -> TaskOut:
    task = await board_service.create_task(session, project_id, body)
    (out,) = await board_service.task_outs(session, [task])
    return out


@router.get("/tasks/{task_id}", response_model=TaskDetail)
async def get_task(task_id: int, session: Session) -> TaskDetail:
    return await board_service.task_detail(session, await board_service.get_task(session, task_id))


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(task_id: int, body: TaskUpdate, session: Session, runs: Runs) -> TaskOut:
    task, previous = await board_service.update_task(session, await board_service.get_task(session, task_id), body)
    # Moving a ticket to "In Progress" on the board hands it to the manager.
    if task.status == TaskStatus.IN_PROGRESS and previous != TaskStatus.IN_PROGRESS:
        try:
            await runs.hand_ticket(session, task)
        except ServiceError:
            pass
    (out,) = await board_service.task_outs(session, [task])
    return out


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: int, session: Session) -> None:
    await board_service.delete_task(session, await board_service.get_task(session, task_id))


@router.post("/tasks/{task_id}/start", response_model=RunOut)
async def start_task(task_id: int, session: Session, runs: Runs) -> RunOut:
    """Hand the ticket to the manager, who plans and delegates it."""
    run = await runs.hand_ticket(session, await board_service.get_task(session, task_id))
    return RunOut.model_validate(run)


@router.get("/tasks/{task_id}/runs", response_model=list[RunOut])
async def task_runs(task_id: int, session: Session) -> list[RunOut]:
    rows = (await session.scalars(select(Run).where(Run.task_id == task_id).order_by(Run.id.desc()).limit(50))).all()
    return [RunOut.model_validate(r) for r in rows]


# --- comments & attachments -----------------------------------------------------------------


@router.post("/tasks/{task_id}/comments", response_model=CommentOut, status_code=201)
async def add_comment(task_id: int, body: CommentCreate, session: Session) -> CommentOut:
    task = await board_service.get_task(session, task_id)
    return CommentOut.model_validate(await board_service.add_comment(session, task, body.body))


@router.post("/tasks/{task_id}/attachments", response_model=list[AttachmentOut], status_code=201)
async def upload(task_id: int, session: Session, files: list[UploadFile] = File(...)) -> list[AttachmentOut]:
    task = await board_service.get_task(session, task_id)
    out = []
    for file in files:
        data = await file.read()
        attachment = await board_service.add_attachment(session, task, file.filename or "file", file.content_type or "", data)
        out.append(AttachmentOut.model_validate(attachment))
    return out


@router.post("/decisions/{decision_id}/answer", response_model=DecisionOut)
async def answer_decision(decision_id: int, body: DecisionAnswer, session: Session, runs: Runs) -> DecisionOut:
    decision = await decision_service.get(session, decision_id)
    await decision_service.answer(session, decision, option_index=body.option_index, text=body.text)
    await runs.decision_answered(session, decision)
    return DecisionOut.model_validate(decision)


@router.get("/attachments/{attachment_id}")
async def download(attachment_id: int, session: Session, inline: bool = True) -> FileResponse:
    attachment = await board_service.get_attachment(session, attachment_id)
    path = board_service.storage_path(attachment)
    if not path.exists():
        raise NotFound("File missing on disk")
    disposition = "inline" if inline else "attachment"
    return FileResponse(path, media_type=attachment.content_type, filename=attachment.filename, content_disposition_type=disposition)


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
