"""Read-only API for customer-facing pages, e.g. a roadmap on a website. Authenticated by a project key."""

from __future__ import annotations

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse

from ..schemas import PublicBoard
from ..services import board as board_service
from ..services import api_keys as api_key_service
from .deps import Session

router = APIRouter(prefix="/api/public", tags=["public"])

# Callable from any website: the key only ever gives access to what customers may see anyway.
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Authorization", "Access-Control-Allow-Methods": "GET"}


@router.options("/tasks")
async def preflight() -> Response:
    return Response(status_code=204, headers={**CORS, "Access-Control-Max-Age": "86400"})


@router.get("/tasks", response_model=PublicBoard)
async def public_tasks(
    session: Session,
    response: Response,
    authorization: str | None = Header(default=None),
    lang: str | None = Query(default=None, max_length=20, description="Customer language, e.g. de; missing texts come in the default language"),
):
    key = (authorization or "").removeprefix("Bearer ").strip()
    project = await api_key_service.project_for(session, key) if key else None
    if project is None:
        return JSONResponse({"detail": "Invalid API key"}, status_code=401, headers=CORS)
    response.headers.update({**CORS, "Cache-Control": "public, max-age=60"})
    return await board_service.public_board(session, project, lang)
