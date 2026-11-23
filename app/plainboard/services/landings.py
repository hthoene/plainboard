"""Landings: finished branches that PlainBoard brings to the base branch (the merge_when_green tool).

A check run is the slowest step of most work, and agents start one for every small change if they
are left to it. So they don't start any. An agent pushes its branch, hands it over and is done.
PlainBoard merges all branches that are ready into one train and has it checked once; one train at a
time per repository, so the number of check runs follows the clock and not the number of changes.
The watcher (orchestration/landing.py) does this without any model.

A repository can also land first and check after (``repository.land_first``): everything that is
ready is merged into the base branch at once, the checks run on the base branch afterwards, and a
failure is repaired there by one assignment. Nothing waits for a check run then, and a failure
takes nothing back; the price is a base branch that is red for a while. While it is red, only its
repair lands (``red_base``), for a limited time.

Every repository whose base branch has checks PlainBoard can read is also watched where nothing lands
first: after each landing and whenever its base branch moves (a merge past PlainBoard, a push), the
checks of the new head are looked at, as a train without branches (``Train.size`` 0, ``direct``).
A red result is repaired like one of a batch that landed first: the same red phase, repairs, hold
and escalation. Until the base branch is green again the manager is reminded now and then.

How a train is checked depends on the repository:
- its check command (``repository.check_command``): PlainBoard runs it on the merged branches and
  pushes to the base branch if it succeeds. Plain git, any host.
- no command, but on GitHub with the general token: a pull request for the merged branches, whose
  checks decide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from plainboard_protocol import RepoSpec
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Assignment, AssignmentStatus, Landing, LandingStatus, Repository, Task, Train, TrainStatus
from .util import truncate
from . import repositories as repo_service

GITHUB_URL = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
MAX_FAILED_ROUNDS = 3  # after this many, the author stops getting it back and the manager decides
MAX_TRAIN = 12  # branches on one train, while trains pass
SIZE_WINDOW = 8  # trains whose results decide the size of the next one
MAX_BATCH = 30  # branches merged in one go where they land before their checks
# A repair of a red base branch that was cancelled is not ordered again for this many red batches: whoever
# cancelled it has the repair in hand. After them it is ordered again, so the branch does not stay red for good.
LEFT_ALONE_BATCHES = 4
# While a base branch is red, only its repair lands: the next check run then says whether the repair worked,
# and what fails belongs to one batch. At most this long from the first red check run, then everything lands
# again as it is: waiting for a repair that does not come must not stop the work.
RED_HOLD_SECONDS = 45 * 60
# While an author resolves a conflict, branches that change the same files wait, at most this long.
# Seen in production: a branch came back seven times in an hour, because each time its author had
# merged the lock file, another branch that changed the lock file had landed in between. Waiting
# costs no turn; resolving the same conflict again does.
CONFLICT_HOLD_SECONDS = 20 * 60
BASE_CHECK_MAX_SECONDS = 3600  # checks of the base branch that take longer are not waited for
# Checks that cannot be read at all (the host answers with an error, e.g. a token without read access to
# checks): a short failure is waited out, after this the batch counts as unchecked and landing goes on.
UNREADABLE_GRACE_SECONDS = 600
ACTIVE = (LandingStatus.WAITING, LandingStatus.TRAIN, LandingStatus.BACK)
# Held branches (#96): a message to the author after the hand-over (MESSAGE), or the manager by hand (MANAGER).
# NEXT: released by a new hand-over while on a train that has the commit from before: it lands with the next one.
MESSAGE, MANAGER, NEXT = "message", "manager", "next"
HELD = (MESSAGE, MANAGER)
HOLD_TELL_SECONDS = 2 * 3600  # held for a message this long without an answer: the manager hears it
RUNNING_TRAINS = (TrainStatus.CHECKING, TrainStatus.BLOCKED)
COMMAND, GITHUB = "command", "github"
UNCHECKED = "none"  # a batch of a repository that has no checks PlainBoard could look at


def github_slug(repo: Repository) -> str | None:
    """owner/name if the repository is on GitHub and reached with the general token (what gh uses)."""
    found = GITHUB_URL.search(repo.url.strip())
    return f"{found.group(1)}/{found.group(2)}" if found and repo.auth == "default" else None


def mode(repo: Repository) -> str | None:
    """How this repository's trains are checked; None if PlainBoard cannot land its branches."""
    if repo.check_command.strip():
        return COMMAND
    return GITHUB if github_slug(repo) else None


