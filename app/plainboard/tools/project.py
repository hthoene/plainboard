from __future__ import annotations

from pydantic import BaseModel

from ..schemas import ProjectUpdate
from ..services import projects as project_service
from ..services import repositories as repo_service
from .registry import MANAGER, ToolContext, tool


class UpdateProjectArgs(BaseModel):
    name: str | None = None
    description: str | None = None
    workspace_path: str | None = None
    auto_wake_manager: bool | None = None


@tool("get_project", "Project settings (name, description, workspace, repositories).", roles=(MANAGER,))
async def get_project(ctx: ToolContext, _args) -> str:
    p = ctx.project
    workspace = f"shared folder '{p.workspace_path}'" if p.workspace_path else "one directory per agent"
    repos = ", ".join(r.name for r in await repo_service.list_for_project(ctx.session, p.id)) or "-"
    return (
        f"Name: {p.name}\nRepositories: {repos}\nWorkspace: {workspace}\n"
        f"Auto-wake manager: {p.auto_wake_manager}\nDescription:\n{p.description or '-'}"
    )


@tool("update_project", "Update project name, description, shared workspace folder or auto-wake.", UpdateProjectArgs, roles=(MANAGER,))
async def update_project(ctx: ToolContext, args: UpdateProjectArgs) -> str:
    await project_service.update(ctx.session, ctx.project, ProjectUpdate(**args.model_dump(exclude_unset=True)))
    return "Project updated."
