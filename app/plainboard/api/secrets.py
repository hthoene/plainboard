from __future__ import annotations

from fastapi import APIRouter

from ..schemas import SecretCreate, SecretOut, SecretUpdate
from ..services import projects as project_service
from ..services import secrets as secret_service
from .deps import Session

router = APIRouter(prefix="/api", tags=["secrets"])


@router.get("/secrets", response_model=list[SecretOut])
async def list_global(session: Session) -> list[SecretOut]:
    return [secret_service.to_out(s) for s in await secret_service.list_secrets(session, None)]


@router.post("/secrets", response_model=SecretOut, status_code=201)
async def create_global(body: SecretCreate, session: Session) -> SecretOut:
    return secret_service.to_out(await secret_service.create(session, None, body))


@router.get("/projects/{project_id}/secrets", response_model=list[SecretOut])
async def list_for_project(project_id: int, session: Session) -> list[SecretOut]:
    return [secret_service.to_out(s) for s in await secret_service.list_secrets(session, project_id)]


@router.post("/projects/{project_id}/secrets", response_model=SecretOut, status_code=201)
async def create_for_project(project_id: int, body: SecretCreate, session: Session) -> SecretOut:
    await project_service.get(session, project_id)
    return secret_service.to_out(await secret_service.create(session, project_id, body))


@router.patch("/secrets/{secret_id}", response_model=SecretOut)
async def update(secret_id: int, body: SecretUpdate, session: Session) -> SecretOut:
    return secret_service.to_out(await secret_service.update(session, await secret_service.get(session, secret_id), body))


@router.delete("/secrets/{secret_id}", status_code=204)
async def delete(secret_id: int, session: Session) -> None:
    await secret_service.delete(session, await secret_service.get(session, secret_id))
