from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from ..models import ACTIVE_RUN_STATUSES, Run
from ..orchestration.runner_client import RunnerError
from ..schemas import AgentCreate, AgentOut, AgentUpdate, CheckOut, ProjectCreate, ProjectOut, ProjectUpdate, PublicApiKey
from ..services import agents as agent_service
from ..services import projects as project_service
from ..services import repositories as repo_service
from ..services import settings as settings_service
from ..services.errors import ServiceError
from .deps import Runner, Runs, Session

router = APIRouter(prefix="/api", tags=["projects"])


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(session: Session) -> list[ProjectOut]:
    return [project_service.to_out(p) for p in await project_service.list_all(session)]


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectCreate, session: Session) -> ProjectOut:
    return project_service.to_out(await project_service.create(session, body))


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: int, session: Session) -> ProjectOut:
    return project_service.to_out(await project_service.get(session, project_id))


@router.patch("/projects/{project_id}", response_model=ProjectOut)
async def update_project(project_id: int, body: ProjectUpdate, session: Session) -> ProjectOut:
    project = await project_service.get(session, project_id)
    return project_service.to_out(await project_service.update(session, project, body))


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: int, session: Session) -> None:
    project = await project_service.get(session, project_id)
    if await session.scalar(select(Run.id).where(Run.project_id == project_id, Run.status.in_(ACTIVE_RUN_STATUSES))):
        raise ServiceError("Stop all running agents of this project first")
    await project_service.delete(session, project)


@router.post("/projects/{project_id}/check-github", response_model=CheckOut)
async def check_project_github(project_id: int, session: Session, runner: Runner) -> CheckOut:
    project = await project_service.get(session, project_id)
    values = await settings_service.get_all(session)
    urls = [r.url for r in await repo_service.list_for_project(session, project_id)]
    result = await runner.check_github(settings_service.credentials(values, project_service.github_token(project)), urls)
    return CheckOut(ok=result.ok, detail=result.detail)


@router.post("/projects/{project_id}/public-api-key", response_model=PublicApiKey, status_code=201)
async def create_public_api_key(project_id: int, session: Session) -> PublicApiKey:
    project = await project_service.get(session, project_id)
    return PublicApiKey(key=await project_service.create_public_api_key(session, project))


@router.delete("/projects/{project_id}/public-api-key", status_code=204)
async def revoke_public_api_key(project_id: int, session: Session) -> None:
    await project_service.revoke_public_api_key(session, await project_service.get(session, project_id))


# --- agents ---------------------------------------------------------------------------------


@router.get("/projects/{project_id}/agents", response_model=list[AgentOut])
async def list_agents(project_id: int, session: Session) -> list[AgentOut]:
    return await agent_service.to_out(session, await agent_service.list_for_project(session, project_id))


async def _validate_model(runner: Runner, provider: str | None, model: str | None) -> None:
    if not provider:
        return
    try:
        names = {p.name for p in await runner.providers()}
    except RunnerError:
        return
    if provider not in names:
        raise ServiceError(f"Unknown provider '{provider}'")


@router.post("/projects/{project_id}/agents", response_model=AgentOut, status_code=201)
async def create_agent(project_id: int, body: AgentCreate, session: Session, runner: Runner) -> AgentOut:
    await project_service.get(session, project_id)
    await _validate_model(runner, body.provider, body.model)
    agent = await agent_service.create(session, project_id, body)
    (out,) = await agent_service.to_out(session, [agent])
    return out


@router.patch("/agents/{agent_id}", response_model=AgentOut)
async def update_agent(agent_id: int, body: AgentUpdate, session: Session, runner: Runner) -> AgentOut:
    await _validate_model(runner, body.provider, body.model)
    agent = await agent_service.update(session, await agent_service.get(session, agent_id), body)
    (out,) = await agent_service.to_out(session, [agent])
    return out


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: int, session: Session, runs: Runs) -> None:
    agent = await agent_service.get(session, agent_id)
    active = (await session.scalars(select(Run.id).where(Run.agent_id == agent_id, Run.status.in_(ACTIVE_RUN_STATUSES)))).all()
    for run_id in active:
        await runs.cancel_run(run_id)
    if active:
        raise ServiceError("The agent is still working; its runs are being cancelled. Try again in a moment.")
    await agent_service.delete(session, agent)