def can_land(repo: Repository) -> bool:
    """Whether PlainBoard brings this repository's branches to the base branch. Landing first needs
    nothing but git; landing after the checks needs checks it can look at."""
    return repo.land_first or mode(repo) is not None


UNAVAILABLE = (
    "PlainBoard cannot land branches of {name}: it has no check command and is not on GitHub with the general token. "
    "Open a merge request yourself and name it in your report. (The manager can set a check command with update_repository.)"
)


def train_key(repo: Repository) -> str:
    """Names the runner's own clone of the repository."""
    return f"p{repo.project_id}-r{repo.id}"


def remote(repo: Repository) -> RepoSpec:
    (spec,) = repo_service.specs([repo])
    return spec


async def find(session: AsyncSession, repository_id: int, branch: str) -> Landing | None:
    q = select(Landing).where(Landing.repository_id == repository_id, Landing.branch == branch).order_by(Landing.id.desc()).limit(1)
    return await session.scalar(q)


def held(landing: Landing) -> bool:
    """Whether it must not land now (see HELD)."""
    return landing.hold in HELD


def hold(landing: Landing, kind: str, reason: str, now: datetime) -> None:
    landing.hold, landing.held_at, landing.hold_reason, landing.hold_told_at = kind, now, truncate(reason, 300), None


def release(landing: Landing) -> None:
    """No longer held. On a train with the commit it was held with, it lands with that train."""
    landing.hold, landing.held_at, landing.hold_reason, landing.hold_told_at = "", None, "", None


async def hold_for_message(session: AsyncSession, agent_id: int, text: str, reason: str, now: datetime) -> list[Landing]:
    """Someone wrote to an agent while branches it handed over have not landed: they may be about to
    change (#95: a correction arrived after the hand-over). Those branches are held until the agent
    has read the message and hands them over again or confirms them. A message that names tickets
    (#<number>) holds only the branches of those tickets, if it has any. A branch the manager holds
    stays held by the manager."""
    mine = list(await session.scalars(select(Landing).where(Landing.agent_id == agent_id, Landing.status.in_(ACTIVE), Landing.hold != MANAGER).order_by(Landing.id)))
    named = {int(n) for n in re.findall(r"#(\d+)", text)}
    if mine and named:
        numbers = dict((await session.execute(select(Task.id, Task.number).where(Task.id.in_([m.task_id for m in mine if m.task_id])))).all())
        about = [m for m in mine if numbers.get(m.task_id or 0) in named]
        mine = about or mine
    for landing in mine:
        hold(landing, MESSAGE, reason, now)
    if mine:
        await session.commit()
    return mine


async def held_long(session: AsyncSession, now: datetime, project_id: int | None = None) -> list[Landing]:
    """Branches held for a message for HOLD_TELL_SECONDS or more: their author has neither handed them over again nor confirmed them."""
    q = select(Landing).where(Landing.hold == MESSAGE, Landing.status.in_(ACTIVE), Landing.held_at <= now - timedelta(seconds=HOLD_TELL_SECONDS))
    if project_id is not None:
        q = q.where(Landing.project_id == project_id)
    return list(await session.scalars(q.order_by(Landing.id)))


async def hand_over(
    session: AsyncSession, repo: Repository, branch: str, head_sha: str, *, agent_id: int, task_id: int | None, fresh_start: bool, assignment_id: int | None = None,
    releases: bool = False,
) -> Landing:
    """Put a branch on the list for the next train. ``fresh_start`` (the manager hands it over, or
    it comes with new commits after it had landed) forgets its failed rounds. ``releases``: a hold
    ends with this hand-over (see ``held``); on a train with another commit, it lands with the next one."""
    landing = await find(session, repo.id, branch)
    # A held branch on a train keeps the commit that train checks (head_sha): released with another one, it
    # must not land with that train.
    checked = landing.head_sha if landing is not None and landing.status == LandingStatus.TRAIN and landing.hold else None
    if landing is not None and landing.hold in HELD and releases:
        release(landing)
        if checked is not None and checked != head_sha:
            landing.hold = NEXT
    if landing is None or landing.status == LandingStatus.MERGED:
        landing = Landing(project_id=repo.project_id, repository_id=repo.id, branch=branch)
        session.add(landing)
    elif fresh_start:
        landing.failures, landing.failed_sha = 0, ""
    if landing.agent_id is None or not fresh_start:
        landing.agent_id = agent_id  # a manager that hands over someone's branch again does not become its author
    landing.head_sha, landing.assignment_id = checked or head_sha, assignment_id
    if not (landing.title or "").startswith(f"{branch} carrying "):
        landing.title = branch[:300]
    landing.task_id = task_id or landing.task_id
    # On a train already: its result decides. Being repaired: it goes on when the repair is finished,
    # together with the branches it failed with.
    if landing.status not in (LandingStatus.TRAIN, LandingStatus.BACK):
        landing.status, landing.train_id, landing.group_no = LandingStatus.WAITING, None, None
    await session.commit()
    return landing


