"""Git repositories of a project and which agents may use them."""

from __future__ import annotations

import re

from plainboard_protocol import RepoSpec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Agent, Repository, agent_repositories
from ..schemas import RepositoryCreate, RepositoryOut, RepositoryUpdate
from .errors import NotFound, ServiceError

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def name_from_url(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "repo"


def _validate_name(name: str) -> str:
    if not NAME_PATTERN.match(name) or name in (".", ".."):
        raise ServiceError(f"Invalid repository name '{name}': use letters, digits, '.', '-' or '_' (it becomes a folder name)")
    return name


async def _check_unique(session: AsyncSession, project_id: int, name: str, exclude_id: int | None = None) -> None:
    q = select(Repository.id).where(Repository.project_id == project_id, Repository.name == name)
    if exclude_id:
        q = q.where(Repository.id != exclude_id)
    if await session.scalar(q) is not None:
        raise ServiceError(f"A repository named '{name}' already exists in this project")


async def _project_agents(session: AsyncSession, project_id: int) -> list[Agent]:
    return list((await session.scalars(select(Agent).where(Agent.project_id == project_id))).all())


async def create(session: AsyncSession, project_id: int, data: RepositoryCreate) -> Repository:
    url = data.url.strip()
    if not url:
        raise ServiceError("Repository URL is required")
    name = _validate_name((data.name or name_from_url(url)).strip())
    await _check_unique(session, project_id, name)
    repo = Repository(project_id=project_id, name=name, url=url, default_branch=data.default_branch or None, description=data.description)
    session.add(repo)
    await session.flush()
    if data.agent_ids is not None:
        await set_repo_agents(session, repo, data.agent_ids, commit=False)
    await session.commit()
    await publish(session, repo)
    return repo


async def update(session: AsyncSession, repo: Repository, data: RepositoryUpdate) -> Repository:
    fields = data.model_dump(exclude_unset=True)
    agent_ids = fields.pop("agent_ids", None)
    if "name" in fields and fields["name"]:
        fields["name"] = _validate_name(fields["name"].strip())
        await _check_unique(session, repo.project_id, fields["name"], exclude_id=repo.id)
    for key, value in fields.items():
        if key == "default_branch":
            value = value or None
        if value is not None or key == "default_branch":
            setattr(repo, key, value)
    if agent_ids is not None:
        await set_repo_agents(session, repo, agent_ids, commit=False)
    await session.commit()
    await publish(session, repo)
    return repo


async def delete(session: AsyncSession, repo: Repository) -> None:
    project_id, repo_id = repo.project_id, repo.id
    await session.delete(repo)
    await session.commit()
    bus.publish(project_id, "repository.deleted", {"id": repo_id})


async def set_repo_agents(session: AsyncSession, repo: Repository, agent_ids: list[int], *, commit: bool = True) -> None:
    """Grant exactly these agents explicit access (agents with 'all repositories' keep access anyway)."""
    targets = set(agent_ids)
    for agent in await _project_agents(session, repo.project_id):
        has = any(r.id == repo.id for r in agent.repositories)
        if agent.id in targets and not has:
            agent.repositories.append(repo)
        elif agent.id not in targets and has:
            agent.repositories = [r for r in agent.repositories if r.id != repo.id]
    if commit:
        await session.commit()


async def get(session: AsyncSession, repo_id: int, project_id: int | None = None) -> Repository:
    repo = await session.get(Repository, repo_id)
    if repo is None or (project_id is not None and repo.project_id != project_id):
        raise NotFound("Repository not found")
    return repo


async def resolve(session: AsyncSession, project_id: int, ref: str | int) -> Repository:
    repos = await list_for_project(session, project_id)
    for repo in repos:
        if str(repo.id) == str(ref) or repo.name.lower() == str(ref).lower():
            return repo
    raise NotFound(f"No repository '{ref}'. Available: {', '.join(r.name for r in repos) or 'none'}")


async def list_for_project(session: AsyncSession, project_id: int) -> list[Repository]:
    return list((await session.scalars(select(Repository).where(Repository.project_id == project_id).order_by(Repository.name))).all())


def accessible(agent: Agent, project_repos: list[Repository]) -> list[Repository]:
    if agent.all_repos:
        return project_repos
    allowed = {r.id for r in agent.repositories}
    return [r for r in project_repos if r.id in allowed]


async def specs_for_agent(session: AsyncSession, agent: Agent) -> list[RepoSpec]:
    repos = accessible(agent, await list_for_project(session, agent.project_id))
    return [RepoSpec(name=r.name, url=r.url, branch=r.default_branch) for r in repos]


async def to_out(session: AsyncSession, repos: list[Repository]) -> list[RepositoryOut]:
    ids = [r.id for r in repos]
    links: dict[int, list[int]] = {}
    if ids:
        rows = await session.execute(
            select(agent_repositories.c.agent_id, agent_repositories.c.repository_id).where(agent_repositories.c.repository_id.in_(ids))
        )
        for agent_id, repo_id in rows.all():
            links.setdefault(repo_id, []).append(agent_id)
    out = []
    for repo in repos:
        item = RepositoryOut.model_validate(repo)
        item.agent_ids = sorted(links.get(repo.id, []))
        out.append(item)
    return out


async def publish(session: AsyncSession, repo: Repository) -> None:
    (out,) = await to_out(session, [repo])
    bus.publish(repo.project_id, "repository.upserted", out)
