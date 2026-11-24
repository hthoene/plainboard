from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from plainboard_protocol import Credentials
from pydantic import BaseModel, Field

from ..orchestration.runner_client import RunnerError
from ..schemas import PromoteIn, PromotionOut, PromotionPreview, RepositoryCreate, RepositoryOut, RepositoryUpdate
from ..services import ownership, permissions
from ..services import projects as project_service
from ..services import promotions as promotion_service
from ..services import repositories as repo_service
from ..services import settings as settings_service
from ..services.errors import ServiceError
from .deps import Runs, Session, Who

router = APIRouter(prefix="/api", tags=["repositories"])


async def _github(session: Session, who: Who, project_id: int | None) -> Credentials | None:
    """For GitHub: a project's own token first, else the installation's if the project may use it
    (``permissions.shared``). Without a project (``/api/github``): the installation's, for admins."""
    if project_id is None:
        return settings_service.credentials(await settings_service.get_all(session)) if who.is_admin else None
    credentials = await permissions.credentials(session, await project_service.get(session, project_id))
    return credentials if credentials.github_token else None


NO_OWN_TOKEN = {"ok": False, "detail": "This project has no GitHub token of its own yet: add one in the project settings, or add the repository by its address.", "repos": []}


@router.get("/github/repos")
async def github_repos(session: Session, runs: Runs, who: Who) -> dict[str, Any]:
    """Repositories visible to the installation's GitHub token, for the repo picker."""
    return await project_github_repos(None, session, runs, who)


@router.get("/projects/{project_id}/github/repos")
async def project_github_repos(project_id: int | None, session: Session, runs: Runs, who: Who) -> dict[str, Any]:
    """Repositories visible to the project's GitHub token, for the repo picker."""
    credentials = await _github(session, who, project_id)
    return await runs.runners.of(project_id).github_repos(credentials) if credentials else NO_OWN_TOKEN  # a project's token goes to its own runner only


class GithubRepoIn(BaseModel):
    name: str = Field(pattern=r"^[\w.-]{1,100}$")
    owner: str = Field(default="", pattern=r"^[\w.-]{0,100}$")  # an organisation. Empty: the token's own account
    private: bool = True


@router.post("/github/repos")
async def create_github_repo(body: GithubRepoIn, session: Session, runs: Runs, who: Who) -> dict[str, Any]:
    """A new repository on GitHub, made with the GitHub token. The answer has the shape of the list above."""
    return await create_project_github_repo(None, body, session, runs, who)


@router.post("/projects/{project_id}/github/repos")
async def create_project_github_repo(project_id: int | None, body: GithubRepoIn, session: Session, runs: Runs, who: Who) -> dict[str, Any]:
    credentials = await _github(session, who, project_id)
    return await runs.runners.of(project_id).github_create_repo(credentials, body.name, body.owner, body.private) if credentials else NO_OWN_TOKEN


@router.get("/projects/{project_id}/repositories", response_model=list[RepositoryOut])
async def list_repositories(project_id: int, session: Session) -> list[RepositoryOut]:
    return await repo_service.to_out(session, await repo_service.list_for_project(session, project_id))


@router.post("/projects/{project_id}/repositories", response_model=RepositoryOut, status_code=201)
async def create_repository(project_id: int, body: RepositoryCreate, session: Session) -> RepositoryOut:
    await project_service.get(session, project_id)
    (out,) = await repo_service.to_out(session, [await repo_service.create(session, project_id, body)])
    return out


@router.patch("/repositories/{repo_id}", response_model=RepositoryOut)
async def update_repository(repo_id: int, body: RepositoryUpdate, session: Session, who: Who) -> RepositoryOut:
    repo = await repo_service.get(session, repo_id)
    holder = ownership.holds(await project_service.get(session, repo.project_id), who.user)
    repo = await repo_service.update(session, repo, body, keep_access=holder)
    (out,) = await repo_service.to_out(session, [repo])
    return out


@router.delete("/repositories/{repo_id}", status_code=204)
async def delete_repository(repo_id: int, session: Session) -> None:
    await repo_service.delete(session, await repo_service.get(session, repo_id))


@router.get("/repositories/{repo_id}/promotion", response_model=PromotionPreview)
async def promotion_preview(repo_id: int, session: Session, runs: Runs) -> PromotionPreview:
    """What promoting the base branch to the release branch would bring now (#129)."""
    try:
        return await promotion_service.preview(session, runs, await repo_service.get(session, repo_id))
    except RunnerError as exc:
        raise ServiceError(f"The repository could not be read: {exc}", 502) from exc


@router.post("/repositories/{repo_id}/promotion", response_model=PromotionOut, status_code=201)
async def promote(repo_id: int, body: PromoteIn, session: Session, runs: Runs, who: Who) -> PromotionOut:
    """Promote: check the base branch's commit once, then bring it to the release branch."""
    try:
        promotion = await promotion_service.start(session, runs, await repo_service.get(session, repo_id), by=who.name, tag=body.tag, sha=body.sha)
    except RunnerError as exc:
        raise ServiceError(f"The repository could not be read: {exc}", 502) from exc
    return promotion_service.out(promotion)
