from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..config import get_config
from ..security import SESSION_COOKIE, SESSION_MAX_AGE, make_session_cookie, password_matches, session_cookie_valid

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    password: str


def is_authenticated(request: Request) -> bool:
    if not get_config().password:
        return True
    return session_cookie_valid(request.cookies.get(SESSION_COOKIE))


@router.get("/status")
async def status(request: Request) -> dict[str, bool]:
    return {"password_required": bool(get_config().password), "authenticated": is_authenticated(request)}


@router.post("/login")
async def login(body: LoginIn, response: Response) -> dict[str, bool]:
    expected = get_config().password
    if not expected:
        return {"authenticated": True}
    if not password_matches(body.password, expected):
        raise HTTPException(401, "Wrong password")
    response.set_cookie(SESSION_COOKIE, make_session_cookie(), max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
    return {"authenticated": True}


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE)
    return {"authenticated": False}
