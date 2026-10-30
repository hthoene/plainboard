from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Attachment, Project
from ..schemas import AgentCreate, ProjectCreate, ProjectOut, ProjectUpdate
from ..security import decrypt, encrypt
from . import agents as agent_service
from . import repositories as repo_service
from .errors import NotFound, ServiceError
from .util import slugify

MANAGER_DESCRIPTION = (
    "Coordinates the project: turns tickets into clear assignments for the team, "
    "manages agents, notes, schedules and integrations, and reports to the user."
)


async def _unique_slug(session: AsyncSession, name: str) -> str:
    base = slugify(name, "project")
    slug, n = base, 2
    while await session.scalar(select(Project.id).where(Project.slug == slug)) is not None:
        slug, n = f"{base}-{n}", n + 1
    return slug


async def create(session: AsyncSession, data: ProjectCreate) -> Project:
    project = Project(
        name=data.name,
        slug=await _unique_slug(session, data.name),
        description=data.description,
        workspace_path=normalize_workspace_path(data.workspace_path),
        github_token_enc=encrypt(data.github_token),
    )
    session.add(project)
    await session.commit()
    for repo in data.repositories:
        await repo_service.create(session, project.id, repo)
    await agent_service.create(
        session,
        project.id,
        AgentCreate(name="Manager", role="Project Manager", description=MANAGER_DESCRIPTION, toolset="board"),
        is_manager=True,
    )
    return project


async def update(session: AsyncSession, project: Project, data: ProjectUpdate) -> Project:
    fields = data.model_dump(exclude_unset=True)
    if "github_token" in fields:
        project.github_token_enc = encrypt(fields.pop("github_token"))
    if "workspace_path" in fields:
        fields["workspace_path"] = normalize_workspace_path(fields["workspace_path"])
    for key, value in fields.items():
        setattr(project, key, value)
    await session.commit()
    bus.publish(project.id, "project.updated", to_out(project))
    return project


async def delete(session: AsyncSession, project: Project) -> None:
    from .board import storage_path

    attachments = (await session.scalars(select(Attachment).where(Attachment.project_id == project.id))).all()
    files = [storage_path(a) for a in attachments]
    await session.delete(project)
    await session.commit()
    for path in files:
        path.unlink(missing_ok=True)


async def get(session: AsyncSession, project_id: int) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise NotFound("Project not found")
    return project


async def list_all(session: AsyncSession) -> list[Project]:
    return list((await session.scalars(select(Project).order_by(Project.id))).all())


def normalize_workspace_path(value: str | None) -> str | None:
    """Relative path below the workspaces root; '' or None = per-agent directories."""
    if not value:
        return None
    parts = [p for p in value.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ServiceError("Workspace path must stay inside the workspaces folder")
    return "/".join(parts) or None


def github_token(project: Project) -> str | None:
    return decrypt(project.github_token_enc)


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def create_public_api_key(session: AsyncSession, project: Project) -> str:
    """A new key replaces the old one. Only its hash is stored, so it can be shown just once."""
    key = f"pb_{secrets.token_urlsafe(32)}"
    project.public_api_key_hash = _key_hash(key)
    await session.commit()
    bus.publish(project.id, "project.updated", to_out(project))
    return key


async def revoke_public_api_key(session: AsyncSession, project: Project) -> None:
    project.public_api_key_hash = None
    await session.commit()
    bus.publish(project.id, "project.updated", to_out(project))


async def by_public_api_key(session: AsyncSession, key: str) -> Project | None:
    return await session.scalar(select(Project).where(Project.public_api_key_hash == _key_hash(key)))


def to_out(project: Project) -> ProjectOut:
    out = ProjectOut.model_validate(project)
    out.has_github_token = bool(project.github_token_enc)
    out.has_public_api_key = bool(project.public_api_key_hash)
    return out
