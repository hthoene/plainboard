from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..schemas import RepositoryCreate, RepositoryOut, RepositoryUpdate
from ..services import projects as project_service
from ..services import repositories as repo_service
from ..services import settings as settings_service
from .deps import Runner, Session

router = APIRouter(prefix="/api", tags=["repositories"])


@router.get("/github/repos")
async def github_repos(session: Session, runner: Runner, project_id: int | None = None) -> dict[str, Any]:
    """Repositories visible to the GitHub token (project override first), for the repo picker."""
    override = project_service.github_token(await project_service.get(session, project_id)) if project_id else None
    credentials = settings_service.credentials(await settings_service.get_all(session), override)
    return await runner.github_repos(credentials)


@router.get("/projects/{project_id}/repositories", response_model=list[RepositoryOut])
async def list_repositories(project_id: int, session: Session) -> list[RepositoryOut]:
    return await repo_service.to_out(session, await repo_service.list_for_project(session, project_id))


@router.post("/projects/{project_id}/repositories", response_model=RepositoryOut, status_code=201)
async def create_repository(project_id: int, body: RepositoryCreate, session: Session) -> RepositoryOut:
    await project_service.get(session, project_id)
    (out,) = await repo_service.to_out(session, [await repo_service.create(session, project_id, body)])
    return out


@router.patch("/repositories/{repo_id}", response_model=RepositoryOut)
async def update_repository(repo_id: int, body: RepositoryUpdate, session: Session) -> RepositoryOut:
    repo = await repo_service.update(session, await repo_service.get(session, repo_id), body)
    (out,) = await repo_service.to_out(session, [repo])
    return out


@router.delete("/repositories/{repo_id}", status_code=204)
async def delete_repository(repo_id: int, session: Session) -> None:
    await repo_service.delete(session, await repo_service.get(session, repo_id))
