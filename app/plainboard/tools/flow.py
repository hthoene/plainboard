"""Tools that take waiting out of the agents' hands: handing a finished branch to PlainBoard, and
seeing where the team's time went."""

from __future__ import annotations

import re
import shlex

from pydantic import BaseModel, Field

from ..models import Assignment, JobStatus, LandingStatus, Repository, Run
from ..orchestration import prompts
from ..orchestration.runner_client import RunnerError
from ..services import flow_check as flow_check_service
from ..services import flow_log
from ..services import landings as landing_service
from ..services import projects as project_service
from ..services import repositories as repo_service
from ..services import settings as settings_service
from ..services import timing as timing_service
from ..services.errors import WaitEnded
from .registry import MANAGER, ToolContext, ToolError, tool

QUICK_CHECK_SECONDS = 600  # a quick check that takes longer is not one: it is stopped and the branch goes on
NOT_AS_PUSHED = 97  # exit code: the clone is not on the commit that is handed over


class MergeWhenGreenArgs(BaseModel):
    repository: str = Field(description="Repository name, as in your workspace")
    branch: str = Field(description="Your pushed branch with the finished change")


class ReturnBranchArgs(BaseModel):
    repository: str
    branch: str
    reason: str = Field(min_length=10, description="What fails and why it is more than a small fix")


class FlowCheckArgs(BaseModel):
    hours: int = Field(default=24, ge=1, le=336)


class TimeReportArgs(BaseModel):
    hours: int = Field(default=24, ge=1, le=336)


async def _repository(ctx: ToolContext, name: str) -> Repository:
    repos = repo_service.accessible(ctx.agent, await repo_service.list_for_project(ctx.session, ctx.project.id))
    repo = next((r for r in repos if r.name.lower() == name.strip().lower()), None)
    if repo is None:
        raise ToolError(f"No repository '{name}'. Yours: {', '.join(r.name for r in repos) or 'none'}")
    return repo


def quick_check_script(folder: str, branch: str, head: str, command: str) -> str:
    """The quick check on exactly ``head``: where the agent has that commit checked out with nothing
    uncommitted (its clone or one of its worktrees: the build cache is warm there), else in a
    worktree that is thrown away afterwards."""
    q = shlex.quote
    return "\n".join(
        [
            flow_check_service.QUICK_CHECK_MARK + folder,  # so its time can be measured (services/flow_check.py)
            f"cd {q(folder)} || exit {NOT_AS_PUSHED}",
            "where=$(git worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p' | while IFS= read -r tree; do",
            f'if [ "$(git -C "$tree" rev-parse HEAD 2>/dev/null)" = "{head}" ] && git -C "$tree" diff --quiet HEAD 2>/dev/null; then printf "%s" "$tree"; break; fi',
            "done)",
            'if [ -n "$where" ]; then',
            f'(\ncd "$where" || exit {NOT_AS_PUSHED}\n{command.strip()}\n)',
            "exit $?",
            "fi",
            f"git cat-file -e {head}^{{commit}} 2>/dev/null || git fetch -q origin {q(branch)} || exit {NOT_AS_PUSHED}",
            f'tmp=$(mktemp -d) && git worktree add -q --detach "$tmp/tree" {head} || exit {NOT_AS_PUSHED}',
            f'(\ncd "$tmp/tree" || exit {NOT_AS_PUSHED}\n{command.strip()}\n)',
            "rc=$?",
            'git worktree remove --force "$tmp/tree" >/dev/null 2>&1; rm -rf "$tmp"',
            "exit $rc",
        ]
    )


