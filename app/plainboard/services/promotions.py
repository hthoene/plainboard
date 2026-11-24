"""Promotion (#129): the base branch (e.g. dev) brought to the release branch (e.g. main) when asked.

Some teams land on a development branch, test or collect there, and release to main now and then. A
repository with a ``release_branch`` can be promoted: by the manager (``promote_branch``) or a person
(the flow view). The preview says what would go: commits and the branches (with their tickets) that
landed since the last promotion. A promotion takes the base branch's head at that moment as a fixed
commit, runs the repository's check command once on exactly that commit (none if it passed before: a
train or an earlier promotion checked it), and then the landing watcher brings it to the release branch:
fast-forward, else a merge commit; a push the host refuses (a protected branch) becomes a pull request.
It works in a clone of its own, so landings on the base branch go on meanwhile. Failure: nothing is
pushed and the manager hears why.
"""

from __future__ import annotations

import secrets

from plainboard_protocol import PromotePlan, PromoteSpec, TrainCheckSpec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Landing, LandingStatus, Project, Promotion, PromotionStatus, Repository, Task, Train, TrainStatus, utcnow
from ..schemas import PromotionCommit, PromotionOut, PromotionPreview, PromotionTicket
from . import flow_log
from . import landings as landing_service
from . import permissions
from .errors import ServiceError

COMMAND, GITHUB, NONE = "command", "github", "none"
LANDED_LOOKED_AT = 500  # the latest branches that landed in the repository, matched against the commits


def key(repo: Repository) -> str:
    """Names the runner's clone for promotions: not the train clone, so trains go on meanwhile."""
    return f"{landing_service.train_key(repo)}-release"


def _runner(runs, project_id: int):  # noqa: ANN001
    return runs.runners.of(project_id)


async def running(session: AsyncSession, repository_id: int) -> Promotion | None:
    return await session.scalar(select(Promotion).where(Promotion.repository_id == repository_id, Promotion.status == PromotionStatus.CHECKING).limit(1))


async def last_done(session: AsyncSession, repository_id: int) -> Promotion | None:
    q = select(Promotion).where(Promotion.repository_id == repository_id, Promotion.status.in_((PromotionStatus.PROMOTED, PromotionStatus.PULL_REQUEST)))
    return await session.scalar(q.order_by(Promotion.id.desc()).limit(1))


async def checked(session: AsyncSession, repository_id: int, sha: str) -> bool:
    """Whether exactly this commit passed its checks already: a train that landed it (or a look at the
    base branch that found it green), or an earlier promotion that ran its check."""
    if await session.scalar(select(Train.id).where(Train.repository_id == repository_id, Train.head_sha == sha, Train.status == TrainStatus.LANDED).limit(1)):
        return True
    q = select(Promotion.id).where(Promotion.repository_id == repository_id, Promotion.sha == sha, Promotion.mode != NONE, Promotion.status.in_((PromotionStatus.PROMOTED, PromotionStatus.PULL_REQUEST)))
    return bool(await session.scalar(q.limit(1)))


async def _plan(session: AsyncSession, runs, repo: Repository, *, sha: str | None = None, checkout: bool = False) -> PromotePlan:  # noqa: ANN001
    if not repo.release_branch.strip():
        raise ServiceError(f"{repo.name} has no release branch: set one in its settings (or update_repository release_branch) first")
    project = await session.get(Project, repo.project_id)
    credentials = await permissions.credentials(session, project)
    spec = PromoteSpec(credentials=credentials, key=key(repo), remote=landing_service.remote(repo), base=repo.default_branch or None, release=repo.release_branch.strip(), sha=sha, checkout=checkout)
    await session.commit()  # nothing of the database is held while the runner fetches
    return await _runner(runs, repo.project_id).promotion_plan(spec)


async def landed_since(session: AsyncSession, repo: Repository, plan: PromotePlan) -> list[Landing]:
    """The branches that landed on the base branch and are not on the release branch yet: by their commit
    (the branch's head, or the train's that brought it), else, for a squash, by time since the last
    promotion. Branches of an earlier promotion are left out."""
    shas = set(plan.first_parent) | {c.sha for c in plan.commits}
    rows = (await session.execute(
        select(Landing, Train.head_sha).outerjoin(Train, Train.id == Landing.train_id)
        .where(Landing.repository_id == repo.id, Landing.status == LandingStatus.MERGED)
        .order_by(Landing.merged_at.desc(), Landing.id.desc()).limit(LANDED_LOOKED_AT)
    )).all()  # fmt: skip
    last = await last_done(session, repo.id)
    since = last.created_at if last else None
    done: set[int] = set()
    for ids in await session.scalars(select(Promotion.landing_ids).where(Promotion.repository_id == repo.id, Promotion.status.in_((PromotionStatus.PROMOTED, PromotionStatus.PULL_REQUEST)))):
        done.update(ids or [])
    out = []
    for landing, train_head in rows:
        if landing.id in done:
            continue
        by_commit = landing.head_sha in shas or (train_head or "") in shas
        by_time = plan.release_sha is None or (since is not None and landing.merged_at is not None and landing.merged_at > since)
        if by_commit or by_time:
            out.append(landing)
    return list(reversed(out))


