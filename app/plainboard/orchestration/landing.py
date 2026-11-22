"""Brings finished branches to the base branch: together, with one check run for all of them.

A check run (CI, a test suite) is the slowest step of most work. Agents start one for every small
change if they are left to it, and then wait for it. Here they start none and wait for none: an
agent pushes its branch and hands it over (merge_when_green). On a timer, without any model:

- All branches of a repository that are ready are merged into one train, and the train is checked
  once: by the repository's check command, or on GitHub by the checks of a pull request for it.
- One train per repository at a time. What gets ready meanwhile goes on the next one, so the number
  of check runs follows the clock, not the number of changes. A train waits a moment for further
  branches before it leaves.
- A train that passes lands: all its branches are on the base branch.
- A train that fails costs no further check run to find out why. One agent gets its branches and
  the failures as one assignment and makes them pass together; a branch that needs more than a
  small fix it gives back to its author (return_branch), so the others land without it. The
  repaired branches are then checked again as a group. Searching for one guilty branch by checking
  halves does not work here: several branches fail at once, each for its own reason, and some only
  pass together. Meanwhile the next train leaves with what is new.
- After a few failed rounds the manager decides.

A repository that lands first (``repository.land_first``) does it the other way round: what is
ready is merged into the base branch at once, and the checks run on the base branch afterwards,
once for everything that landed. Their result takes nothing back. If they fail, one agent repairs
the base branch on a branch of its own, which lands like any other. The next batch lands when the
checks of the last one are over, so a check run is never replaced by a newer push. A repair that
someone cancelled is not ordered again for a few batches: they have it in hand. While the base
branch is red, only its repair lands (PlainBoard's, or one the manager ordered as such): the other
finished branches wait, at most ``RED_HOLD_SECONDS`` from the first red check run. Agents wait for
nothing; only merging does.

Every repository whose base branch has checks the host shows is guarded as well (``_guard``): after a
train landed, and whenever the base branch moves by itself (a merge past PlainBoard, a push), the
checks of its new head are looked at, as a train without branches. A check that runs only on the base
branch, one that fails at random, a merge result that differs from what was checked, a merge by hand:
all of them make it red although every train passed. A failure is run once more (it may be flaky);
if it stays red, it is repaired like a red batch that landed first, and trains wait the same way. A
base branch that stays red with nothing on its way to repair it is brought up to the manager every
``RED_REMIND_SECONDS`` until it is green.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from plainboard_protocol import Credentials, PullRequestCheck, PullRequestState, RepoSpec, TrainCheckSpec, TrainLandSpec, TrainResult, TrainSpec
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import ACTIVE_RUN_STATUSES, Agent, agent_repositories, Assignment, AssignmentStatus, Landing, LandingStatus, Project, Repository, Run, Task, Train, TrainStatus, utcnow
from ..services import agents as agent_service
from ..services import board as board_service
from ..services import flow_check as flow_check_service
from ..services import flow_log
from ..services import landings as landing_service
from ..services import repositories as repo_service
from ..services import permissions
from ..services import secrets as secret_service
from ..services import timing as timing_service
from ..services.errors import ServiceError
from ..services.util import truncate
from . import inbox, prompts
from .run_manager import RunManager
from .runner_client import RunnerError

log = logging.getLogger(__name__)

# Checks appear a moment after a push. A pull request without any after this time has none.
NO_CHECKS_GRACE_SECONDS = 120
# How a train's pull request is merged when its checked commit cannot be pushed to the base branch as it is (see
# the runner's github_merge): a merge commit keeps each branch's commits, a rebase replays them, a squash is the last resort.
MERGE_METHODS = ["merge", "rebase", "squash"]
# How often the head of a base branch is read (git ls-remote, no API quota): while branches land, and otherwise.
GUARD_BUSY_SECONDS = 3 * 60
GUARD_QUIET_SECONDS = 20 * 60
RED_REMIND_SECONDS = 4 * 3600  # a red base branch nothing repairs: the manager hears of it again after this
ACTIONS_RUN = re.compile(r"/actions/runs/(\d+)")


def checks_result(state: PullRequestState, age: float) -> str:
    """pending | failed | passed. Only when every check has finished: a failure is reported once, with all of them."""
    if any(c.state == "pending" for c in state.checks):
        return "pending"
    if any(c.state == "fail" for c in state.checks):
        return "failed"
    if not state.checks and age < NO_CHECKS_GRACE_SECONDS:
        return "pending"
    return "passed"


def base_result(checks: list[PullRequestCheck], age: float) -> str:
    """The checks of a base branch that got its branches first: pending | failed | passed | none.
    none: no result will come (the repository runs no checks on a push, or a newer push replaced the run)."""
    pending = any(c.state == "pending" for c in checks)
    if age < NO_CHECKS_GRACE_SECONDS or (pending and age < landing_service.BASE_CHECK_MAX_SECONDS):
        return "pending"
    if any(c.state == "fail" for c in checks):
        return "failed"
    if not checks or pending or any(c.state == "cancelled" for c in checks):
        return "none"
    return "passed"


def base_moved(detail: str) -> bool:
    """Whether a push was refused only because someone else pushed to the base branch meanwhile."""
    return "fetch first" in detail or "non-fast-forward" in detail


@dataclass
class Repo:
    """A repository with what the watcher needs to reach it."""

    repo: Repository
    project: Project
    credentials: Credentials
    mode: str | None
    slug: str | None
    key: str
    remote: RepoSpec
    redact: object


class LandingWatcher:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runs: RunManager, interval: float, linger: float = 0.0):
        self._sm = sessionmaker
        self._runs = runs
        self._interval = interval
        self.linger = linger  # seconds a train waits for further branches before it leaves
        self._holds: set[tuple[int, datetime]] = set()  # red phases whose hold is in the work log already
        # Branches held back because they change a file someone resolves a conflict in, with what was
        # being resolved then (Busy.key). While that is the same, they are not tried again.
        self._held: dict[int, tuple] = {}
        self._looked: dict[int, datetime] = {}  # repository -> when the head of its base branch was last read
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="landing-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Landing watcher tick failed")
            await asyncio.sleep(self._interval)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        async with self._sm() as s:
            waiting = await s.scalars(select(Landing.repository_id).where(Landing.status.in_(landing_service.ACTIVE)))
            running = await s.scalars(select(Train.repository_id).where(Train.status.in_(landing_service.RUNNING_TRAINS)))
            busy = {*waiting, *running}
            for repository_id in sorted(busy):
                try:
                    await self._repository(s, repository_id, now)
                except (RunnerError, ServiceError) as exc:  # e.g. the runner or the git host is unreachable: next time
                    await s.rollback()
                    log.info("Landing for repository %s postponed: %s", repository_id, exc)
            for repository_id in list(await s.scalars(select(Repository.id).order_by(Repository.id))):
                try:
                    await self._guard(s, repository_id, repository_id in busy, now)
                except (RunnerError, ServiceError) as exc:
                    await s.rollback()
                    log.info("Base branch of repository %s not looked at: %s", repository_id, exc)

    async def _repo(self, s: AsyncSession, repository_id: int) -> Repo | None:
        repo = await s.get(Repository, repository_id)
        project = await s.get(Project, repo.project_id) if repo else None
        if repo is None or project is None or not self._runs.runners.ready(project.id):
            return None  # an external runner that is away: its trains wait for it, nothing fails
        credentials = await permissions.credentials(s, project)
        return Repo(
            repo=repo,
            project=project,
            credentials=credentials,
            mode=landing_service.mode(repo),
            slug=landing_service.github_slug(repo),
            key=landing_service.train_key(repo),
            remote=landing_service.remote(repo),
            redact=secret_service.redactor(credentials.secrets),
        )

    async def _repository(self, s: AsyncSession, repository_id: int, now: datetime) -> None:
        r = await self._repo(s, repository_id)
        if r is None:
            return
        repo, project = r.repo, r.project
        if repo.land_first:
            train = await landing_service.running_train(s, repo.id)
        else:
            # Trains of branches don't wait for a look at the base branch: both go on side by side.
            if guard := await landing_service.running_train(s, repo.id, direct=True):
                try:
                    await self._watch_base(s, r, guard, now)
                except RunnerError as exc:
                    await s.rollback()
                    log.info("Checks of the base branch of %s not read: %s", repo.name, exc)
            train = await landing_service.running_train(s, repo.id, direct=False)
        if train is not None:
            try:
                over = await (self._watch_base(s, r, train, now) if train.direct else self._watch(s, r, train, now))
            except RunnerError as exc:
                # A train whose state cannot be read for long (e.g. a token that may not read checks): it can
                # only wait, but not silently. The manager hears it once.
                if not train.direct and not train.escalated and (now - train.created_at).total_seconds() >= landing_service.UNREADABLE_GRACE_SECONDS:
                    train.escalated = True
                    await s.commit()
                    await flow_log.record(s, project.id, "train.unreadable", train_id=train.id, what=truncate(str(exc), 300))
                    await self._tell_manager(
                        s, r,
                        f"The train of {repo.name} cannot be read from the git host ({truncate(str(exc), 200)}), so nothing of {repo.name} lands until it can.{permissions.github_hint(r.credentials)} "
                        "Most likely the access token may not read checks or pull requests. Tell the user; or set a check command for this repository "
                        "(update_repository), which needs no checks of the host.",
                    )
                raise
            if not over:
                return
        if self._runs.paused or project.paused_at is not None:
            return  # paused (everything, or this project): the train on its way is watched to its end, no new one leaves
        await (self._land_first(s, r, now) if repo.land_first else self._depart(s, r, now))

    # ------------------------------------------------------------------ a train on its way

    async def _watch(self, s: AsyncSession, r: Repo, train: Train, now: datetime) -> bool:
        """Look at the running train. True once it is over, so the next one can leave."""
        members = await landing_service.members(s, train)
        if not members:
            await self._abort(s, r, train, [], now)
            return True
        if train.mode == landing_service.GITHUB:
            return await self._watch_pull_request(s, r, train, members, now)
        state = (await self._runs.runners.of(r.repo.project_id).job_status([train.job_id or ""])).get(train.job_id or "")
        if state is None:  # the runner restarted and the check with it: again, with the next train
            await self._abort(s, r, train, members, now)
            return True
        if state.running:
            return False
        self._finished(train, now)
        if state.exit_code != 0:
            evidence = f"Check command: {r.repo.check_command.strip()}\nIt ended with exit code {state.exit_code}. Last output:\n{r.redact(state.tail)}\nFull log: {state.log_path}"  # type: ignore[operator]
            await self._failed(s, r, train, members, f"the check command ended with exit code {state.exit_code}", evidence, ["check command"])
            return True
        spec = TrainLandSpec(credentials=r.credentials, key=r.key, remote=r.remote, base=train.base, head_sha=train.head_sha, delete_branches={m.branch: m.head_sha for m in members})
        landed = await self._runs.runners.of(r.repo.project_id).land_train(spec)
        if landed.ok:
            await self._landed(s, r, train, members, now)
        elif base_moved(landed.detail):
            await self._abort(s, r, train, members, now)  # assembled and checked again on the new base
        else:
            await self._stuck(s, r, train, members, f"passed the check but could not be pushed to {train.base}: {truncate(landed.detail, 300)}")
        return True

    async def _watch_pull_request(self, s: AsyncSession, r: Repo, train: Train, members: list[Landing], now: datetime) -> bool:
        assert r.slug and train.number
        state = await self._runs.runners.of(r.repo.project_id).pull_request(r.credentials, r.slug, train.number)
        if state.state == "merged":  # by PlainBoard, or by someone who merged it by hand
            await self._landed(s, r, train, members, now)
            return True
        if state.state == "closed":
            self._finished(train, now)
            await self._stuck(s, r, train, members, f"was on a train whose pull request #{train.number} was closed without a merge")
            return True
        if train.status == TrainStatus.BLOCKED:
            return False  # until someone merges it or closes it
        if state.mergeable == "conflict":  # the base branch moved under it: assemble again
            await self._runs.runners.of(r.repo.project_id).close_pull_request(r.credentials, r.slug, train.number)
            await self._abort(s, r, train, members, now)
            return True
        result = checks_result(state, (now - train.created_at).total_seconds())
        if result == "pending" or (result == "passed" and state.mergeable == "unknown"):
            return False  # checks still running, or GitHub has not worked out yet whether it can be merged
        self._finished(train, now)
        if result == "failed":
            await self._runs.runners.of(r.repo.project_id).close_pull_request(r.credentials, r.slug, train.number)
            failed = [c for c in state.checks if c.state == "fail"]
            listed = "\n".join(f"- {c.name}" + (f": {c.link}" if c.link else "") for c in failed[:15])
            evidence = f"Failed checks ({train.url}):\n{listed}\nRead the failed logs once: the links, or gh run view <run id> --log-failed."
            await self._failed(s, r, train, members, "failed checks: " + ", ".join(c.name for c in failed[:8]), evidence, [c.name for c in failed])
            return True
        # Branches handed over that are not on this train may build on one of its branches: after a squash they are moved onto it.
        others = select(Landing.branch).where(Landing.repository_id == r.repo.id, Landing.status.in_([LandingStatus.WAITING, LandingStatus.BACK]), Landing.branch != "")
        restack = list(await s.scalars(others))
        await s.commit()  # nothing of the database is held while the runner merges (see _depart)
        merged = await self._runs.runners.of(r.repo.project_id).merge_pull_request(
            r.credentials, r.slug, train.number, methods=MERGE_METHODS, key=r.key, remote=r.remote, delete_branches={m.branch: m.head_sha for m in members},
            base=train.base, head_sha=state.head_sha or train.head_sha, branch=train.branch, restack=restack,
        )  # fmt: skip
        if merged.ok:
            await flow_log.record(s, r.project.id, "train.merged", train_id=train.id, how=truncate(merged.detail, 300))
        if merged.ok:
            await self._landed(s, r, train, members, now)
            return True
        train.status = TrainStatus.BLOCKED
        await s.commit()
        await flow_log.record(s, r.project.id, "train.blocked", train_id=train.id, detail=truncate(merged.detail, 1000))
        branches = ", ".join(m.branch for m in members)
        await self._tell_manager(
            s, r,
            f"The train with {branches} passed its checks but could not be merged: {truncate(merged.detail, 300)} "
            f"Merge pull request #{train.number} by hand or remove what blocks it; PlainBoard notices when it is merged. No further train leaves in {r.repo.name} until then.",
        )
        return False

    @staticmethod
    def _finished(train: Train, now: datetime) -> None:
        train.finished_at = now
        train.check_seconds = max(int((now - train.created_at).total_seconds()), 0)
        timing_service.forget_cache()

    async def _abort(self, s: AsyncSession, r: Repo, train: Train, members: list[Landing], now: datetime) -> None:
        """Give a train up without a result: its branches go on the next one."""
        train.status, train.finished_at = TrainStatus.ABORTED, train.finished_at or now
        for member in members:
            member.status, member.train_id = LandingStatus.WAITING, None
        await s.commit()
        await flow_log.record(s, r.project.id, "train.aborted", train_id=train.id, branches=[m.branch for m in members])

    async def _landed(self, s: AsyncSession, r: Repo, train: Train, members: list[Landing], now: datetime) -> None:
        if train.finished_at is None:
            self._finished(train, now)
        train.status = TrainStatus.LANDED
        for member in members:
            member.status, member.merged_at, member.group_no = LandingStatus.MERGED, now, None
        r.repo.tidied_at = None  # what landed alongside may have left branches behind: looked at with the next tidy tick
        await s.commit()
        self._looked.pop(r.repo.id, None)  # the base branch moved: its checks are looked at right away (_guard)
        await flow_log.record(s, r.project.id, "train.landed", train_id=train.id, branches=[m.branch for m in members], check_seconds=train.check_seconds)
        one_run = f"one check run of {timing_service.duration(train.check_seconds)} for {len(members)} branch{'es' if len(members) != 1 else ''}"
        for member in members:
            if ticket := await self._ticket(s, member):
                await board_service.add_comment(s, ticket, f"Branch {member.branch} is on {train.base} ({one_run}).", author_kind="system")
        listing = "; ".join([await self._label(s, m) for m in members])
        await self._tell_manager(s, r, f"Landed on {train.base} in {r.repo.name}: {listing}. It took {one_run}. Move the tickets this finishes to done.")

    async def _stuck(self, s: AsyncSession, r: Repo, train: Train, members: list[Landing], what: str) -> None:
        train.status = TrainStatus.FAILED
        for member in members:
            member.status, member.train_id, member.group_no = LandingStatus.STUCK, None, None
        await s.commit()
        await flow_log.record(s, r.project.id, "train.stuck", train_id=train.id, branches=[m.branch for m in members], what=what)
        branches = "; ".join([await self._label(s, m) for m in members])
        await self._tell_manager(s, r, f"Not landed in {r.repo.name}: {branches} {what}. PlainBoard has stopped for these branches; fix the cause, then hand them over again with merge_when_green.")

    async def _failed(self, s: AsyncSession, r: Repo, train: Train, members: list[Landing], summary: str, evidence: str, checks: list[str]) -> None:
        """The train did not pass. One branch on it: its author repairs it. Several: one agent makes
        them pass together, and they are checked again as a group."""
        train.status, train.failed_checks = TrainStatus.FAILED, "\n".join(checks)
        await s.commit()
        await flow_log.record(
            s, r.project.id, "train.failed", train_id=train.id, branches=[m.branch for m in members], landing_ids=[m.id for m in members],
            repaired_group=train.group_no is not None, check_seconds=train.check_seconds, summary=summary, evidence=truncate(evidence, 4000),
        )  # fmt: skip
        await self._keeps_failing(s, r, train.finished_at or utcnow())
        if len(members) == 1:
            instructions = prompts.repair_instructions(members[0].branch, evidence, members[0].failures + 1, landing_service.MAX_FAILED_ROUNDS)
            await self._back(s, r, members[0], f"did not pass on its own ({summary})", f"Repair branch {members[0].branch}", instructions)
            return
        for member in members:
            member.failures += 1
            member.failed_sha = member.head_sha
        # A branch that has used up its rounds goes to the manager on its own: the branches it was
        # checked with did not fail that often, and are repaired like any others.
        spent = [m for m in members if m.failures >= landing_service.MAX_FAILED_ROUNDS]
        members = [m for m in members if m not in spent]
        fixer = await self._fixer(s, members) if members else None
        nobody = bool(members) and fixer is None
        if fixer is None:
            spent, members = [*spent, *members], []
        if spent:
            listed = "; ".join([await label(s, m) for m in spent])
            for member in spent:
                member.status, member.train_id, member.group_no = LandingStatus.STUCK, None, None
            await s.commit()
            await flow_log.record(s, r.project.id, "train.given_up", train_id=train.id, branches=[m.branch for m in spent], repaired=len(members), nobody_available=nobody)
            why = "and none of their authors is available" if fixer is None and not any(m.failures >= landing_service.MAX_FAILED_ROUNDS for m in spent) else f"{landing_service.MAX_FAILED_ROUNDS} times now"
            others = f" The other {len(members)} branch{'es' if len(members) != 1 else ''} of that train {'are' if len(members) != 1 else 'is'} being repaired without {'them' if len(spent) != 1 else 'it'}." if members else ""
            await self._tell_manager(
                s, r,
                f"Not landed in {r.repo.name}: {listed} did not pass{' together' if len(spent) != 1 else ''} ({summary}), {why}. PlainBoard has stopped for {'these branches' if len(spent) != 1 else 'this branch'}.{others} "
                "Decide how to go on (another approach, the failing check itself, the user); merge_when_green puts a branch on a train again.",
            )
        if not members:
            return
        if len(members) == 1:  # what is left of the train is one branch: its author repairs it
            only = members[0]
            instructions = prompts.repair_instructions(only.branch, evidence, only.failures, landing_service.MAX_FAILED_ROUNDS)
            await give_back(s, self._runs, r.repo, r.project, only, f"did not pass ({summary})", f"Repair branch {only.branch}", instructions, counts=False)
            return
        rounds = max(m.failures for m in members)
        labels = [await label(s, m) for m in members]
        group = await landing_service.next_group(s, r.repo.id)
        for member in members:
            member.status, member.group_no = LandingStatus.BACK, group  # train_id stays: it names the repair they wait for
        await s.commit()
        instructions = prompts.train_repair_instructions(r.repo.name, train.base, labels, evidence, rounds, landing_service.MAX_FAILED_ROUNDS)
        repair = await self._runs.delegate(s, None, fixer, f"Make {len(members)} branches of {r.repo.name} pass together", instructions, None, train_id=train.id)
        await flow_log.record(s, r.project.id, "train.repair_ordered", train_id=train.id, agent_id=fixer.id, assignment_id=repair.id, rounds=rounds, branches=len(members))

    @staticmethod
    async def _fixer(s: AsyncSession, members: list[Landing]) -> Agent | None:
        """Who repairs a failed train: one of its authors who has nothing to do right now, so the
        repair starts at once instead of waiting behind other work; among those, and if all are
        busy, the author of most of its branches. Until the repair is done nothing of the train lands."""
        counts: dict[int, int] = {}
        for member in members:
            if member.agent_id:
                counts[member.agent_id] = counts.get(member.agent_id, 0) + 1
        authors = [a for a in [await s.get(Agent, agent_id) for agent_id in counts] if a and can_repair(a)]
        if not authors:
            return None
        ids = [a.id for a in authors]
        busy = {
            *await s.scalars(select(Assignment.agent_id).where(Assignment.agent_id.in_(ids), Assignment.status.in_((AssignmentStatus.QUEUED, AssignmentStatus.RUNNING)))),
            *await s.scalars(select(Run.agent_id).where(Run.agent_id.in_(ids), Run.status.in_(ACTIVE_RUN_STATUSES))),
        }
        return min(authors, key=lambda a: (a.id in busy, -counts[a.id], a.id))

    async def _back(self, s: AsyncSession, r: Repo, landing: Landing, what: str, title: str, instructions: str, *, counts: bool = True) -> None:
        await give_back(s, self._runs, r.repo, r.project, landing, what, title, instructions, counts=counts)

    # ------------------------------------------------------------------ the next train

    async def _depart(self, s: AsyncSession, r: Repo, now: datetime) -> None:
        """Assemble the next train from what is ready, and start its check."""
        waiting, busy = await self._ready(s, r, now)
        waiting = await self._hold(s, r, waiting, now)  # a train on a red base would fail with it
        # Branches that were repaired together go first and on their own: what fails then is theirs.
        groups = {w.group_no for w in waiting if w.group_no is not None}
        group = min(groups) if groups else None
        # A repaired group is checked whole. New branches: as many as land per check run (see train_size).
        size = landing_service.MAX_TRAIN if group is not None else await landing_service.train_size(s, r.repo.id)
        pick = [w for w in waiting if w.group_no == group][:size]
        await s.commit()
        if not pick:
            return
        if group is not None and await self._unrepaired(s, r, pick):
            return
        if group is None and self.linger > 0 and (now - min(p.updated_at for p in pick)).total_seconds() < self.linger:
            return  # more branches may be about to get ready: a fuller train means fewer check runs
        if r.mode is None:
            for landing in pick:
                landing.status = LandingStatus.STUCK
            await s.commit()
            await self._tell_manager(s, r, f"Not landed: {', '.join(p.branch for p in pick)}. " + landing_service.UNAVAILABLE.format(name=r.repo.name))
            return

        tickets = {p.id: await self._ticket(s, p) for p in pick}
        body = "Checked together by PlainBoard, so the checks run once for all of these branches:\n\n" + "\n".join(
            f"- {p.branch}" + (f" (ticket #{tickets[p.id].number}: {tickets[p.id].title})" if tickets[p.id] else "") for p in pick
        )
        spec = TrainSpec(
            credentials=r.credentials,
            key=r.key,
            remote=r.remote,
            base=r.repo.default_branch or None,
            branch=f"plainboard/train-{secrets.token_hex(4)}",  # not the train's number: see below
            members=[p.branch for p in pick],
            # A group failed together: a branch of it that needed no change still belongs on the train.
            skip_unchanged={p.branch: p.failed_sha for p in pick if p.failed_sha} if group is None else {},
            busy_files=list(busy.files),
            github=r.slug if r.mode == landing_service.GITHUB else None,
            title="Land " + truncate(", ".join(p.branch for p in pick), 200),
            body=body,
        )
        # The train is stored once it exists, not before. A row written first and committed after would keep
        # the database's one writer for as long as git takes, and every write of everyone waits for
        # that: seen as a click in a ticket that took nine seconds, and turns that started late.
        await s.commit()
        result = await self._runs.runners.of(r.repo.project_id).assemble_train(spec)
        by_branch = {p.branch: p for p in pick}
        on_train = [by_branch[b] for b in result.merged if b in by_branch]
        train = None
        if on_train:
            job_id = secrets.token_hex(8) if r.mode == landing_service.COMMAND else None
            if job_id:
                await self._runs.runners.of(r.repo.project_id).check_train(TrainCheckSpec(credentials=r.credentials, key=r.key, id=job_id, command=r.repo.check_command.strip()))
            train = Train(
                project_id=r.project.id, repository_id=r.repo.id, mode=r.mode, group_no=group, status=TrainStatus.CHECKING, created_at=now, branch=spec.branch,
                base=result.base, head_sha=result.head_sha, size=len(on_train), number=result.number, url=result.url, job_id=job_id,
            )  # fmt: skip
            s.add(train)
            await s.flush()
            for landing in on_train:
                landing.status, landing.train_id, landing.head_sha = LandingStatus.TRAIN, train.id, result.merged[landing.branch]
                landing.conflict_at, landing.conflict_files = None, []
        await s.commit()
        await flow_log.record(
            s, r.project.id, "train.departed" if train else "train.empty", train_id=train.id if train else None, branches=[p.branch for p in on_train],
            ready=len(waiting), size_limit=size, repaired_group=group is not None, mode=r.mode, landed_already=list(result.landed), conflicts=list(result.conflicts),
            missing=list(result.missing), unchanged=list(result.unchanged), waits=list(result.waits), held=list(result.held),
        )  # fmt: skip

        await self._left_out(s, r, result, by_branch, tickets, now, busy)

    async def _ready(self, s: AsyncSession, r: Repo, now: datetime) -> tuple[list[Landing], landing_service.Busy]:
        """What can go to the base branch next: handed over, or repaired since it came back. A branch
        whose conflict was resolved, or that waited behind another one, goes first: what lands before
        it could put it in conflict again. Without the branches that are held back for the files
        someone still resolves a conflict in (the second value)."""
        ready = select(Landing).where(Landing.repository_id == r.repo.id)
        for landing in list(await s.scalars(ready.where(Landing.status == LandingStatus.BACK))):
            if not await open_repair(s, landing):  # repaired: it goes on by itself
                landing.status, landing.conflict_files = LandingStatus.WAITING, []
        await s.commit()
        waiting = list(await s.scalars(ready.where(Landing.status == LandingStatus.WAITING).order_by(Landing.id)))
        waiting.sort(key=lambda w: (w.conflict_at is None, w.conflict_at or datetime.min, w.id))
        busy = await landing_service.busy(s, r.repo.id, now)  # after the look above: who is back resolves nothing any more
        waiting = [w for w in waiting if not (busy.files and self._held.get(w.id) == busy.key)]
        for landing in [w for w in waiting if not w.branch]:  # handed over as a pull request number, before trains
            state = await self._runs.runners.of(r.repo.project_id).pull_request(r.credentials, r.slug, landing.number) if r.slug and landing.number else None
            landing.branch = state.branch if state and state.state == "open" else ""
            if not landing.branch:
                landing.status = LandingStatus.CLOSED
        await s.commit()
        return [w for w in waiting if w.branch], busy

    async def _left_out(
        self, s: AsyncSession, r: Repo, result: TrainResult, by_branch: dict[str, Landing], tickets: dict[int, Task | None], now: datetime, busy: landing_service.Busy
    ) -> None:
        """What could not be merged with the others."""
        for branch in result.landed:
            landing = by_branch[branch]
            landing.status, landing.merged_at, landing.group_no = LandingStatus.MERGED, now, None
            await s.commit()
            if ticket := tickets[landing.id]:
                await board_service.add_comment(s, ticket, f"Branch {branch} is on {result.base} already.", author_kind="system")
            await self._tell_manager(s, r, f"{await self._label(s, landing)} is on {result.base} in {r.repo.name} already. Move the ticket this finishes to done.")
        for branch in result.conflicts:
            landing = by_branch[branch]
            files = list(result.conflict_files.get(branch, []))
            # A conflict is no failed check: it does not use up a round. Unchanged, it is not tried again.
            landing.failed_sha = landing.head_sha or ""
            # While its author resolves it, branches that change these files wait (landings.busy).
            landing.conflict_at, landing.conflict_files = now, files
            await self._back(
                s, r, landing, f"conflicts with {result.base}", f"Resolve the conflicts of branch {branch}", prompts.conflict_instructions(branch, result.base, files), counts=False
            )  # fmt: skip
        for branch in result.waits:
            # It merges onto the base branch, but not with a branch that went before it on this train.
            # Its author could resolve nothing yet: what it conflicts with is not on the base branch.
            # It stays ready and goes first next time. If the train lands, it conflicts with the base
            # branch then and goes to its author; if not, it may just land.
            landing = by_branch[branch]
            landing.conflict_at = landing.conflict_at or now
            await s.commit()
            await flow_log.record(s, r.project.id, "landing.waits", landing_id=landing.id, task_id=landing.task_id, branch=branch, files=list(result.conflict_files.get(branch, [])))
        for branch in result.held:
            landing = by_branch[branch]
            if self._held.get(landing.id) != busy.key:
                self._held[landing.id] = busy.key
                await flow_log.record(s, r.project.id, "landing.held", landing_id=landing.id, task_id=landing.task_id, branch=branch, files=list(busy.files), minutes=landing_service.CONFLICT_HOLD_SECONDS // 60)
        for branch, why in [*((b, "is not on the remote any more") for b in result.missing), *((b, "still fails, and nothing new was pushed since") for b in result.unchanged)]:
            landing = by_branch[branch]
            landing.status, landing.group_no = LandingStatus.STUCK, None
            await s.commit()
            await flow_log.record(s, r.project.id, "landing.stuck", landing_id=landing.id, task_id=landing.task_id, branch=branch, why=why)
            await self._tell_manager(s, r, f"Not landed in {r.repo.name}: {await self._label(s, landing)} {why}. Read the report on its ticket and decide how to go on; merge_when_green puts it on a train again.")

    # ------------------------------------------------------------------ land first, check after

    async def _land_first(self, s: AsyncSession, r: Repo, now: datetime) -> None:
        """Merge everything that is ready into the base branch now. The checks run on the base
        branch afterwards (_watch_base) and take nothing back."""
        waiting, busy = await self._ready(s, r, now)
        waiting = await self._hold(s, r, waiting, now)
        pick = waiting[: landing_service.MAX_BATCH]
        if not pick or (self.linger > 0 and (now - min(p.updated_at for p in pick)).total_seconds() < self.linger):
            return
        tickets = {p.id: await self._ticket(s, p) for p in pick}
        spec = TrainSpec(
            credentials=r.credentials,
            key=r.key,
            remote=r.remote,
            base=r.repo.default_branch or None,
            branch=f"plainboard/land-{secrets.token_hex(4)}",
            members=[p.branch for p in pick],
            skip_unchanged={p.branch: p.failed_sha for p in pick if p.failed_sha},
            busy_files=list(busy.files),
        )
        await s.commit()  # nothing of the database is held while the runner merges and pushes (see _depart)
        result = await self._runs.runners.of(r.repo.project_id).assemble_train(spec)
        by_branch = {p.branch: p for p in pick}
        landed = [by_branch[b] for b in result.merged if b in by_branch]
        pushed = None
        if landed:
            heads = {m.branch: result.merged[m.branch] for m in landed}
            pushed = await self._runs.runners.of(r.repo.project_id).land_train(TrainLandSpec(credentials=r.credentials, key=r.key, remote=r.remote, base=result.base, head_sha=result.head_sha, delete_branches=heads))
        if pushed is None or not pushed.ok:
            if pushed is not None and base_moved(pushed.detail):
                return  # someone else pushed to the base branch meanwhile: merged again with the next look
            if pushed is not None:
                for landing in landed:
                    landing.status, landing.group_no = LandingStatus.STUCK, None
                await s.commit()
                listing = "; ".join([await label(s, m) for m in landed])
                await flow_log.record(s, r.project.id, "train.stuck", branches=[m.branch for m in landed], what=truncate(pushed.detail, 300))
                await self._tell_manager(
                    s, r, f"Not landed in {r.repo.name}: {listing} could not be pushed to {result.base}: {truncate(pushed.detail, 300)} Fix the cause, then hand them over again with merge_when_green."
                )
        else:
            train = Train(
                project_id=r.project.id, repository_id=r.repo.id, mode=r.mode or landing_service.UNCHECKED, direct=True, status=TrainStatus.CHECKING, created_at=now,
                branch=spec.branch, base=result.base, head_sha=result.head_sha, size=len(landed),
            )  # fmt: skip
            if r.mode == landing_service.COMMAND:
                train.job_id = secrets.token_hex(8)
                await self._runs.runners.of(r.repo.project_id).check_train(TrainCheckSpec(credentials=r.credentials, key=r.key, id=train.job_id, command=r.repo.check_command.strip()))
            elif r.mode is None:
                train.status, train.finished_at = TrainStatus.LANDED, now  # nothing checks this repository
            s.add(train)
            await s.flush()
            for landing in landed:
                landing.status, landing.train_id, landing.head_sha, landing.merged_at, landing.group_no = LandingStatus.MERGED, train.id, result.merged[landing.branch], now, None
                landing.conflict_at, landing.conflict_files = None, []
            r.repo.tidied_at = None  # the branch tidy looks again (orchestration/tidy.py)
            await s.commit()
            timing_service.forget_cache()
            await flow_log.record(
                s, r.project.id, "train.landed", train_id=train.id, branches=[m.branch for m in landed], direct=True, ready=len(waiting), mode=train.mode,
                conflicts=list(result.conflicts), missing=list(result.missing), unchanged=list(result.unchanged), waits=list(result.waits), held=list(result.held),
            )  # fmt: skip
            for landing in landed:
                if ticket := tickets[landing.id]:
                    await board_service.add_comment(s, ticket, f"Branch {landing.branch} is on {result.base}.", author_kind="system")
            listing = "; ".join([await label(s, m) for m in landed])
            checks = "This repository has no checks." if r.mode is None else f"Its checks run on {result.base} now, once for all of this; PlainBoard has a failure repaired."
            await self._tell_manager(s, r, f"Landed on {result.base} in {r.repo.name}: {listing}. {checks} Move the tickets this finishes to done.")
        await self._left_out(s, r, result, by_branch, tickets, now, busy)

    async def _hold(self, s: AsyncSession, r: Repo, waiting: list[Landing], now: datetime) -> list[Landing]:
        """The base branch is red: only what repairs it lands. The next check run then says whether the
        repair worked, and a new failure belongs to one batch instead of piling onto the old one."""
        red = await landing_service.red_base(s, r.repo.id, now)
        if red is None:
            return waiting
        fixes = {a.id for a in red.repairs}
        held = [w for w in waiting if w.assignment_id not in fixes]
        if held and (r.repo.id, red.since) not in self._holds:
            self._holds.add((r.repo.id, red.since))
            await flow_log.record(s, r.project.id, "base.hold", train_id=red.train.id, branches=len(held), minutes=landing_service.RED_HOLD_SECONDS // 60)
        return [w for w in waiting if w.assignment_id in fixes]

    # ------------------------------------------------------------------ the base branch's own checks

    async def _guard(self, s: AsyncSession, repository_id: int, busy: bool, now: datetime) -> None:
        """Look at the base branch of a repository now and then: remind the manager of a red one nothing
        repairs, and when its head moved, look at the checks of the new head (a train without branches)."""
        last = self._looked.get(repository_id)
        if last is not None and abs((now - last).total_seconds()) < (GUARD_BUSY_SECONDS if busy else GUARD_QUIET_SECONDS):
            return
        self._looked[repository_id] = now
        r = await self._repo(s, repository_id)
        if r is None or self._runs.paused or r.project.paused_at is not None:
            return
        await self._remind(s, r, now)
        if r.slug is None:
            return  # no checks of a host to read: the check command of each train is all there is
        running = await landing_service.running_train(s, r.repo.id, direct=None if r.repo.land_first else True)
        if running is not None and r.repo.land_first:
            return  # a batch that landed first is being checked: its checks are those of the head
        base = r.repo.default_branch or await s.scalar(select(Train.base).where(Train.repository_id == r.repo.id, Train.base != "").order_by(Train.id.desc()).limit(1))
        if not base:
            return  # nothing landed yet, and no base branch is named: once a train ran, it is known
        await s.commit()
        head = await self._runs.runners.of(r.repo.project_id).branch_head(r.credentials, r.remote, base)
        seen = running.head_sha if running else await s.scalar(select(Train.head_sha).where(Train.repository_id == r.repo.id, Train.direct).order_by(Train.id.desc()).limit(1))
        if not head or head == seen:
            return
        if running is not None:  # the base branch moved on: the newer head contains the older one, its checks are what counts
            running.status, running.finished_at = TrainStatus.ABORTED, now
        s.add(Train(project_id=r.project.id, repository_id=r.repo.id, mode=landing_service.GITHUB, direct=True, size=0, status=TrainStatus.CHECKING, created_at=now, base=base, head_sha=head))
        await s.commit()

    async def _remind(self, s: AsyncSession, r: Repo, now: datetime) -> None:
        """A red base branch on which nothing moves: no repair open, no fix on its way, no check run. The
        manager was told when it went red or was given up; it may have dropped it since. It hears again
        every RED_REMIND_SECONDS, until the base branch is green."""
        red = await landing_service.red_now(s, r.repo.id)
        if red is None:
            return
        fixes = [a.id for a in red.repairs]
        if any(a.status in (AssignmentStatus.QUEUED, AssignmentStatus.RUNNING) for a in red.repairs):
            return
        if fixes and await s.scalar(select(Landing.id).where(Landing.repository_id == r.repo.id, Landing.assignment_id.in_(fixes), Landing.status.in_(landing_service.ACTIVE)).limit(1)):
            return
        if await s.scalar(select(Train.id).where(Train.repository_id == r.repo.id, Train.direct, Train.status == TrainStatus.CHECKING).limit(1)):
            return
        news = max(red.train.finished_at or red.train.created_at, r.repo.red_told_at or datetime.min)
        if (now - news).total_seconds() < RED_REMIND_SECONDS:
            return
        r.repo.red_told_at = now
        await s.commit()
        checks = ", ".join(red.train.failed_checks.split("\n")[:6]) or "its checks"
        await flow_log.record(s, r.project.id, "base.reminded", train_id=red.train.id, red_minutes=int((now - red.since).total_seconds() // 60))
        await self._tell_manager(
            s, r,
            f"{red.train.base} of {r.repo.name} has been red for {timing_service.duration((now - red.since).total_seconds())} ({truncate(checks, 200)}), and nothing repairs it now: "
            "no repair is open, no fix is on its way. Everything that lands builds on it. Delegate the repair with base_repair=true (its branch then lands first), "
            f"or change the check that keeps failing. PlainBoard reminds you every {RED_REMIND_SECONDS // 3600} h until it is green.",
        )

    async def _watch_base(self, s: AsyncSession, r: Repo, train: Train, now: datetime) -> bool:
        """A batch that landed before its checks: look at the checks of the base branch at that
        commit. True once they are over or will not come, so the next batch can land."""
        failed: list[str] = []
        links: dict[str, str] = {}
        evidence = ""
        if train.mode == landing_service.COMMAND:
            state = (await self._runs.runners.of(r.repo.project_id).job_status([train.job_id or ""])).get(train.job_id or "")
            if state is not None and state.running:
                return False
            result = "none" if state is None else "passed" if state.exit_code == 0 else "failed"
            if state is not None and result == "failed":
                failed = ["check command"]
                evidence = f"Check command: {r.repo.check_command.strip()}\nIt ended with exit code {state.exit_code}. Last output:\n{r.redact(state.tail)}\nFull log: {state.log_path}"  # type: ignore[operator]
        else:
            try:
                checks = await self._runs.runners.of(r.repo.project_id).commit_checks(r.credentials, r.slug, train.head_sha) if r.slug else []
            except RunnerError as exc:
                return await self._unreadable(s, r, train, now, exc)
            result = base_result(checks, (now - (train.rerun_at or train.created_at)).total_seconds())
            if result == "pending":
                return False
            links = {c.name: c.link for c in checks if c.state == "fail"}
            failed = list(links)
            evidence = self._failed_checks(train, links)
            if result == "failed" and landing_service.is_guard(train) and train.rerun_at is None and r.slug:
                # Every train passed its checks, and still: it may be a check that fails at random. Its failed
                # jobs run once more on the same commit before anybody repairs anything.
                runs = sorted({int(m.group(1)) for link in links.values() if (m := ACTIONS_RUN.search(link or ""))})[:20]
                if runs and await self._runs.runners.of(r.repo.project_id).rerun_failed(r.credentials, r.slug, runs):
                    train.rerun_at = now
                    await s.commit()
                    await flow_log.record(s, r.project.id, "base.rerun", train_id=train.id, checks=failed, runs=len(runs))
                    return False
        self._finished(train, now)
        train.status = {"failed": TrainStatus.FAILED, "passed": TrainStatus.LANDED}.get(result, TrainStatus.ABORTED)
        train.failed_checks = "\n".join(failed)
        await s.commit()
        guard = landing_service.is_guard(train)
        if guard and result not in ("failed", "passed"):
            return True  # a host without checks for it: nothing to say
        extra = {"guard": True, "rerun": train.rerun_at is not None} if guard else {}
        await flow_log.record(s, r.project.id, {"failed": "base.red", "passed": "base.green"}.get(result, "base.unchecked"), train_id=train.id, checks=failed, check_seconds=train.check_seconds, **extra)
        if result == "failed":
            await self._base_red(s, r, train, failed, evidence, links)
            await self._keeps_failing(s, r, now)
        await repo_service.publish(s, r.repo)  # the repository list shows a red base branch
        return True

    async def _unreadable(self, s: AsyncSession, r: Repo, train: Train, now: datetime, exc: RunnerError) -> bool:
        """The checks of a batch that landed cannot be read (the host answers with an error, e.g. a token that may not read them).
        Seen in production: the watcher tried again with every look, for hours, and nothing landed. A short
        failure is waited out. After that the batch counts as unchecked, landing goes on, and the manager
        hears once why nobody will be told when the base branch breaks."""
        known = await landing_service.checks_unreadable(s, train)
        if not known and (now - train.created_at).total_seconds() < landing_service.UNREADABLE_GRACE_SECONDS:
            raise exc
        self._finished(train, now)
        train.status, train.escalated = TrainStatus.ABORTED, True
        await s.commit()
        await flow_log.record(s, r.project.id, "base.unchecked", train_id=train.id, unreadable=truncate(str(exc), 300))
        if not known and not landing_service.is_guard(train):  # only a look by the way: no news for anybody
            await self._tell_manager(
                s, r,
                f"The checks of {train.base} in {r.repo.name} cannot be read from GitHub ({truncate(str(exc), 200)}). PlainBoard goes on landing without their "
                f"result, so nobody is told when {train.base} breaks.{permissions.github_hint(r.credentials)} "
                "Most likely the GitHub token used for this repository lacks a read permission: with a fine-grained token, read access to Actions and to Commit statuses. Tell the user; or set a check command for this repository (update_repository), which needs no checks of GitHub.",
            )
        return True

    @staticmethod
    def _failed_checks(train: Train, links: dict[str, str]) -> str:
        listed = "\n".join(f"- {name}" + (f": {link}" if link else "") for name, link in list(links.items())[:20])
        return f"Failed checks of {train.base} at {train.head_sha[:10]}:\n{listed}\nRead the failed logs once: the links, or gh run view <run id> --log-failed."

    async def _base_red(self, s: AsyncSession, r: Repo, train: Train, failed: list[str], evidence: str, links: dict[str, str]) -> None:
        """The base branch does not pass its checks with what landed. Nothing is taken back: one
        agent repairs it on a branch of its own, which lands like any other. A failure that is being
        repaired already, or whose fix has not landed yet, orders nothing more; what failed in
        addition gets a repair of its own, without the failures the other one has."""
        streak = await landing_service.red_streak(s, train)
        ids = [t.id for t in streak]
        was_red = {t.id: set(t.failed_checks.split("\n")) for t in streak}
        # A repair that was cancelled before it began repaired nothing.
        repairs = list(await s.scalars(select(Assignment).where(Assignment.train_id.in_(ids), Assignment.status != AssignmentStatus.CANCELLED).order_by(Assignment.id.desc()))) if ids else []
        known = {name for names in was_red.values() for name in names}
        # The latest repair of this red phase was cancelled: the manager has it in hand (it ordered one of its
        # own, or knows one cause behind several checks). Ordered again with every batch, it was cancelled again
        # with every batch, a manager turn each time.
        cancelled = await s.scalar(select(Assignment).where(Assignment.train_id.in_(ids), Assignment.status == AssignmentStatus.CANCELLED).order_by(Assignment.id.desc()).limit(1)) if ids else None
        if cancelled is not None and (not repairs or cancelled.id > repairs[0].id):
            batches = sum(1 for t in streak if t.id > (cancelled.train_id or 0)) + 1
            if batches <= landing_service.LEFT_ALONE_BATCHES:
                await flow_log.record(s, r.project.id, "base.left_alone", train_id=train.id, assignment_id=cancelled.id, batches=batches)
                return
        if repairs:
            last = repairs[0]
            # The branch that was handed over while that repair was worked on, not any branch of the same agent.
            on_its_way = select(Landing.id).where(Landing.repository_id == r.repo.id, Landing.assignment_id == last.id, Landing.status.in_(landing_service.ACTIVE))
            # Its fix could not be in this batch yet: what it repairs is not ordered again.
            if last.status in (AssignmentStatus.QUEUED, AssignmentStatus.RUNNING) or (last.finished_at and last.finished_at > train.created_at) or await s.scalar(on_its_way.limit(1)):
                more = [name for name in failed if name not in known]
                if not more:
                    await flow_log.record(s, r.project.id, "base.still_red", train_id=train.id, assignment_id=last.id)
                    return
                if links:  # only where the checks have names of their own
                    being_fixed = [name for name in failed if name in known]
                    evidence = self._failed_checks(train, {name: links.get(name, "") for name in more}) + (
                        f"\nThese failed too, and assignment #{last.id} repairs them (its fix has not landed yet): {', '.join(being_fixed[:12])}. Leave them: what they have in common with yours is fixed there."
                        if being_fixed
                        else ""
                    )
                    failed = more
        # How often this failure was repaired already: repairs of a batch that failed on at least all
        # of this. A base branch that is red for a new reason every time gets a repair every time;
        # only what does not get repaired goes to the manager.
        tried = [a for a in repairs if set(failed) <= was_red.get(a.train_id or 0, set())]
        if any(t.escalated and set(failed) <= was_red[t.id] for t in streak):
            return  # the manager has it
        guard = landing_service.is_guard(train)
        members = await self._since_green(s, r, train) if guard else list(await s.scalars(select(Landing).where(Landing.train_id == train.id).order_by(Landing.id)))
        fixer = await self._fixer(s, members) or (await self._fixer(s, list(await s.scalars(select(Landing).where(Landing.train_id.in_(ids))))) if ids else None)
        if fixer is None and guard:
            fixer = await self._anyone(s, r)
        names = ", ".join(failed[:8]) + (f" and {len(failed) - 8} more" if len(failed) > 8 else "")
        if fixer is None or len(tried) >= landing_service.MAX_FAILED_ROUNDS:
            train.escalated = True
            await s.commit()
            why = "and nobody who wrote what landed is available" if fixer is None else f"and {len(tried)} repairs have not made it pass"
            await flow_log.record(s, r.project.id, "base.escalated", train_id=train.id, repairs=len(tried), checks=failed)
            await self._tell_manager(
                s, r,
                f"{train.base} of {r.repo.name} does not pass its checks ({names}), {why}. Nothing was taken back: what landed is on {train.base}. "
                "Decide how to go on: delegate the repair with what you know, or change the check that keeps failing.",
            )
            return
        branches = [await label(s, m) for m in members]
        instructions = prompts.base_red_instructions(r.repo.name, train.base, branches, evidence, len(tried) + 1, landing_service.MAX_FAILED_ROUNDS, checked=guard)
        repair = await self._runs.delegate(s, None, fixer, f"Make {train.base} of {r.repo.name} pass its checks again", instructions, None, train_id=train.id)
        await flow_log.record(s, r.project.id, "base.repair_ordered", train_id=train.id, agent_id=fixer.id, assignment_id=repair.id, round=len(tried) + 1, checks=failed)
        if guard and not streak:
            # Where trains land after their checks, a red base branch is news to the manager: once per red phase,
            # with its next turn (no turn of its own: the repair is ordered).
            manager = await agent_service.get_manager(s, r.project.id)
            await inbox.push(
                s, manager.id,
                f"{train.base} of {r.repo.name} fails its checks ({names}) although its trains passed theirs"
                f"{' (also when run again)' if train.rerun_at else ''}. PlainBoard ordered the repair from {fixer.name} (assignment #{repair.id}); "
                f"finished branches of {r.repo.name} wait for it, at most {landing_service.RED_HOLD_SECONDS // 60} min.",
            )

    @staticmethod
    async def _since_green(s: AsyncSession, r: Repo, train: Train) -> list[Landing]:
        """For a look at the base branch: the branches that landed since the look before it."""
        before = await s.scalar(select(Train.created_at).where(Train.repository_id == r.repo.id, Train.direct, Train.id < train.id).order_by(Train.id.desc()).limit(1))
        since = before or train.created_at - timedelta(days=1)
        q = select(Landing).where(Landing.repository_id == r.repo.id, Landing.status == LandingStatus.MERGED, Landing.merged_at >= since)
        return list(await s.scalars(q.order_by(Landing.merged_at.desc()).limit(30)))

    async def _anyone(self, s: AsyncSession, r: Repo) -> Agent | None:
        """Nothing of PlainBoard's landed on a base branch that went red (a merge by hand): a member who works
        in this repository and can repair, one with nothing to do first."""
        team = [a for a in await s.scalars(select(Agent).join(agent_repositories, agent_repositories.c.agent_id == Agent.id).where(agent_repositories.c.repository_id == r.repo.id).order_by(Agent.id)) if can_repair(a)]
        if not team:
            return None
        ids = [a.id for a in team]
        busy = set(await s.scalars(select(Assignment.agent_id).where(Assignment.agent_id.in_(ids), Assignment.status.in_((AssignmentStatus.QUEUED, AssignmentStatus.RUNNING)))))
        return min(team, key=lambda a: (a.id in busy, a.id))

    async def _unrepaired(self, s: AsyncSession, r: Repo, pick: list[Landing]) -> bool:
        """Whether a group comes back from its repair exactly as it failed: the same branches, none
        of them changed. Checking that again would fail again, so the manager decides."""
        failed = await s.get(Train, pick[0].train_id) if pick[0].train_id else None
        if failed is None or failed.size != len(pick):
            return False  # a branch was taken off: the others may pass without it
        for landing in pick:
            if await self._runs.runners.of(r.repo.project_id).branch_head(r.credentials, r.remote, landing.branch) != landing.failed_sha:
                return False
        for landing in pick:
            landing.status, landing.train_id, landing.group_no = LandingStatus.STUCK, None, None
        await s.commit()
        await flow_log.record(s, r.project.id, "train.unrepaired", train_id=failed.id, branches=[p.branch for p in pick])
        branches = "; ".join([await label(s, p) for p in pick])
        await self._tell_manager(
            s, r,
            f"Not landed in {r.repo.name}: {branches} did not pass together, and their repair changed none of them. PlainBoard has stopped for these "
            "branches. Read the repair's report (list_assignments): if the cause is outside the branches (a check that fails at random, a broken "
            f"{failed.base}), get that fixed; merge_when_green puts a branch on a train again.",
        )
        return True

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _ticket(s: AsyncSession, landing: Landing) -> Task | None:
        return await s.get(Task, landing.task_id) if landing.task_id else None

    async def _label(self, s: AsyncSession, landing: Landing) -> str:
        return await label(s, landing)

    async def _tell_manager(self, s: AsyncSession, r: Repo, text: str) -> None:
        await tell_manager(s, self._runs, r.project, text)

    async def _keeps_failing(self, s: AsyncSession, r: Repo, now: datetime) -> None:
        """A check run failed, and so did most of the last day's: the manager reads which checks and what
        the quick check covers with its next turn, at most every ``FAILING_TOLD_SECONDS`` per repository.
        It gets no turn for it: every failure is repaired without the manager. Seen in production: 43
        of 65 check runs of a night failed, mostly on the same check. The nightly tidy-up found it
        (flow_check), but hours later, and in a turn in which the manager delegates nothing."""
        told = r.repo.failing_told_at
        if told is not None and (now - told).total_seconds() < flow_check_service.FAILING_TOLD_SECONDS:
            return
        finding = await flow_check_service.failing_checks(s, r.repo, now - timedelta(hours=24))
        if finding is None:
            return
        r.repo.failing_told_at = now
        await s.commit()
        manager = await agent_service.get_manager(s, r.project.id)
        await inbox.push(s, manager.id, prompts.checks_keep_failing(finding.title, finding.detail, finding.action))
        await flow_log.record(s, r.project.id, "checks.failing_told", repository=r.repo.name, finding=finding.title)


def can_repair(agent: Agent) -> bool:
    """Whether a branch can go back to this agent. A manager does not repair. An agent that is paused
    with the team can: the assignment waits for it and starts when the team goes on."""
    return not agent.is_manager and (agent.enabled or agent.team_pause)


async def open_repair(s: AsyncSession, landing: Landing) -> bool:
    """Whether someone is repairing this branch right now: on its own, or with the train it failed on."""
    mine = Assignment.landing_id == landing.id
    if landing.train_id is not None:
        mine = or_(mine, Assignment.train_id == landing.train_id)
    q = select(Assignment.id).where(mine, Assignment.status.in_((AssignmentStatus.QUEUED, AssignmentStatus.RUNNING)))
    return bool(await s.scalar(q.limit(1)))


async def label(s: AsyncSession, landing: Landing) -> str:
    author = await s.get(Agent, landing.agent_id) if landing.agent_id else None
    ticket = await s.get(Task, landing.task_id) if landing.task_id else None
    about = ", ".join(part for part in (author.name if author else "", f"ticket #{ticket.number}" if ticket else "") if part)
    name = landing.title if (landing.title or "").startswith(f"{landing.branch} carrying ") else landing.branch  # a branch that carries others says which
    return name + (f" ({about})" if about else "")


async def tell_manager(s: AsyncSession, runs: RunManager, project: Project, text: str) -> None:
    manager = await agent_service.get_manager(s, project.id)
    await inbox.push(s, manager.id, text)
    if project.auto_wake_manager:
        await runs.request_wake(s, manager.id)


async def give_back(
    s: AsyncSession, runs: RunManager, repo: Repository, project: Project, landing: Landing, what: str, title: str, instructions: str, *, counts: bool = True
) -> bool:
    """Give a branch back to its author as one assignment; after a few rounds the manager decides.
    False if it went to the manager. ``counts``: this is a failed round of its own (not one that was
    counted with its train already)."""
    if counts:
        landing.failures += 1
        landing.failed_sha = landing.head_sha
    landing.train_id, landing.group_no = None, None
    author = await s.get(Agent, landing.agent_id) if landing.agent_id else None
    ticket = await s.get(Task, landing.task_id) if landing.task_id else None
    available = author is not None and can_repair(author)
    if landing.failures >= landing_service.MAX_FAILED_ROUNDS or not available:
        landing.status = LandingStatus.STUCK
        await s.commit()
        await flow_log.record(s, project.id, "landing.stuck", landing_id=landing.id, task_id=landing.task_id, branch=landing.branch, why=what, failures=landing.failures)
        if available:
            why = f"{landing.failures} times now"
        elif author is None or author.is_manager:
            why = "and it has no author who could repair it (you handed it over): delegate that to the member who knows it"
        else:
            why = f"and its author {author.name} is switched off"
        await tell_manager(
            s, runs, project,
            f"Not landed in {repo.name}: {await label(s, landing)} {what}, {why}. PlainBoard has stopped for this branch. "
            "Decide how to go on (another approach, the failing check itself, the user); merge_when_green puts it on a train again.",
        )
        return False
    landing.status = LandingStatus.BACK
    await s.commit()
    await flow_log.record(s, project.id, "landing.back", landing_id=landing.id, task_id=landing.task_id, agent_id=author.id, branch=landing.branch, why=what, failures=landing.failures)
    if not await open_repair(s, landing):  # the repair is an assignment on the ticket: no comment says it again
        await runs.delegate(s, None, author, title, instructions, ticket, landing_id=landing.id)
    return True
