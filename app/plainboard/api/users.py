from __future__ import annotations

from fastapi import APIRouter, File, Response, UploadFile

from ..schemas import MemberIn, MemberOut, OwnerChangeOut, MentionsOut, MentionsRead, PersonOut, UserCreate, UserOut, UserUpdate
from ..services import mentions as mention_service
from ..services import ownership
from ..services import permissions
from ..services import projects as project_service
from ..services import users as user_service
from ..services.errors import NotFound
from .deps import Runs, Session, Who

router = APIRouter(prefix="/api", tags=["users"])


@router.get("/users", response_model=list[UserOut])
async def list_users(session: Session) -> list[UserOut]:
    return [UserOut.model_validate(u) for u in await user_service.list_all(session)]


@router.get("/users/directory", response_model=list[PersonOut])
async def directory(session: Session) -> list[PersonOut]:
    """Names only, for everyone signed in: whom a project can be shared with."""
    return [PersonOut.model_validate(u) for u in await user_service.list_all(session) if u.enabled]


@router.get("/avatars/{user_id}")
async def avatar(user_id: int, session: Session) -> Response:
    """A user's profile picture. Its address carries the version, so it may be kept for good."""
    found = await user_service.avatar(session, user_id)
    if found is None:
        raise NotFound("No picture")
    data, content_type = found
    return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(body: UserCreate, session: Session) -> UserOut:
    user = await user_service.create(session, body.username, body.name, body.password, is_admin=body.is_admin, may_create_projects=body.may_create_projects)
    return UserOut.model_validate(user)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(user_id: int, body: UserUpdate, session: Session, runs: Runs) -> UserOut:
    user = await user_service.get(session, user_id)
    fields = body.model_dump(exclude_unset=True)
    limits = {key: fields.pop(key) for key in permissions.FIELDS if key in fields}
    out = UserOut.model_validate(await user_service.update(session, user, **fields, limits=limits))
    runs.recheck()  # another limit of members at work: waiting turns may start now
    return out


@router.put("/users/{user_id}/avatar", response_model=UserOut)
async def set_user_avatar(user_id: int, session: Session, file: UploadFile = File(...)) -> UserOut:
    """Another person's profile picture, as for one's own (``PUT /api/auth/me/avatar``). Admins only."""
    user = await user_service.get(session, user_id)
    data = await file.read(user_service.AVATAR_MAX_BYTES + 1)
    return UserOut.model_validate(await user_service.set_avatar(session, user, data, file.content_type or ""))


@router.delete("/users/{user_id}/avatar", response_model=UserOut)
async def remove_user_avatar(user_id: int, session: Session) -> UserOut:
    return UserOut.model_validate(await user_service.set_avatar(session, await user_service.get(session, user_id), None))


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: int, session: Session) -> None:
    await user_service.delete(session, await user_service.get(session, user_id))


@router.get("/projects/{project_id}/members", response_model=list[MemberOut])
async def list_members(project_id: int, session: Session) -> list[MemberOut]:
    project = await project_service.get(session, project_id)
    return [
        MemberOut(user_id=u.id, username=u.username, name=u.name, role=role, project_owner=u.id == project.owner_id)
        for u, role in await user_service.members(session, project_id)
    ]


@router.get("/projects/{project_id}/owner/{user_id}", response_model=OwnerChangeOut)
async def owner_preview(project_id: int, user_id: int, session: Session, who: Who) -> OwnerChangeOut:
    """What changes for the project if this person owned it."""
    return await ownership.preview(session, await project_service.get(session, project_id), who.user, user_id)


@router.put("/projects/{project_id}/owner/{user_id}", response_model=list[MemberOut])
async def change_owner(project_id: int, user_id: int, session: Session, who: Who, runs: Runs) -> list[MemberOut]:
    await ownership.change(session, await project_service.get(session, project_id), who.user, user_id)
    runs.recheck()  # another owner's limit of members at work
    return await list_members(project_id, session)


@router.put("/projects/{project_id}/members/{user_id}", response_model=list[MemberOut])
async def set_member(project_id: int, user_id: int, body: MemberIn, session: Session) -> list[MemberOut]:
    await project_service.get(session, project_id)
    await user_service.set_member(session, project_id, user_id, body.role)
    return await list_members(project_id, session)


@router.delete("/projects/{project_id}/members/{user_id}", response_model=list[MemberOut])
async def remove_member(project_id: int, user_id: int, session: Session) -> list[MemberOut]:
    await user_service.remove_member(session, project_id, user_id)
    return await list_members(project_id, session)


@router.get("/projects/{project_id}/people", response_model=list[PersonOut])
async def project_people(project_id: int, session: Session) -> list[PersonOut]:
    """Who can be mentioned with @username in the project: everyone with access to it."""
    await project_service.get(session, project_id)
    return [PersonOut.model_validate(u) for u in await mention_service.people(session, project_id)]


@router.get("/mentions", response_model=MentionsOut)
async def my_mentions(session: Session, who: Who) -> MentionsOut:
    return await mention_service.for_user(session, who.user)


@router.post("/mentions/read", status_code=204)
async def read_mentions(body: MentionsRead, session: Session, who: Who) -> None:
    await mention_service.mark_read(session, who.user, body.ids)
