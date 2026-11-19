"""Disk space: what may be deleted and deleting it, for the manager (services/storage.py)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..orchestration.runner_client import RunnerError
from ..services import settings as settings_service
from ..services import storage
from .registry import MANAGER, ToolContext, ToolError, tool


class DeleteArgs(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=50, description="As list_cleanup shows them")
    force: bool = Field(default=False, description="Also busy and unsure ones; never kept ones")


@tool("list_cleanup", "Disk use and what of your project can be deleted, with sizes and safety.", roles=(MANAGER,))
async def list_cleanup(ctx: ToolContext, _args) -> str:
    settings = await settings_service.get_all(ctx.session)
    try:
        found = await storage.listing(ctx.session, ctx.runs.runner)
    except RunnerError as exc:
        raise ToolError(str(exc)) from exc
    return storage.tool_listing(found, ctx.project, storage.TREND.forecast(), settings)


@tool("delete_cleanup", "Delete your project's cleanup candidates from list_cleanup.", DeleteArgs, roles=(MANAGER,))
async def delete_cleanup(ctx: ToolContext, args: DeleteArgs) -> str:
    try:
        result, refused = await storage.delete(ctx.session, ctx.runs.runner, ctx.project, ctx.agent, args.paths, args.force)
    except RunnerError as exc:
        raise ToolError(str(exc)) from exc
    out = storage.delete_text(result, refused)
    if result.freed and any(i.what == "build" for i in result.items) and args.force:
        out += "\nBusy members' build outputs that went are built again with their next build, which takes longer."
    return out
