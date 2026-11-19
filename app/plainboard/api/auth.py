from __future__ import annotations

from fastapi import APIRouter, File, Request, Response, UploadFile
from pydantic import BaseModel, Field

from ..models import User
from ..schemas import UserOut
from ..security import SESSION_COOKIE, SESSION_MAX_AGE
from ..services import sessions as session_service
from ..services import users as user_service
from ..services.errors import ServiceError
from .deps import Session, Who
from .guard import secure_cookies

router = APIRouter(prefix="/api/auth", tags=["auth"])


class AuthStatus(BaseModel):
    setup_required: bool  # no user yet: the first admin is created in the browser
    setup_password_required: bool  # ... and that takes PLAINBOARD_PASSWORD
    user: UserOut | None  # who is signed in


class LoginIn(BaseModel):
    username: str
    password: str


class SetupIn(BaseModel):
    username: str
    name: str = ""
    password: str
    setup_password: str | None = None


class MeUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    current_password: str | None = None
    new_password: str | None = None


async def _sign_in(session: Session, request: Request, response: Response, user: User) -> None:
    cookie = await session_service.start(session, user)
    response.set_cookie(SESSION_COOKIE, cookie, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=secure_cookies(request))


@router.get("/status", response_model=AuthStatus)
async def status(request: Request, session: Session) -> AuthStatus:
    who = getattr(request.state, "principal", None)
    return AuthStatus(
        setup_required=not await user_service.count(session),
        setup_password_required=user_service.setup_password_required(),
        user=UserOut.model_validate(who.user) if who and who.user else None,
    )


@router.post("/setup", response_model=UserOut, status_code=201)
async def setup(body: SetupIn, session: Session, request: Request, response: Response) -> UserOut:
    """The first admin, once: an installation without users is not set up yet."""
    user = await user_service.setup(session, body.username, body.name, body.password, body.setup_password)
    await _sign_in(session, request, response, user)
    return UserOut.model_validate(user)


@router.post("/login", response_model=UserOut)
async def login(body: LoginIn, session: Session, request: Request, response: Response) -> UserOut:
    user = await user_service.authenticate(session, body.username, body.password)
    await _sign_in(session, request, response, user)
    return UserOut.model_validate(user)


@router.post("/logout", status_code=204)
async def logout(session: Session, request: Request, response: Response) -> None:
    """Ends this browser's session for good: a copy of its cookie is of no use afterwards."""
    await session_service.end(session, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="lax", secure=secure_cookies(request))


@router.patch("/me", response_model=UserOut)
async def update_me(body: MeUpdate, who: Who, session: Session, request: Request, response: Response) -> UserOut:
    """One's own name and password. A new password signs every other browser out."""
    if who.user is None:
        raise ServiceError("Only a signed-in user has an account.", 403)
    user = await user_service.get(session, who.user.id)
    if body.name is not None:
        user = await user_service.update(session, user, name=body.name)
    if body.new_password is not None:
        user = await user_service.change_own_password(session, user, body.current_password or "", body.new_password)
        await session_service.end(session, request.cookies.get(SESSION_COOKIE))
        await _sign_in(session, request, response, user)
    return UserOut.model_validate(user)


@router.put("/me/avatar", response_model=UserOut)
async def set_avatar(who: Who, session: Session, file: UploadFile = File(...)) -> UserOut:
    """One's own profile picture: a small square image, which the browser makes from what was chosen."""
    if who.user is None:
        raise ServiceError("Only a signed-in user has an account.", 403)
    user = await user_service.get(session, who.user.id)
    data = await file.read(user_service.AVATAR_MAX_BYTES + 1)
    return UserOut.model_validate(await user_service.set_avatar(session, user, data, file.content_type or ""))


@router.delete("/me/avatar", response_model=UserOut)
async def remove_avatar(who: Who, session: Session) -> UserOut:
    if who.user is None:
        raise ServiceError("Only a signed-in user has an account.", 403)
    return UserOut.model_validate(await user_service.set_avatar(session, await user_service.get(session, who.user.id), None))
