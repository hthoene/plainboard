"""Repository tools: project repositories and which agents may work on them."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import Agent
from ..schemas import AgentUpdate, RepositoryCreate, RepositoryUpdate
from ..services import agents as agent_service
from ..services import promotions as promotion_service
from ..services import repositories as repo_service
from ..services.util import truncate
from ..orchestration.runner_client import RunnerError
from .registry import MANAGER, ToolContext, ToolError, tool

ALL = "*"


class AddRepoArgs(BaseModel):
    url: str = Field(description="Clone URL, e.g. https://github.com/org/repo.git")
    name: str | None = Field(default=None, description="Folder name in the workspace; default from URL")
    branch: str | None = Field(default=None, description="Branch to check out; default: the repo's default")
    description: str = Field(default="", description="What the repository contains (helps routing work)")
    check_command: str = Field(default="", description="Command that decides whether finished branches may land; run once on all of them merged. Empty: on GitHub, a pull request's checks")
    quick_check_command: str = Field(default="", description="Fast checks (format, lint; a minute or two), run on each branch when it is handed over")
    land_first: bool = Field(default=False, description="Merge finished branches at once and run the checks on the base branch afterwards (a development branch); false: nothing lands before its checks passed")
    agents: list[str] | None = Field(default=None, description="Grant access to these agents (agents with access to all repos get it anyway)")


class UpdateRepoArgs(BaseModel):
    repository: str = Field(description="Repository name or id")
    url: str | None = None
    name: str | None = None
    branch: str | None = None
    description: str | None = None
    check_command: str | None = Field(default=None, description="Decides whether finished branches may land; '' = on GitHub, a pull request's checks")
    quick_check_command: str | None = Field(default=None, description="Fast checks (format, lint; a minute or two), run on each branch when it is handed over; '' = none")
    land_first: bool | None = Field(default=None, description="true: finished branches are merged at once, the checks run on the base branch afterwards and a failure is repaired there. false: nothing lands before its checks passed")
    release_branch: str | None = Field(default=None, max_length=200, description="Branch the base branch is promoted to on request (promote_branch), e.g. main; '' = none")
    checks_baseline_minutes: float | None = Field(default=None, ge=0, description="After a speed-up of the checks: what a check run takes now (the member's verdict)")
    checks_verdict: str | None = Field(default=None, max_length=300, description="e.g. 'sped up to 6 min' or 'near the limit: 14 min is what it takes'")


class RepoRef(BaseModel):
    repository: str


class PromoteArgs(BaseModel):
    repository: str
    tag: str = Field(default="", max_length=100, description="Optional tag/version name set on the promoted commit")


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
        quick = f" | quick check: {truncate(repo.quick_check_command, 80)}" if repo.quick_check_command.strip() else ""
        landing = " | lands first, checked after" if repo.land_first else " | lands when its checks passed"
        verdict = f" | checks: {truncate(repo.checks_verdict, 80)}" if repo.checks_verdict else ""
        release = f" | promoted to {repo.release_branch} on request" if repo.release_branch else ""
        lines.append(f"{repo.name}{branch}: {repo.url}{about} | agents: {', '.join(who) or 'none'}{quick}{landing}{release}{verdict}")
    return "\n".join(lines)


@tool("add_repository", "Add a git repository to the project; it is cloned into the workspace of agents with access.", AddRepoArgs, roles=(MANAGER,))
async def add_repository(ctx: ToolContext, args: AddRepoArgs) -> str:
    agent_ids = [(await agent_service.resolve(ctx.session, ctx.project.id, a)).id for a in args.agents] if args.agents else None
    repo = await repo_service.create(
        ctx.session,
        ctx.project.id,
        RepositoryCreate(url=args.url, name=args.name, default_branch=args.branch, description=args.description, check_command=args.check_command, quick_check_command=args.quick_check_command, land_first=args.land_first, agent_ids=agent_ids),
    )
    return f"Repository {repo.name} added."


@tool("update_repository", "Change a repository's URL, folder name, branch, description, check commands, how its branches land or its release branch.", UpdateRepoArgs, roles=(MANAGER,))
async def update_repository(ctx: ToolContext, args: UpdateRepoArgs) -> str:
    repo = await repo_service.resolve(ctx.session, ctx.project.id, args.repository)
    fields = args.model_dump(exclude_unset=True, exclude={"repository", "branch", "checks_baseline_minutes"})
    if args.checks_baseline_minutes is not None:
        fields["checks_baseline_seconds"] = round(args.checks_baseline_minutes * 60)
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


TICKETS_SHOWN = 40


@tool("promotion_preview", "What promoting a repository's base branch to its release branch would bring now: commits and landed branches with their tickets.", RepoRef, roles=(MANAGER,))
async def promotion_preview(ctx: ToolContext, args: RepoRef) -> str:
    repo = await repo_service.resolve(ctx.session, ctx.project.id, args.repository)
    try:
        p = await promotion_service.preview(ctx.session, ctx.runs, repo)
    except RunnerError as exc:
        raise ToolError(f"{repo.name} could not be read: {exc}") from exc
    if p.total == 0:
        return f"Nothing to promote: {p.release} has {p.base} at {p.sha[:12]} already."
    how = "fast-forward" if p.fast_forward else f"merge commit ({p.release} has commits of its own)"
    lines = [f"{p.base} at {p.sha[:12]} → {p.release}: {p.total} commits, {how}; " + ("this commit passed its checks already." if p.checked else "one check run before the push.")]
    if p.running:
        lines.append(f"A promotion is on its way already ({p.running.sha[:12]}).")
    lines += [f"- {'#' + str(t.task_number) if t.task_number else t.branch}: {truncate(t.title, 90)}" for t in p.tickets[:TICKETS_SHOWN]]
    if len(p.tickets) > TICKETS_SHOWN:
        lines.append(f"... and {len(p.tickets) - TICKETS_SHOWN} more branches")
    if not p.tickets:
        lines += [f"- {c.sha[:8]} {truncate(c.message, 90)}" for c in p.commits[:15]]
    return "\n".join(lines)


@tool(
    "promote_branch",
    "Promote a repository's base branch to its release branch (e.g. dev → main): its head now is checked once, then pushed (or a pull request if the branch is protected). Only when the user asks or the agreed workflow says so. The result comes as an update.",
    PromoteArgs,
    roles=(MANAGER,),
)
async def promote_branch(ctx: ToolContext, args: PromoteArgs) -> str:
    repo = await repo_service.resolve(ctx.session, ctx.project.id, args.repository)
    try:
        p = await promotion_service.start(ctx.session, ctx.runs, repo, by=ctx.agent.slug, tag=args.tag)
    except RunnerError as exc:
        raise ToolError(f"{repo.name} could not be read: {exc}") from exc
    check = "no check run needed" if p.mode == promotion_service.NONE else "its check runs now"
    return f"Promotion of {p.base} at {p.sha[:12]} to {p.release} started ({p.commits} commits, {len(p.landing_ids or [])} branches; {check}). You hear the result; nothing to wait for."