async def _tickets(session: AsyncSession, landings: list[Landing]) -> list[PromotionTicket]:
    tasks = {t.id: t for t in await session.scalars(select(Task).where(Task.id.in_({x.task_id for x in landings if x.task_id})))} if landings else {}
    out = []
    for landing in landings:
        task = tasks.get(landing.task_id or 0)
        out.append(PromotionTicket(task_id=task.id if task else None, task_number=task.number if task else None, title=task.title if task else landing.title, branch=landing.branch))
    return out


def out(promotion: Promotion) -> PromotionOut:
    return PromotionOut(
        id=promotion.id, repository_id=promotion.repository_id, base=promotion.base, release=promotion.release, sha=promotion.sha, tag=promotion.tag,
        status=promotion.status, how=promotion.how, url=promotion.url, detail=promotion.detail, commits=promotion.commits,  # type: ignore[arg-type]
        tickets=len(promotion.landing_ids or []), by=promotion.by, started_at=promotion.created_at, finished_at=promotion.finished_at,
    )  # fmt: skip


async def preview(session: AsyncSession, runs, repo: Repository) -> PromotionPreview:  # noqa: ANN001
    """What a promotion would bring now."""
    plan = await _plan(session, runs, repo)
    landings = await landed_since(session, repo, plan)
    on_way = await running(session, repo.id)
    return PromotionPreview(
        repository_id=repo.id, base=plan.base, release=plan.release, sha=plan.sha, release_sha=plan.release_sha, fast_forward=plan.fast_forward,
        total=plan.total, commits=[PromotionCommit(sha=c.sha, author=c.author, date=c.date, message=c.message) for c in plan.commits],
        tickets=await _tickets(session, landings), checked=await checked(session, repo.id, plan.sha), running=out(on_way) if on_way else None,
    )  # fmt: skip


async def start(session: AsyncSession, runs, repo: Repository, *, by: str, tag: str = "", sha: str | None = None) -> Promotion:  # noqa: ANN001
    """Take the base branch's head (or ``sha``) as the commit to promote and start its check. The landing
    watcher pushes it when the check passed."""
    if await running(session, repo.id):
        raise ServiceError(f"A promotion of {repo.name} is on its way already")
    tag = tag.strip()
    command = repo.check_command.strip()
    plan = await _plan(session, runs, repo, sha=sha, checkout=bool(command))
    if plan.total == 0:
        raise ServiceError(f"Nothing to promote: {plan.release} has {plan.base} at {plan.sha[:12]} already")
    if await running(session, repo.id):  # started by someone else while the runner fetched
        raise ServiceError(f"A promotion of {repo.name} is on its way already")
    landings = await landed_since(session, repo, plan)
    promotion = Promotion(
        project_id=repo.project_id, repository_id=repo.id, base=plan.base, release=plan.release, sha=plan.sha, release_sha=plan.release_sha or "",
        tag=tag, status=PromotionStatus.CHECKING, commits=plan.total, landing_ids=[x.id for x in landings], by=by[:100],
    )  # fmt: skip
    if await checked(session, repo.id, plan.sha):
        promotion.mode, promotion.detail = NONE, "This commit passed its checks before."
    elif command:
        promotion.mode, promotion.job_id = COMMAND, secrets.token_hex(8)
    elif landing_service.github_slug(repo):
        promotion.mode = GITHUB  # the host's checks of that commit (it is on the base branch, so they ran on its push)
    else:
        promotion.mode, promotion.detail = NONE, "No check command and no checks of the host: promoted without a check run."
    session.add(promotion)
    await session.commit()
    if promotion.mode == COMMAND:
        project = await session.get(Project, repo.project_id)
        credentials = await permissions.credentials(session, project)
        await session.commit()
        try:
            await _runner(runs, repo.project_id).check_train(TrainCheckSpec(credentials=credentials, key=key(repo), id=promotion.job_id or "", command=command))
        except Exception as exc:
            promotion.status, promotion.finished_at, promotion.detail = PromotionStatus.FAILED, utcnow(), f"The check could not be started: {str(exc)[:300]}"
            await session.commit()
            raise ServiceError(promotion.detail) from exc
    await flow_log.record(session, repo.project_id, "promotion.started", repository_id=repo.id, promotion_id=promotion.id, sha=plan.sha, base=plan.base, release=plan.release, commits=plan.total, branches=len(landings), mode=promotion.mode, by=by)
    return promotion


async def latest(session: AsyncSession, project_id: int) -> list[Promotion]:
    """Per repository of the project: the promotion on its way, else the last one."""
    rows = await session.scalars(select(Promotion).where(Promotion.project_id == project_id).order_by(Promotion.id.desc()).limit(50))
    seen: dict[int, Promotion] = {}
    for p in rows:
        seen.setdefault(p.repository_id, p)
    return list(seen.values())


async def of_landings(session: AsyncSession, landing_ids: list[int]) -> dict[int, Promotion]:
    """The promotion (pushed, or as a pull request) that took each of these branches to the release branch."""
    if not landing_ids:
        return {}
    repos = select(Landing.repository_id).where(Landing.id.in_(landing_ids))
    wanted, found = set(landing_ids), {}
    q = select(Promotion).where(Promotion.repository_id.in_(repos), Promotion.status.in_((PromotionStatus.PROMOTED, PromotionStatus.PULL_REQUEST))).order_by(Promotion.id)
    for p in await session.scalars(q):
        for landing_id in set(p.landing_ids or []) & wanted:
            found.setdefault(landing_id, p)
    return found
