"""Notes, schedules, MCP servers and skills."""

from __future__ import annotations

import json

from fastapi import APIRouter, File, Form, UploadFile

from ..schemas import (
    McpServerCreate,
    McpServerOut,
    McpServerUpdate,
    NoteCreate,
    NoteOut,
    NoteUpdate,
    ScheduleCreate,
    ScheduleOut,
    ScheduleUpdate,
    SkillImport,
    SkillOut,
    SkillUpdate,
)
from ..services import mcp as mcp_service
from ..services import notes as note_service
from ..services import projects as project_service
from ..services import schedules as schedule_service
from ..services import skills as skill_service
from ..services.errors import ServiceError
from .deps import Runs, Session

router = APIRouter(prefix="/api/projects/{project_id}", tags=["knowledge"])


@router.get("/notes", response_model=list[NoteOut])
async def list_notes(project_id: int, session: Session, q: str | None = None) -> list[NoteOut]:
    return [NoteOut.model_validate(n) for n in await note_service.list_for_project(session, project_id, q)]


@router.post("/notes", response_model=NoteOut, status_code=201)
async def create_note(project_id: int, body: NoteCreate, session: Session) -> NoteOut:
    await project_service.get(session, project_id)
    return NoteOut.model_validate(await note_service.create(session, project_id, body))


@router.patch("/notes/{note_id}", response_model=NoteOut)
async def update_note(project_id: int, note_id: int, body: NoteUpdate, session: Session) -> NoteOut:
    note = await note_service.get(session, note_id, project_id)
    return NoteOut.model_validate(await note_service.update(session, note, body))


@router.delete("/notes/{note_id}", status_code=204)
async def delete_note(project_id: int, note_id: int, session: Session) -> None:
    await note_service.delete(session, await note_service.get(session, note_id, project_id))


@router.get("/schedules", response_model=list[ScheduleOut])
async def list_schedules(project_id: int, session: Session) -> list[ScheduleOut]:
    return [ScheduleOut.model_validate(s) for s in await schedule_service.list_for_project(session, project_id)]


@router.post("/schedules", response_model=ScheduleOut, status_code=201)
async def create_schedule(project_id: int, body: ScheduleCreate, session: Session) -> ScheduleOut:
    return ScheduleOut.model_validate(await schedule_service.create(session, project_id, body))


@router.patch("/schedules/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(project_id: int, schedule_id: int, body: ScheduleUpdate, session: Session) -> ScheduleOut:
    schedule = await schedule_service.get(session, schedule_id, project_id)
    return ScheduleOut.model_validate(await schedule_service.update(session, schedule, body))


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(project_id: int, schedule_id: int, session: Session) -> None:
    await schedule_service.delete(session, await schedule_service.get(session, schedule_id, project_id))


@router.get("/mcp-servers", response_model=list[McpServerOut])
async def list_mcp(project_id: int, session: Session) -> list[McpServerOut]:
    return await mcp_service.to_out(session, await mcp_service.list_for_project(session, project_id))


@router.post("/mcp-servers", response_model=McpServerOut, status_code=201)
async def create_mcp(project_id: int, body: McpServerCreate, session: Session) -> McpServerOut:
    await project_service.get(session, project_id)
    (out,) = await mcp_service.to_out(session, [await mcp_service.create(session, project_id, body)])
    return out


@router.patch("/mcp-servers/{server_id}", response_model=McpServerOut)
async def update_mcp(project_id: int, server_id: int, body: McpServerUpdate, session: Session) -> McpServerOut:
    server = await mcp_service.get(session, server_id, project_id)
    (out,) = await mcp_service.to_out(session, [await mcp_service.update(session, server, body, project_id)])
    return out


@router.delete("/mcp-servers/{server_id}", status_code=204)
async def delete_mcp(project_id: int, server_id: int, session: Session) -> None:
    await mcp_service.delete(session, await mcp_service.get(session, server_id, project_id), project_id)


async def _upload(file: UploadFile) -> skill_service.Bundle:
    data = await file.read(skill_service.MAX_UPLOAD_BYTES + 1)
    return skill_service.parse_upload(file.filename or "", data)


def _ids(raw: str) -> list[int]:
    try:
        return [int(i) for i in json.loads(raw or "[]")]
    except (ValueError, TypeError) as exc:
        raise ServiceError("agent_ids must be a list of ids") from exc


@router.get("/skills", response_model=list[SkillOut])
async def list_skills(project_id: int, session: Session) -> list[SkillOut]:
    return [SkillOut.model_validate(s) for s in await skill_service.list_for_project(session, project_id)]


@router.post("/skills", response_model=SkillOut, status_code=201)
async def upload_skill(
    project_id: int, session: Session, file: UploadFile = File(...), all_agents: bool = Form(True), agent_ids: str = Form("[]")
) -> SkillOut:
    """A zip of the skill's folder or a single SKILL.md."""
    await project_service.get(session, project_id)
    bundle = await _upload(file)
    return SkillOut.model_validate(await skill_service.store(session, project_id, bundle, all_agents=all_agents, agent_ids=_ids(agent_ids)))


@router.post("/skills/import", response_model=SkillOut, status_code=201)
async def import_skill(project_id: int, body: SkillImport, session: Session, runs: Runs) -> SkillOut:
    project = await project_service.get(session, project_id)
    return SkillOut.model_validate(await skill_service.import_git(session, runs.runners.of(project_id), project, body))


@router.patch("/skills/{skill_id}", response_model=SkillOut)
async def update_skill(project_id: int, skill_id: int, body: SkillUpdate, session: Session) -> SkillOut:
    skill = await skill_service.get(session, skill_id, project_id)
    return SkillOut.model_validate(await skill_service.update(session, skill, body))


@router.put("/skills/{skill_id}/files", response_model=SkillOut)
async def replace_skill_files(project_id: int, skill_id: int, session: Session, file: UploadFile = File(...)) -> SkillOut:
    """New files for an uploaded skill."""
    skill = await skill_service.get(session, skill_id, project_id)
    bundle = await _upload(file)
    return SkillOut.model_validate(await skill_service.store(session, project_id, bundle, skill=skill))


@router.post("/skills/{skill_id}/reimport", response_model=SkillOut)
async def reimport_skill(project_id: int, skill_id: int, session: Session, runs: Runs) -> SkillOut:
    skill = await skill_service.get(session, skill_id, project_id)
    return SkillOut.model_validate(await skill_service.reimport(session, runs.runners.of(project_id), skill))


@router.delete("/skills/{skill_id}", status_code=204)
async def delete_skill(project_id: int, skill_id: int, session: Session) -> None:
    await skill_service.delete(session, await skill_service.get(session, skill_id, project_id))
