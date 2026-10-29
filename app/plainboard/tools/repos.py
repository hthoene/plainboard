"""Repository tools: project repositories and which agents may work on them."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import Agent
from ..schemas import AgentUpdate, RepositoryCreate, RepositoryUpdate
from ..services import agents as agent_service
from ..services import repositories as repo_service
from ..services.util import truncate
from .registry import MANAGER, ToolContext, tool

ALL = "*"


class AddRepoArgs(BaseModel):
    url: str = Field(description="Clone URL, e.g. https://github.com/org/repo.git")
    name: str | None = Field(default=None, description="Folder name in the workspace; default from URL")
    branch: str | None = Field(default=None, description="Branch to check out; default: the repo's default")
    description: str = Field(default="", description="What the repository contains (helps routing work)")
    agents: list[str] | None = Field(default=None, description="Grant access to these agents (agents with access to all repos get it anyway)")


class UpdateRepoArgs(BaseModel):
    repository: str = Field(description="Repository name or id")
    url: str | None = None
    name: str | None = None
    branch: str | None = None
    description: str | None = None


class RepoRef(BaseModel):
    repository: str


class AgentReposArgs(BaseModel):
    agent: str
    repositories: list[str] = Field(description="Repository names the agent may use; ['*'] = all (including future ones), [] = none")


async def apply_repo_access(ctx: ToolContext, agent: Agent, refs: list[str]) -> str:
    if ALL in refs:
        await agent_service.update(ctx.session, agent, AgentUpdate(all_repos=True, repository_ids=[]))
        return "all repositories"
    repos = [await repo_service.resolve(ctx.session, ctx.project.id, r) for r in refs]
    await agent_service.update(ctx.session, agent, AgentUpdate(all_repos=False, repository_ids=[r.id for r in repos]))
    return ", ".join(r.name for r in repos) or "no repositories"


@tool("list_repositories", "Project repositories and which agents can access them.")
async def list_repositories(ctx: ToolContext, _args) -> str:
    repos = await repo_service.list_for_project(ctx.session, ctx.project.id)
    if not repos:
        return "No repositories."
    agents = [a for a in await agent_service.list_for_project(ctx.session, ctx.project.id) if not a.is_manager]
    lines = []
    for repo in repos:
        who = [a.slug for a in agents if repo in repo_service.accessible(a, repos)]
        branch = f" @{repo.default_branch}" if repo.default_branch else ""
        about = f" — {truncate(repo.description, 100)}" if repo.description else ""
        lines.append(f"{repo.name}{branch}: {repo.url}{about} | agents: {', '.join(who) or 'none'}")
    return "\n".join(lines)


@tool("add_repository", "Add a git repository to the project; it is cloned into the workspace of agents with access.", AddRepoArgs, roles=(MANAGER,))
async def add_repository(ctx: ToolContext, args: AddRepoArgs) -> str:
    agent_ids = [(await agent_service.resolve(ctx.session, ctx.project.id, a)).id for a in args.agents] if args.agents else None
    repo = await repo_service.create(
        ctx.session,
        ctx.project.id,
        RepositoryCreate(url=args.url, name=args.name, default_branch=args.branch, description=args.description, agent_ids=agent_ids),
    )
    return f"Repository {repo.name} added."


@tool("update_repository", "Change a repository's URL, folder name, branch or description.", UpdateRepoArgs, roles=(MANAGER,))
async def update_repository(ctx: ToolContext, args: UpdateRepoArgs) -> str:
    repo = await repo_service.resolve(ctx.session, ctx.project.id, args.repository)
    fields = args.model_dump(exclude_unset=True, exclude={"repository", "branch"})
    if "branch" in args.model_fields_set:
        fields["default_branch"] = args.branch
    repo = await repo_service.update(ctx.session, repo, RepositoryUpdate(**fields))
    return f"Repository {repo.name} updated (existing clones are not changed)."


@tool("remove_repository", "Remove a repository from the project (existing clones stay on disk).", RepoRef, roles=(MANAGER,))
async def remove_repository(ctx: ToolContext, args: RepoRef) -> str:
    repo = await repo_service.resolve(ctx.session, ctx.project.id, args.repository)
    await repo_service.delete(ctx.session, repo)
    return f"Repository {repo.name} removed."


@tool("set_agent_repositories", "Define which repositories an agent may work on.", AgentReposArgs, roles=(MANAGER,))
async def set_agent_repositories(ctx: ToolContext, args: AgentReposArgs) -> str:
    agent = await agent_service.resolve(ctx.session, ctx.project.id, args.agent)
    granted = await apply_repo_access(ctx, agent, args.repositories)
    return f"{agent.slug} now has access to: {granted}."