async def _quick_check(ctx: ToolContext, repo: Repository, branch: str, head: str) -> str:
    """Run the repository's quick check on the commit that is handed over. A failure comes back in
    this call, while the agent still knows the change; it never reaches a train, where it would cost
    everybody a check run. Where the agent worked does not matter (most use worktrees of their own):
    if its clone happens to be on that commit, the check runs there, with a warm build cache;
    otherwise in a throw-away worktree of exactly that commit."""
    if not re.fullmatch(r"[0-9a-f]{7,64}", head):
        return ""
    job = await ctx.runs.start_job(ctx.session, ctx.project, ctx.agent, quick_check_script(repo.name, branch, head, repo.quick_check_command), ctx.run.assignment_id)
    await ctx.session.commit()  # don't hold a DB transaction while waiting
    try:
        result = await ctx.runs.wait_job(job.id, QUICK_CHECK_SECONDS, ctx.run.run_id)
    except WaitEnded as exc:
        await ctx.runs.stop_job(ctx.session, job, settle=False)
        raise ToolError(f"Not handed over: the quick check was stopped ({exc}). Hand the branch over again afterwards.") from exc
    await ctx.session.refresh(job)
    if job.status == JobStatus.RUNNING or job.exit_code != 0:
        why = "too_slow" if job.status == JobStatus.RUNNING else "not_as_pushed" if job.exit_code == NOT_AS_PUSHED else "failed"
        await flow_log.record(
            ctx.session, ctx.project.id, "landing.quick_check_failed", agent_id=ctx.agent.id, run_id=ctx.run.run_id, assignment_id=ctx.run.assignment_id,
            repository=repo.name, branch=branch, head=head, job_id=job.id, why=why,
        )  # fmt: skip
    if job.status == JobStatus.RUNNING:
        await ctx.runs.stop_job(ctx.session, job, settle=False)
        return f" The quick check of {repo.name} did not end within {QUICK_CHECK_SECONDS // 60} min and was skipped: say so in your report, it is not quick."
    if job.exit_code == NOT_AS_PUSHED:
        raise ToolError(
            f"Not handed over: PlainBoard could not check out the commit you pushed ({head[:10]}) from your clone of {repo.name} to run the quick check on it. "
            f"See that the workspace folder {repo.name} is a working clone that can fetch {branch}, and hand it over again.\n" + (result or "")
        )
    if job.exit_code != 0:
        raise ToolError(
            f"Not handed over: the quick check of {repo.name} failed. Fix all of it, push, and hand the branch over again.\n"
            + (result or f"Job #{job.id} ended with exit code {job.exit_code}. Log: {job.log_path}")
        )
    return " The quick check passed."


@tool(
    "merge_when_green",
    "Hand your pushed, finished branch to PlainBoard: it runs the repository's quick check at once, then lands the branch together with the other finished branches, with one check run for all of them. Then finish: open no pull request, start no checks, wait for none.",
    MergeWhenGreenArgs,
)
async def merge_when_green(ctx: ToolContext, args: MergeWhenGreenArgs) -> str:
    repo = await _repository(ctx, args.repository)
    if not landing_service.can_land(repo):
        raise ToolError(landing_service.UNAVAILABLE.format(name=repo.name))
    branch = args.branch.strip().removeprefix("origin/")
    if not branch or branch == (repo.default_branch or ""):
        raise ToolError("Name the branch with your change, not the base branch.")
    credentials = settings_service.credentials(await settings_service.get_all(ctx.session), project_service.github_token(ctx.project))
    try:
        head = await ctx.runs.runners.of(ctx.project.id).branch_head(credentials, landing_service.remote(repo), branch)
    except RunnerError as exc:
        raise ToolError(f"Could not reach {repo.name}: {str(exc)[:300]}") from exc
    if head is None:
        raise ToolError(f"Branch {branch} is not on the remote of {repo.name}: push it first, then hand it over.")
    task_id = ctx.run.task_id
    if task_id is None and (run := await ctx.session.get(Run, ctx.run.run_id)):
        task_id = run.task_id
    before = await landing_service.find(ctx.session, repo.id, branch)
    if before and before.status == LandingStatus.STUCK and before.failed_sha == head and not ctx.agent.is_manager:
        raise ToolError(f"Branch {branch} failed with exactly this commit and went to the manager. Push a change first, or leave it to the manager.")
    # The manager hands over what others wrote, from a clone that is elsewhere: its call is a decision, not a check.
    quick = await _quick_check(ctx, repo, branch, head) if repo.quick_check_command.strip() and not ctx.agent.is_manager else ""
    landing = await landing_service.hand_over(
        ctx.session, repo, branch, head, agent_id=ctx.agent.id, task_id=task_id, fresh_start=ctx.agent.is_manager, assignment_id=ctx.run.assignment_id
    )
    # Repairing a failed train with a branch of one's own: that branch carries the train's branches.
    repairing = await ctx.session.get(Assignment, ctx.run.assignment_id) if ctx.run.assignment_id else None
    carried = await landing_service.carry(ctx.session, landing, repairing.train_id) if repairing and repairing.train_id else []
    timing_service.forget_cache()
    await flow_log.record(
        ctx.session, ctx.project.id, "landing.handed_over", landing_id=landing.id, agent_id=ctx.agent.id, run_id=ctx.run.run_id, assignment_id=ctx.run.assignment_id,
        task_id=task_id, repository=repo.name, branch=branch, head=head, status=str(landing.status), quick_check=quick.strip(), again=before is not None,
        carries=[c.branch for c in carried], land_first=repo.land_first,
    )  # fmt: skip
    base = repo.default_branch or "the base branch"
    if carried:
        return (
            f"Branch {branch} is with PlainBoard now.{quick} It carries {', '.join(c.branch for c in carried)}: they land with it and are not checked on their own any more. "
            "Your part is done: open no pull request, start no checks and wait for none. Finish and report."
        )
    if repo.land_first and landing.status == LandingStatus.WAITING:
        return (
            f"Branch {branch} is with PlainBoard now.{quick} It is merged into {base} with the other finished branches within minutes; the full checks run on {base} "
            "afterwards, once for all of them. If they fail, PlainBoard has that repaired; you may get it as an assignment. If your branch conflicts you get it back. "
            "Your part is done: open no pull request, start no checks and wait for none. Finish and report."
        )
    if landing.status == LandingStatus.TRAIN:
        return f"Branch {branch} is being checked already. Commits pushed since then are not part of that check: hand the branch over again when it has landed."
    if landing.status == LandingStatus.BACK:
        return f"Branch {branch} is taken as pushed.{quick} It goes on the next train when your repair is finished: finish and report."
    return (
        f"Branch {branch} is with PlainBoard now.{quick} It is checked together with the other finished branches (one check run for all of them) and merged into "
        f"{repo.default_branch or 'the base branch'}. If that run fails, PlainBoard has it repaired; you may get it back as an assignment. "
        "Your part is done: open no pull request, start no checks and wait for none. Finish and report."
    )


