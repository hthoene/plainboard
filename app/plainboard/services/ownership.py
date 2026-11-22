"""Handing a project to another owner (``Project.owner_id``): whose limits hold for it (services/permissions.py).

An admin may do it for any project (also one of nobody); its owner may hand it to another member.
A runner the new owner may not use refuses it, for admins too: moving the project to another runner
is a step of its own. What the project or one of its agents chose and the new owner may not use
chooses automatically again, and the project's manager is told once.
"""

from __future__ import annotations

from sqlalchemy import select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, Agent, InboxItem, Project, ProjectMember, ProjectRole, Runner, User
from ..schemas import OwnerChangeOut
from . import permissions
from . import users as user_service
from .errors import ServiceError


async def _allowed(session: AsyncSession, project: Project, user: User | None) -> set[int]:
    allowed = await permissions.usable(session, user)
    return allowed & set(project.account_ids) if project.account_ids else allowed


async def _parties(session: AsyncSession, project: Project, actor: User | None, user_id: int) -> tuple[User | None, User]:
    if actor is None or (not actor.is_admin and project.owner_id != actor.id):
        raise ServiceError("Only an admin or the project's owner can hand it to someone else.", 403)
    new = await user_service.get(session, user_id)
    if new.id == project.owner_id:
        raise ServiceError(f"{new.name} already owns this project.")
    if not actor.is_admin and await session.scalar(select(ProjectMember.id).where(ProjectMember.project_id == project.id, ProjectMember.user_id == new.id)) is None:
        raise ServiceError("Hand the project only to one of its members.")
    old = await session.get(User, project.owner_id) if project.owner_id else None
    return old, new


def _limited(user: User | None) -> User | None:
    return user if permissions.limited(user) else None


async def preview(session: AsyncSession, project: Project, actor: User | None, user_id: int) -> OwnerChangeOut:
    """What changes for the project if ``user_id`` owned it."""
    old, new = await _parties(session, project, actor, user_id)
    now, then = await _allowed(session, project, old), await _allowed(session, project, new)
    lost = sorted(now - then)
    labels = dict((await session.execute(select(Account.id, Account.label).where(Account.id.in_(lost)))).all()) if lost else {}
    runner = await session.get(Runner, project.runner_id) if project.runner_id else None
    return OwnerChangeOut(
        user_id=new.id,
        name=new.name,
        runner=runner.name if runner else "",
        runner_allowed=permissions.may_run_on(_limited(new), project.runner_id),
        lost_accounts=[labels[i] for i in lost if i in labels],
        unpinned=await _unpinned(session, project, then),
        loses_shared=permissions.may_use_shared(_limited(old)) and not permissions.may_use_shared(_limited(new)),
        max_parallel_before=_limited(old).max_parallel if _limited(old) else None,
        max_parallel_after=_limited(new).max_parallel if _limited(new) else None,
    )


async def _unpinned(session: AsyncSession, project: Project, allowed: set[int]) -> list[str]:
    """What chose a subscription outside ``allowed``: "the project", and agents by name."""
    names = ["the project"] if project.account_id is not None and project.account_id not in allowed else []
    rows = await session.scalars(
        select(Agent.name).where(Agent.project_id == project.id, Agent.account_id.is_not(None), Agent.account_id.not_in(allowed or [-1])).order_by(Agent.id)
    )
    return names + list(rows)


async def _owner_role(session: AsyncSession, project_id: int, user_id: int) -> None:
    member = await session.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id))
    if member is None:
        session.add(ProjectMember(project_id=project_id, user_id=user_id, role=ProjectRole.OWNER))
    else:
        member.role = ProjectRole.OWNER


async def change(session: AsyncSession, project: Project, actor: User | None, user_id: int) -> None:
    """``user_id`` owns the project from now on and has the role owner in it; the one before keeps that role."""
    old, new = await _parties(session, project, actor, user_id)
    if not permissions.may_run_on(_limited(new), project.runner_id):
        runner = await session.get(Runner, project.runner_id) if project.runner_id else None
        where = f"the runner '{runner.name}'" if runner else "the built-in runner"
        raise ServiceError(f"{new.name} may not run projects on {where}. Move the project to a runner they may use first, then hand it over.")
    allowed = await _allowed(session, project, new)
    unpinned = await _unpinned(session, project, allowed)
    project.owner_id = new.id
    await _owner_role(session, project.id, new.id)
    if old is not None:
        await _owner_role(session, project.id, old.id)
    if project.account_id is not None and project.account_id not in allowed:
        project.account_id = None
    await session.execute(
        sql_update(Agent).where(Agent.project_id == project.id, Agent.account_id.is_not(None), Agent.account_id.not_in(allowed or [-1])).values(account_id=None)
    )
    manager = await session.scalar(select(Agent).where(Agent.project_id == project.id, Agent.is_manager.is_(True)))
    if unpinned and manager is not None:
        session.add(
            InboxItem(
                agent_id=manager.id,
                text=f"This project now belongs to {new.name}, who may not use the subscription chosen for {', '.join(unpinned)}: it now chooses a subscription automatically.",
            )
        )
    await session.commit()
