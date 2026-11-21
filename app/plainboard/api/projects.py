from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse
from plainboard_protocol import ProviderInfo
from sqlalchemy import select
from starlette.background import BackgroundTask

from ..events import bus
from ..models import ACTIVE_RUN_STATUSES, ProjectRole, Run
from ..orchestration.runner_client import RunnerError
from ..schemas import AgentCreate, AgentOut, AgentUpdate, CheckOut, ExportIn, ImportOut, PreviewOut, ProjectAccountsIn, ProjectCreate, ProjectOut, ProjectUpdate, PublicApiKeyIn, PublicApiKeyOut
from ..services import accounts as account_service
from ..services import agents as agent_service
from ..services import api_keys as api_key_service
from ..services import jobs as job_service
from ..services import preview as preview_service
from ..services import projects as project_service
from ..services import repositories as repo_service
from ..services import settings as settings_service
from ..services import transfer as transfer_service
from ..services.errors import ServiceError
from ..services import users as user_service
from .deps import Runner, Runs, Session, Who

router = APIRouter(prefix="/api", tags=["projects"])


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(session: Session, who: Who) -> list[ProjectOut]:
    """The projects the user has a part in (an admin and a read key: all), each with the user's role."""
    projects = await project_service.list_all(session)
    if who.user is None:
        return [project_service.to_out(p) for p in projects]
    roles = await user_service.roles(session, who.user)
    return [project_service.to_out(p, roles[p.id]) for p in projects if p.id in roles]


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectCreate, session: Session, who: Who) -> ProjectOut:
    project = await project_service.create(session, body)
    if who.user is not None:  # whoever creates a project owns it
        await user_service.set_member(session, project.id, who.user.id, ProjectRole.OWNER)
    return project_service.to_out(project, ProjectRole.OWNER)


@router.post("/projects/import", response_model=ImportOut, status_code=201)
async def import_project(session: Session, who: Who, file: UploadFile = File(...), passphrase: str = Form("")) -> ImportOut:
    """A project from the file another PlainBoard made (``export``). Whoever brings it in owns it."""
    result = await transfer_service.import_project(session, file.file, passphrase=passphrase)
    if who.user is not None:
        await user_service.set_member(session, result.project.id, who.user.id, ProjectRole.OWNER)
    return ImportOut(project=project_service.to_out(result.project, ProjectRole.OWNER), counts=result.counts, missing=result.missing)


@router.post("/projects/{project_id}/export")
async def export_project(project_id: int, body: ExportIn, session: Session) -> FileResponse:
    """The whole project as one file, to take it to another PlainBoard (services/transfer.py)."""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as out:
        try:
            name = await transfer_service.export_project(session, project_id, out, passphrase=body.passphrase, chats=body.chats)
        except BaseException:
            out.close()
            os.unlink(out.name)
            raise
    return FileResponse(out.name, filename=name, media_type="application/zip", background=BackgroundTask(os.unlink, out.name))


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: int, session: Session, who: Who) -> ProjectOut:
    role = await user_service.role_in(session, who.user, project_id) if who.user else None
    return project_service.to_out(await project_service.get(session, project_id), role)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
async def update_project(project_id: int, body: ProjectUpdate, session: Session, runs: Runs, who: Who) -> ProjectOut:
    project = await project_service.get(session, project_id)
    role = await user_service.role_in(session, who.user, project_id) if who.user else None
    out = project_service.to_out(await project_service.update(session, project, body), role)
    runs.recheck()  # more parallel agents: waiting turns may start now
    return out


@router.put("/projects/{project_id}/accounts", response_model=ProjectOut)
async def limit_accounts(project_id: int, body: ProjectAccountsIn, session: Session, who: Who) -> ProjectOut:
    """The only subscriptions this project's agents may run on; none: any of the installation's."""
    project = await account_service.limit_project(session, await project_service.get(session, project_id), body.account_ids)
    out = project_service.to_out(project, await user_service.role_in(session, who.user, project_id) if who.user else None)
    bus.publish(project.id, "project.updated", project_service.to_out(project))
    return out


@router.post("/projects/{project_id}/pause", response_model=ProjectOut)
async def pause_project(project_id: int, session: Session, runs: Runs, who: Who) -> ProjectOut:
    """Bring this project to rest for good: nothing of it starts until a person resumes it."""
    project = await project_service.get(session, project_id)
    await runs.pause_project(session, project, who.name)
    return project_service.to_out(project, await user_service.role_in(session, who.user, project_id) if who.user else None)