@tool(
    "return_branch",
    "Only while you repair branches that failed together: give one that needs more than a small fix back to its author, so the others land without it.",
    ReturnBranchArgs,
)
async def return_branch(ctx: ToolContext, args: ReturnBranchArgs) -> str:
    from ..orchestration import landing as landing_flow  # it imports the run manager, which loads the tools

    repo = await _repository(ctx, args.repository)
    branch = args.branch.strip().removeprefix("origin/")
    landing = await landing_service.find(ctx.session, repo.id, branch)
    assignment = await ctx.session.get(Assignment, ctx.run.assignment_id) if ctx.run.assignment_id else None
    repairing = bool(assignment and assignment.train_id and landing and landing.status == LandingStatus.BACK and landing.train_id == assignment.train_id)
    if not repairing and not (ctx.agent.is_manager and landing and landing.status in (LandingStatus.BACK, LandingStatus.WAITING)):
        raise ToolError(f"{branch} is not one of the branches you are repairing together.")
    assert landing
    instructions = prompts.returned_instructions(branch, ctx.agent.name, args.reason, landing.failures, landing_service.MAX_FAILED_ROUNDS)
    went_back = await landing_flow.give_back(
        ctx.session, ctx.runs, repo, ctx.project, landing, f"was taken off a failed train by {ctx.agent.name}", f"Repair branch {branch}", instructions, counts=False
    )
    timing_service.forget_cache()
    await flow_log.record(ctx.session, ctx.project.id, "landing.returned", landing_id=landing.id, agent_id=ctx.agent.id, run_id=ctx.run.run_id, branch=branch, reason=args.reason, to_author=went_back)
    where = "is back with its author" if went_back else "went to the manager"
    return f"Branch {branch} {where}. The others go on without it: none of them may contain it or need it."


@tool("forget_branch", "Take a branch off PlainBoard's landing list: its work landed another way, or is dropped. For stuck branches that will not be handed over again.", MergeWhenGreenArgs, roles=(MANAGER,))
async def forget_branch(ctx: ToolContext, args: MergeWhenGreenArgs) -> str:
    repo = await _repository(ctx, args.repository)
    branch = args.branch.strip().removeprefix("origin/")
    landing = await landing_service.find(ctx.session, repo.id, branch)
    if landing is None or landing.status in (LandingStatus.MERGED, LandingStatus.CLOSED):
        raise ToolError(f"{branch} is not on PlainBoard's list.")
    if landing.status == LandingStatus.TRAIN:
        raise ToolError(f"{branch} is being checked right now: wait for the result.")
    was = str(landing.status)
    landing.status, landing.train_id, landing.group_no = LandingStatus.CLOSED, None, None
    await ctx.session.commit()
    timing_service.forget_cache()
    await flow_log.record(ctx.session, ctx.project.id, "landing.forgotten", landing_id=landing.id, agent_id=ctx.agent.id, branch=branch, was=was)
    return f"Branch {branch} is off the list (it was {was}). PlainBoard does nothing more with it; the branch itself is untouched."


@tool("flow_check", "PlainBoard's own checks of what slows the team down, measured, heaviest first, each with what usually helps.", FlowCheckArgs, roles=(MANAGER,))
async def flow_check(ctx: ToolContext, args: FlowCheckArgs) -> str:
    return flow_check_service.text(await flow_check_service.findings(ctx.session, ctx.project.id, args.hours), args.hours)


@tool("time_report", "Where the team's time went: work against waiting, the slowest commands, check runs for landing, repeated runs.", TimeReportArgs, roles=(MANAGER,))
async def time_report(ctx: ToolContext, args: TimeReportArgs) -> str:
    overview = await timing_service.overview(ctx.session, ctx.project.id, args.hours)
    return timing_service.report_text(overview, await timing_service.running_trains(ctx.session, ctx.project.id))