async def running_train(session: AsyncSession, repository_id: int, direct: bool | None = None) -> Train | None:
    """``direct``: only a look at the base branch (True), or only a train of branches (False)."""
    q = select(Train).where(Train.repository_id == repository_id, Train.status.in_(RUNNING_TRAINS))
    if direct is not None:
        q = q.where(Train.direct.is_(direct))
    return await session.scalar(q.order_by(Train.id.desc()).limit(1))


def is_guard(train: Train) -> bool:
    """A look at the checks of the base branch's head with no branches of its own (see the module doc)."""
    return train.direct and train.size == 0


async def members(session: AsyncSession, train: Train) -> list[Landing]:
    q = select(Landing).where(Landing.train_id == train.id, Landing.status == LandingStatus.TRAIN).order_by(Landing.id)
    return list((await session.scalars(q)).all())


async def active_for_project(session: AsyncSession, project_id: int) -> list[Landing]:
    q = select(Landing).where(Landing.project_id == project_id, Landing.status.in_(ACTIVE)).order_by(Landing.id)
    return list((await session.scalars(q)).all())


async def train_size(session: AsyncSession, repository_id: int) -> int:
    """How many branches the next train takes. One branch that fails takes its whole train with it,
    so a big train only pays while nearly every branch passes: of n branches that each fail with
    probability p, n * (1 - p) ** n land per check run. p is measured on the trains of new branches
    that ran lately (every failed one had at least one such branch), and the size is the n that
    lands most. No failures: as many as fit."""
    lately = select(Train).where(
        Train.repository_id == repository_id, Train.group_no.is_(None), Train.direct.is_(False), Train.size > 0, Train.status.in_((TrainStatus.LANDED, TrainStatus.FAILED))
    )
    trains = list(await session.scalars(lately.order_by(Train.id.desc()).limit(SIZE_WINDOW)))
    failed = sum(t.status == TrainStatus.FAILED for t in trains)
    if not failed:
        return MAX_TRAIN
    p = min(failed / sum(t.size for t in trains), 0.9)
    return max(2, max(range(1, MAX_TRAIN + 1), key=lambda n: n * (1 - p) ** n))


async def carry(session: AsyncSession, carrier: Landing, train_id: int) -> list[Landing]:
    """The repair of a failed train handed over a branch of its own instead of one of the train's:
    that branch carries them. They are no longer landed on their own, so nothing is checked again
    unchanged and nobody is told that the repair changed nothing. Returns what it carries."""
    train = await session.get(Train, train_id)
    if train is None or train.direct or carrier.train_id == train_id:
        return []
    group = list(await session.scalars(select(Landing).where(Landing.train_id == train_id, Landing.status == LandingStatus.BACK, Landing.id != carrier.id).order_by(Landing.id)))
    if not group:
        return []
    numbers = dict((await session.execute(select(Task.id, Task.number).where(Task.id.in_([m.task_id for m in group if m.task_id])))).all())
    for member in group:
        member.status, member.group_no = LandingStatus.CLOSED, None
    listed = ", ".join(m.branch + (f" (#{numbers[m.task_id]})" if m.task_id in numbers else "") for m in group)
    carrier.title = truncate(f"{carrier.branch} carrying {listed}", 300)
    await session.commit()
    return group


@dataclass
class RedBase:
    """A base branch that fails its checks with what landed, while finished branches wait for its repair."""

    train: Train  # the latest batch whose checks failed
    since: datetime  # when the first check run of this red phase ended
    until: datetime  # from then on everything lands again
    repairs: list[Assignment]  # ordered for it and not cancelled, newest first
    streak: list[Train] = field(default_factory=list)  # the failed batches of this red phase, newest first