@router.delete("/projects/{project_id}/pause", response_model=ProjectOut)
async def resume_project(project_id: int, session: Session, runs: Runs, who: Who) -> ProjectOut:
    project = await project_service.get(session, project_id)
    await runs.resume_project(session, project)
    return project_service.to_out(project, await user_service.role_in(session, who.user, project_id) if who.user else None)


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: int, session: Session, runs: Runs, files: bool = False) -> None:
    """``files``: also the folders its agents worked in on the runner."""
    project = await project_service.get(session, project_id)
    if await session.scalar(select(Run.id).where(Run.project_id == project_id, Run.status.in_(ACTIVE_RUN_STATUSES))):
        raise ServiceError("Stop all running agents of this project first")
    for job in await job_service.running_for_project(session, project_id):
        await runs.stop_job(session, job, settle=False)  # nothing of it keeps running on the runner
    await project_service.delete(session, project, runs.runners.of(project_id), files=files)
    runs.runners.assign(project_id, None)


@router.post("/projects/{project_id}/check-github", response_model=CheckOut)
async def check_project_github(project_id: int, session: Session, runs: Runs) -> CheckOut:
    project = await project_service.get(session, project_id)
    values = await settings_service.get_all(session)
    repos = repo_service.specs(await repo_service.list_for_project(session, project_id))
    result = await runs.runners.of(project_id).check_github(settings_service.credentials(values, project_service.github_token(project)), repos)
    return CheckOut(ok=result.ok, detail=result.detail)


@router.get("/projects/{project_id}/public-api-keys", response_model=list[PublicApiKeyOut])
async def list_public_api_keys(project_id: int, session: Session) -> list[PublicApiKeyOut]:
    return [PublicApiKeyOut.model_validate(k) for k in await api_key_service.list_for_project(session, project_id)]


@router.post("/projects/{project_id}/public-api-keys", response_model=PublicApiKeyOut, status_code=201)
async def create_public_api_key(project_id: int, body: PublicApiKeyIn, session: Session) -> PublicApiKeyOut:
    """The answer carries the key itself, this once."""
    key, secret = await api_key_service.create(session, await project_service.get(session, project_id), body.label)
    out = PublicApiKeyOut.model_validate(key)
    out.key = secret
    return out


@router.patch("/public-api-keys/{key_id}", response_model=PublicApiKeyOut)
async def rename_public_api_key(key_id: int, body: PublicApiKeyIn, session: Session) -> PublicApiKeyOut:
    return PublicApiKeyOut.model_validate(await api_key_service.rename(session, await api_key_service.get(session, key_id), body.label))


@router.delete("/public-api-keys/{key_id}", status_code=204)
async def revoke_public_api_key(key_id: int, session: Session) -> None:
    await api_key_service.delete(session, await api_key_service.get(session, key_id))


# --- previews: the servers agents have started and left running ---------------------------------


@router.get("/projects/{project_id}/previews", response_model=list[PreviewOut])
async def list_previews(project_id: int, session: Session, runs: Runs) -> list[PreviewOut]:
    jobs = preview_service.servers(await job_service.running_for_project(session, project_id))
    try:
        ports = await runs.runners.of(project_id).job_ports([job.runner_id for job in jobs]) if jobs else {}
    except RunnerError:
        ports = {}  # the runner is restarting, or older than this: nothing to show for now
    return await preview_service.running(session, project_id, jobs, ports)


@router.post("/projects/{project_id}/previews/{job_id}/stop", status_code=204)
async def stop_preview(project_id: int, job_id: int, session: Session, runs: Runs, who: Who) -> None:
    job = await job_service.get_for_project(session, project_id, job_id)
    await runs.stop_job(session, job, by=who.name or "the user")


# --- agents ---------------------------------------------------------------------------------


@router.get("/projects/{project_id}/agents", response_model=list[AgentOut])
async def list_agents(project_id: int, session: Session) -> list[AgentOut]:
    return await agent_service.to_out(session, await agent_service.list_for_project(session, project_id))


async def _catalog(session: Session, runner: Runner, provider: str | None) -> list[ProviderInfo] | None:
    """The providers agents can run on, after checking that ``provider`` is one of them. None if the runner is down."""
    try:
        catalog = await account_service.catalog(session, await runner.providers())
    except RunnerError:
        return None
    if provider and provider not in {p.name for p in catalog}:
        raise ServiceError(f"Unknown provider '{provider}'")
    return catalog


@router.post("/projects/{project_id}/agents", response_model=AgentOut, status_code=201)
async def create_agent(project_id: int, body: AgentCreate, session: Session, runner: Runner) -> AgentOut:
    await project_service.get(session, project_id)
    catalog = await _catalog(session, runner, body.provider)
    agent = await agent_service.create(session, project_id, body, catalog=catalog)
    (out,) = await agent_service.to_out(session, [agent])
    return out


@router.patch("/agents/{agent_id}", response_model=AgentOut)
async def update_agent(agent_id: int, body: AgentUpdate, session: Session, runner: Runner, runs: Runs) -> AgentOut:
    catalog = await _catalog(session, runner, body.provider)
    agent = await agent_service.get(session, agent_id)
    was_enabled = agent.enabled
    agent = await agent_service.update(session, agent, body, catalog)
    if body.enabled is False:
        await runs.wind_down(session, agent)
    elif body.enabled and not was_enabled:
        await runs.resume(session, agent)
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