async def _last_result(session: AsyncSession, repository_id: int) -> Train | None:
    """The latest batch that landed before its checks and has a result."""
    done = Train.status.in_((TrainStatus.FAILED, TrainStatus.LANDED))
    return await session.scalar(select(Train).where(Train.repository_id == repository_id, Train.direct, done).order_by(Train.id.desc()).limit(1))


async def red_now(session: AsyncSession, repository_id: int) -> RedBase | None:
    """Set while the base branch of this repository is red: its latest checks failed. However long ago."""
    last = await _last_result(session, repository_id)
    if last is None or last.status != TrainStatus.FAILED:
        return None
    streak = [last, *await red_streak(session, last)]
    since = streak[-1].finished_at or streak[-1].created_at
    ordered = select(Assignment).where(Assignment.train_id.in_([t.id for t in streak]), Assignment.status != AssignmentStatus.CANCELLED)
    return RedBase(last, since, since + timedelta(seconds=RED_HOLD_SECONDS), list(await session.scalars(ordered.order_by(Assignment.id.desc()))), streak)


async def red_base(session: AsyncSession, repository_id: int, now: datetime) -> RedBase | None:
    """Set while finished branches wait for the repair of this repository's base branch (RED_HOLD_SECONDS)."""
    red = await red_now(session, repository_id)
    return red if red is not None and now < red.until else None


async def red_repositories(session: AsyncSession, project_id: int) -> list[Repository]:
    """The project's repositories whose base branch is red now."""
    return [repo for repo in await session.scalars(select(Repository).where(Repository.project_id == project_id).order_by(Repository.id)) if await red_now(session, repo.id)]


async def checks_unreadable(session: AsyncSession, train: Train) -> bool:
    """Whether the batch before this one was given up because its checks could not be read: this one is then
    not waited for either, and nobody is told a second time."""
    before = select(Train).where(Train.repository_id == train.repository_id, Train.direct, Train.id < train.id).order_by(Train.id.desc()).limit(1)
    last = await session.scalar(before)
    return last is not None and last.status == TrainStatus.ABORTED and last.escalated


async def latest_red(session: AsyncSession, project_id: int) -> Train | None:
    """The batch a repair of a base branch is for: the latest one that failed, of a base branch that is red now."""
    found: Train | None = None
    for repository_id in list(await session.scalars(select(Repository.id).where(Repository.project_id == project_id))):
        last = await _last_result(session, repository_id)
        if last is not None and last.status == TrainStatus.FAILED and (found is None or last.id > found.id):
            found = last
    return found


async def red_streak(session: AsyncSession, train: Train) -> list[Train]:
    """The batches right before this one whose checks failed on the base branch, newest first: how
    long the base branch has been red. One without a result does not end it."""
    earlier = select(Train).where(Train.repository_id == train.repository_id, Train.direct, Train.id < train.id).order_by(Train.id.desc()).limit(30)
    streak = []
    for other in await session.scalars(earlier):
        if other.status == TrainStatus.LANDED:
            break
        if other.status == TrainStatus.FAILED:
            streak.append(other)
    return streak


async def next_group(session: AsyncSession, repository_id: int) -> int:
    """A number above every group used so far, for the branches of a train that failed together."""
    used = [
        await session.scalar(select(func.max(Landing.group_no)).where(Landing.repository_id == repository_id)) or 0,
        await session.scalar(select(func.max(Train.group_no)).where(Train.repository_id == repository_id)) or 0,
    ]
    return max(used) + 1


@dataclass(frozen=True)
class Busy:
    """The files in which authors resolve conflicts of this repository's branches right now."""

    files: tuple[str, ...] = ()
    key: tuple = ()  # changes when one of them is back, gives up, or another one starts


async def busy(session: AsyncSession, repository_id: int, now: datetime) -> Busy:
    since = now - timedelta(seconds=CONFLICT_HOLD_SECONDS)
    q = select(Landing).where(Landing.repository_id == repository_id, Landing.status == LandingStatus.BACK, Landing.conflict_at.is_not(None), Landing.conflict_at > since)
    resolving = [landing for landing in await session.scalars(q.order_by(Landing.id)) if landing.conflict_files]
    files = tuple(sorted({name for landing in resolving for name in landing.conflict_files}))
    return Busy(files, tuple((landing.id, landing.conflict_at.isoformat()) for landing in resolving))
