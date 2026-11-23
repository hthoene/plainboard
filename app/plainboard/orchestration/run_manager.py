"""Run orchestration: queue, dispatch, execution, inter-agent communication and run lifecycle.

Rules:
- One run per agent at a time; runs of different members execute in parallel up to the project's
  limit (``projects.parallel_limit``; every project counts by itself). The limit is about members only: a manager's turn starts as soon as the
  manager is free, also at limit 0, because the user and the team wait for it.
  Nested runs (an agent waiting on ``ask_agent``) bypass the limit so a waiting agent can never
  starve the run it waits for.
- A wait inside a turn (``wait_job``, ``ask_agent``) never holds back an answer: it ends when the
  user writes to the agent, and a wait for a job also when a teammate asks it something.
- ``ask_agent`` blocks the caller; a wait-for graph between agents rejects cycles (deadlocks).
- An assignment is one piece of work with one report. It stays open while a job started for it is
  running: the job's result continues it in a later turn, and only the turn after which nothing of
  it is running any more closes it and sends the report.
- A member's context belongs to its piece of work: an assignment for another ticket starts a new session.
- Messages between agents and results for the requester go to the inbox; an agent is woken up once
  per batch of updates (coalesced).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime

from plainboard_protocol import CompletionSpec, JobSpec, JobState, ResultEvent, RunEvent, RunSpec, ToolBridgeSpec, ToolCallResult, WorkspaceSpec
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import get_config
from ..events import bus
from ..models import (
    ACTIVE_RUN_STATUSES,
    Agent,
    Assignment,
    AssignmentStatus,
    Attachment,
    Comment,
    Conversation,
    Decision,
    Job,
    JobStatus,
    Message,
    Project,
    Repository,
    Run,
    RunStatus,
    RunTrigger,
    Runner,
    Task,
    TaskStatus,
    Train,
    utcnow,
)
from ..schemas import AssignmentOut, MessageOut, RunOut, TaskUpdate
from ..services import agents as agent_service
from ..services import board as board_service
from ..services import decisions as decision_service
from ..services import ideas as idea_service
from ..services import ticket_state
from ..services import flow_log
from ..services import jobs as job_service
from ..services import landings as landing_service
from ..services import mcp as mcp_service
from ..services import skills as skill_service
from ..services import mentions as mention_service
from ..services import pause as pause_service
from ..services import permissions
from ..services import projects as project_service
from ..services import repositories as repo_service
from ..services import runners as runner_service
from ..services import secrets as secret_service
from ..services import timing as timing_service
from ..services import todos as todo_service
from ..services import scratchpad as scratchpad_service
from ..services import search as search_service
from ..services import accounts as account_service
from ..services import limits as limits_service
from ..services import settings as settings_service
from ..services.errors import NotFound, ServiceError, WaitEnded
from ..services.util import truncate
from ..tools import registry
from . import inbox, prompts, tool_code
from .context import ContextManager, workspace_path
from .runner_client import RunnerClient, RunnerError, RunnerStreamBroken, RunnerUnavailable
from .runner_updates import RunnerUpdates
from .runners import Runners
from .tunnel import Tunnel

log = logging.getLogger(__name__)

MAX_ASK_DEPTH = 3
SIGN_INS_SECONDS = 60  # how often the runners are asked for kept sign-ins that were refreshed (check_sign_ins)
FRESH_SECONDS = 1800  # how often the built-in runner looks whether a kept sign-in is due for a refresh ahead
ASK_TIMEOUT_SECONDS = 900
ASK_BUSY_TIMEOUT_SECONDS = 120  # the teammate is in a turn of unknown length, so don't block the asker for long
CANCEL_GRACE_SECONDS = 15
STOP_WAIT_SECONDS = 10  # at shutdown: how long a turn that ended may go on writing its end
# The state of tickets that were moved to done after it was last written (refresh_closed_tickets).
CLOSED_STATES_LOOKED_AT = 20
CLOSED_STATES_AT_ONCE = 2  # rewrites running at a time: an installation with many old tickets catches up over minutes
CLOSED_STATE_RETRY_SECONDS = 3600
RESTARTED = "Interrupted by a restart"
MAX_RESTART_RESUMES = 3  # an assignment cut off this often by restarts goes to the manager instead of on
# ... if those turns were cut off right after they began: restarts that leave no time to work. A turn
# that had worked for a while when an update came is no sign of that, however many updates there are.
CUT_SHORT_SECONDS = 300
EMPTY_TURN = "The turn ended without a single step (no answer, no tool call), also when tried once more"
SWITCH_TRIES = 3  # a turn turned away by a used-up plan limit runs again on another account, this often at most
RUNNER_TRIES, RUNNER_RETRY_SECONDS = 12, 5.0  # how long a turn waits for a runner that is not up yet (about a minute, plus connect timeouts)
LOST = "The turn broke off: the runner no longer had it"
LOST_AFTER_SECONDS = 60  # a turn the runner does not list for this long is over (check_turns)


@dataclass
class RunContext:
    run_id: int
    project_id: int
    agent_id: int
    token: str
    redact: Callable[[str], str] = lambda text: text  # hides secret values in what is stored and shown
    account_key: str | None = None  # the provider account the run uses (its limit reports belong to it)
    assignment_id: int | None = None  # the piece of work this turn belongs to
    task_id: int | None = None
    tools: frozenset[str] | None = None  # the only tools it may call (a question, orchestration/questions.py); None: its role's


def _count_and_age(stats: tuple[int, datetime | None] | None) -> tuple[int, int]:
    """Open tasks and the age of the oldest in days."""
    if not stats or not stats[1]:
        return (stats[0] if stats else 0), 0
    return stats[0], (utcnow() - stats[1]).days


@dataclass
class Outcome:
    result: ResultEvent | None = None
    error: str | None = None
    cancelled: bool = False
    last_text: str | None = None
    tool_messages: dict[str, int] = field(default_factory=dict)
    resumed: bool = False
    limited: bool = False  # the provider reported a used-up plan limit of the account during the turn
    broken: bool = False  # the connection to the runner broke during the turn

    @property
    def broke_early(self) -> bool:
        """The connection broke before the turn did a single step: nothing is lost by running it again."""
        return self.broken and self.result is None and not self.last_text and not self.tool_messages

    @property
    def empty(self) -> bool:
        """The turn ended as done without a single step: no model call, no text, no tool call. Seen
        when Claude Code took a session up with something left over from before and ended at once."""
        res = self.result
        return bool(res and not res.is_error and not self.error and not self.cancelled and not self.limited and not res.num_turns and not res.text and not self.last_text and not self.tool_messages)


async def _limit_since_answer(s: AsyncSession, conversation_id: int) -> bool:
    """A turn of this conversation was turned away by a used-up limit after the agent last answered."""
    error = await s.scalar(
        select(Message).where(Message.conversation_id == conversation_id, Message.role == "system", Message.kind == "error").order_by(Message.id.desc()).limit(1)
    )
    if error is None or not (error.meta or {}).get("limit"):
        return False
    answered = await s.scalar(select(func.max(Message.id)).where(Message.conversation_id == conversation_id, Message.role == "assistant"))
    return answered is None or answered < error.id



def _iso(at: datetime) -> str:
    return at.isoformat() + "Z"


def chat_parts(run: Run) -> list[dict]:
    """The messages waiting for a chat run; an older run without parts is one message."""
    if run.parts:
        return list(run.parts)
    return [{"id": 1, "author": run.author_name, "text": run.input_text, "named": False, "attachment_ids": list(run.attachment_ids), "at": _iso(run.created_at)}]


def _part_input(part: dict) -> str:
    """What the agent reads of one message: where several people work, with the name in front."""
    return f"[{part['author']}] {part['text']}" if part.get("named") else part["text"]


def _chat_input(parts: list[dict]) -> str:
    return "\n\n".join(text for text in map(_part_input, parts) if text.strip())


class RunManager:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runner: RunnerClient):
        self._sm = sessionmaker
        self.runner = runner  # the built-in runner: what is the installation's, not a project's
        self.runners = Runners(runner)  # each project's runner (orchestration/runners.py)
        self.updates = RunnerUpdates(self)  # external runners update themselves when free
        self.runners.on_connect = lambda _runner_id: self._wake.set()
        self.context = ContextManager(self.runners)
        from .questions import Questions  # it builds on this module

        self.questions = Questions(self)  # people's questions beside the chat: no queue, read only
        from .ideas import Ideas

        self.ideas = Ideas(self)  # sorting ideas and planning tickets from them: no queue, no tools
        self._uploads: dict[int, set[str]] = {}  # external runner -> attachment files it has
        self._copying: dict[tuple[int, int], asyncio.Task[None]] = {}  # (runner, project) -> attachments being copied in the background
        self._seen: dict[int, float] = {}  # runner id -> when its last_seen_at was last written (monotonic)
        self._orphan_checks: set[asyncio.Task[None]] = set()  # looks at turns of an external runner nobody follows
        self._running: dict[int, asyncio.Task[None]] = {}
        self._unlimited: set[int] = set()  # running turns outside the parallel limit: nested ones and managers'
        self._busy_agents: set[int] = set()
        self._job_news: set[int] = set()  # agents whose open assignment has a job result they have not read yet
        self._comment_news: dict[int, set[int]] = {}  # agent -> its open assignments with a ticket comment it has not read yet
        self._job_waits: dict[int, float] = {}  # run id -> seconds its turn spent waiting for jobs
        self._tokens: dict[str, RunContext] = {}
        self._waiters: dict[int, asyncio.Future[str]] = {}
        self._orphan_asks: set[int] = set()
        self._job_waiters: dict[int, asyncio.Future[str]] = {}  # job id -> the turn waiting for its result
        self._job_waiter_runs: dict[int, int | None] = {}  # job id -> the run of that turn
        # run id -> what its turn waits for right now ("job" or "ask"), each with the way to end that wait
        self._turn_waits: dict[int, list[tuple[str, asyncio.Future[str]]]] = {}
        self._waits_for: dict[int, dict[int, int]] = {}
        self._cancel_requested: set[int] = set()
        self._streams: dict[int, float] = {}  # run id -> when its current request to the runner began
        self._preparing: set[int] = set()  # running turns not handed to the runner yet (prompt, attachments copied)
        self._missing: dict[int, float] = {}  # running turn -> since when the runner does not list it
        self._lost: set[int] = set()  # turns being ended because the runner lost them
        self._limits_asked = 0.0
        self._limits_task: asyncio.Task[None] | None = None
        self._sign_ins_asked = 0.0
        self._fresh_asked = 0.0
        self._sign_in_tasks: set[asyncio.Task[None]] = set()
        self._state_tasks: dict[int, asyncio.Task[None]] = {}  # tickets whose state is being rewritten
        self._resting: set[asyncio.Task[None]] = set()  # paused projects whose turns and jobs are being stopped
        self._run_tasks: set[asyncio.Task[None]] = set()  # every turn's task until it is done
        self._state_tried: dict[int, float] = {}  # closed tickets whose last rewrite was started then (see refresh_closed_tickets)
        self._unread: dict[int, set[int]] = {}  # running turn -> waiting chat messages (runs) it was told about and has not read yet
        # agent -> what changes its waiting chat run (a message joining, one withdrawn): read and write back
        # the parts one after the other, so no message overwrites another. The app runs as one process.
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._wake = asyncio.Event()
        self._limit_timer: asyncio.TimerHandle | None = None  # wakes the dispatcher when a used-up subscription is free again
        self._dispatcher: asyncio.Task[None] | None = None
        self.paused = False  # everything is paused (services/pause.py): nothing new starts

    # ------------------------------------------------------------------ lifecycle

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------ external runners

    async def runner_connected(self, runner_id: int, tunnel: Tunnel, hello: dict) -> None:
        """An external runner connected (api/runners.py): what waited for it starts. A runner that was
        started anew (another instance) has lost what ran on it: the turns' streams find their turn
        gone and end as after a restart, the job watcher reports its jobs as lost."""
        self.updates.connected(runner_id)
        old, restarted = self.runners.attach(runner_id, tunnel, hello)
        if old is not None:
            log.info("Runner %s connected again: its older connection is closed", runner_id)
        if restarted:
            log.warning("Runner %s was started anew: what ran on it is lost", runner_id)
        self._uploads.pop(runner_id, None)  # it may have been rebuilt
        for key, task in list(self._copying.items()):
            if key[0] == runner_id:
                task.cancel()
        check = asyncio.create_task(self._drop_orphans(runner_id), name=f"orphans-{runner_id}")  # one per connection
        self._orphan_checks.add(check)
        check.add_done_callback(self._orphan_checks.discard)
        self._seen[runner_id] = time.monotonic()
        async with self._sm() as s:
            runner = await runner_service.seen(s, runner_id, str(hello.get("version") or ""))
            if runner is not None and runner.away_told_at is not None:
                runner.away_told_at = None
                await s.commit()
                for manager in await self._managers_on(s, runner_id):
                    await inbox.push(s, manager.id, f'The runner "{runner.name}" is connected again: the turns and jobs of this project run again.')
        log.info("Runner %s connected (version %s)", runner_id, hello.get("version") or "?")

    async def _drop_orphans(self, runner_id: int) -> None:
        """Turns the runner still runs that no task here follows any more (the app restarted, or the
        stream could not be resumed): nobody would read their events, so they are cancelled."""
        try:
            client = self.runners.client(runner_id)
            for run_id in await client.active_runs() - set(self._running):
                log.info("Cancelling run %s on runner %s: nobody here follows it any more", run_id, runner_id)
                await client.cancel(run_id)
        except RunnerError as exc:
            log.info("Orphaned turns of runner %s not looked at: %s", runner_id, exc)

    async def runner_gone(self, runner_id: int, tunnel: Tunnel) -> None:
        if not self.runners.detach(runner_id, tunnel):
            return
        async with self._sm() as s:
            await runner_service.seen(s, runner_id)
        log.info("Runner %s disconnected", runner_id)

    async def _managers_on(self, s: AsyncSession, runner_id: int) -> list[Agent]:
        return list(await s.scalars(select(Agent).join(Project, Project.id == Agent.project_id).where(Project.runner_id == runner_id, Agent.is_manager)))

    async def mark_seen(self, *, every: float = runner_service.SEEN_SECONDS) -> None:
        """The connected runners' last_seen_at, at most every ``every`` seconds: the app may end without
        their disconnect being written (a restart), and then this is the time shown."""
        now = time.monotonic()
        due = [rid for rid in list(self.runners.tunnels) if self.runners.connected(rid) and now - self._seen.get(rid, 0.0) >= every]
        if not due:
            return
        async with self._sm() as s:
            await s.execute(update(Runner).where(Runner.id.in_(due)).values(last_seen_at=utcnow()))
            await s.commit()
        self._seen.update(dict.fromkeys(due, now))

    async def check_sign_ins(self, force: bool = False) -> None:
        """Kept sign-ins, at most every ``SIGN_INS_SECONDS`` (the job watcher's look): the installation's still in the
        built-in runner's home are taken over (``accounts.take_over``), what the runners refreshed is kept and
        handed on (``keep_sign_ins``), and every ``FRESH_SECONDS`` those due soon are refreshed ahead on the
        built-in runner, so the other runners always get them fresh and none refreshes one itself."""
        if not force and time.monotonic() - self._sign_ins_asked < SIGN_INS_SECONDS:
            return
        self._sign_ins_asked = time.monotonic()
        async with self._sm() as s:
            if await account_service.any_home_login(s):
                await account_service.take_over(s, self.runner)
            if not await account_service.any_kept_login(s):
                return
            for runner_id in self.runners.in_use():
                if self.runners.connected(runner_id):
                    await self.keep_sign_ins(s, runner_id)
            if force or time.monotonic() - self._fresh_asked >= FRESH_SECONDS:
                self._fresh_asked = time.monotonic()
                await self._refresh_sign_ins(s)

    async def keep_sign_ins(self, s: AsyncSession, runner_id: int | None) -> None:
        """Keep what a runner refreshed of kept sign-ins, and give it to the other runners where a turn of the
        account is going on: the CLI there reads its file again before it refreshes, and finds the new token."""
        kept = await account_service.take_sign_ins(s, self.runners.client(runner_id))
        if not kept:
            return
        busy: dict[int | None, set[str]] = {}
        for ctx in self._tokens.values():
            if ctx.account_key:
                busy.setdefault(self.runners.runner_id(ctx.project_id), set()).add(ctx.account_key)
        for other, keys in busy.items():
            entries = [e for e in kept if e.account in keys]
            if other == runner_id or not entries or not self.runners.connected(other) or not self.runners.signins(other):
                continue
            try:
                await self.runners.client(other).update_sign_ins(entries)
            except Exception as exc:
                log.info("Runner %s did not take the refreshed sign-ins: %s", other, exc)

    async def _refresh_sign_ins(self, s: AsyncSession) -> None:
        values = await settings_service.get_all(s)
        accounts = [a for a in await account_service.kept_logins(s) if a.enabled and a.secret_enc]
        for account in accounts:
            try:
                await self.runner.refresh_sign_in(account.provider, settings_service.credentials(values, account=account_service.fields(account)))
            except Exception as exc:
                log.info("Could not refresh the sign-in of account %s ahead: %s", account.key, str(exc)[:200])
        if accounts:
            await self.keep_sign_ins(s, None)

    def _take_sign_ins_soon(self, project_id: int) -> None:
        """A turn on a kept sign-in ended: what it refreshed is kept at once, not only at the next look."""

        async def take() -> None:
            try:
                async with self._sm() as s:
                    await self.keep_sign_ins(s, self.runners.runner_id(project_id))
            except Exception:
                log.exception("Could not keep the sign-ins of project %s's runner", project_id)

        task = asyncio.create_task(take(), name=f"sign-ins-{project_id}")
        self._sign_in_tasks.add(task)
        task.add_done_callback(self._sign_in_tasks.discard)

    async def check_runners(self) -> None:
        """An external runner that stays away: the managers of its projects hear it once (job_watcher's look).
        The connected ones are marked seen."""
        await self.mark_seen()
        now = time.monotonic()
        for runner_id, since in list(self.runners.away_since.items()):
            if now - since < runner_service.AWAY_TELL_SECONDS or self.runners.connected(runner_id):
                continue
            async with self._sm() as s:
                runner = await s.get(Runner, runner_id)
                if runner is None or runner.away_told_at is not None:
                    self.runners.away_since.pop(runner_id, None)
                    continue
                runner.away_told_at = utcnow()
                await s.commit()
                self.runners.away_since.pop(runner_id, None)
                for manager in await self._managers_on(s, runner_id):
                    await inbox.push(
                        s,
                        manager.id,
                        f'The runner "{runner.name}" this project runs on has not been connected for more than {runner_service.AWAY_TELL_SECONDS // 60} minutes. '
                        "Turns and jobs of this project wait until it is back. Tell the user: the runner's machine or its container (plainboard-runner) has to run and reach PlainBoard.",
                    )
                    await self.request_wake(s, manager.id)
        self.updates.poke()

    async def _sync_uploads(self, project_id: int, prompt: str = "") -> None:
        """An external runner lacks the project's attachments when its uploads are on no volume or it is
        new: they are copied to it, each once. What the turn's prompt names is copied before the turn
        starts, the rest in the background, the newest first."""
        runner_id = self.runners.runner_id(project_id)
        if runner_id is None:
            return
        async with self._sm() as s:
            names = list(await s.scalars(select(Attachment.storage_name).where(Attachment.project_id == project_id, Attachment.purged_at.is_(None)).order_by(Attachment.id.desc())))
        have = self._uploads.setdefault(runner_id, set())
        if not (wanted := [n for n in names if n not in have]):
            return
        try:
            missing = set(await self.runners.client(runner_id).missing_uploads(wanted))
        except RunnerError as exc:
            log.warning("Attachments not copied to runner %s: %s", runner_id, exc)
            return
        have.update(n for n in wanted if n not in missing)
        if first := [n for n in wanted if n in missing and n in prompt]:
            await self._copy_uploads(runner_id, project_id, first, have)
        key = (runner_id, project_id)
        if key not in self._copying and (rest := [n for n in wanted if n in missing and n not in have]):
            task = asyncio.create_task(self._copy_uploads(runner_id, project_id, rest, have), name=f"uploads-{runner_id}-{project_id}")
            self._copying[key] = task
            task.add_done_callback(lambda _t: self._copying.pop(key, None))

    async def _copy_uploads(self, runner_id: int, project_id: int, names: list[str], have: set[str]) -> None:
        """Copy these attachments to the runner; the chat shows how far it got ("uploads.copying")."""
        client = self.runners.client(runner_id)
        total, shown = len(names), 0.0
        bus.publish(project_id, "uploads.copying", {"done": 0, "total": total})
        try:
            for done, name in enumerate(names, 1):
                if name not in have:
                    path = get_config().uploads_dir / name
                    if path.is_file():
                        await client.put_upload(name, await asyncio.to_thread(path.read_bytes))
                    have.add(name)
                if done == total or time.monotonic() - shown >= 1:
                    shown = time.monotonic()
                    bus.publish(project_id, "uploads.copying", {"done": done, "total": total})
        except RunnerError as exc:
            log.warning("Attachments not copied to runner %s: %s", runner_id, exc)
            bus.publish(project_id, "uploads.copying", {"done": 0, "total": 0})
        except asyncio.CancelledError:
            bus.publish(project_id, "uploads.copying", {"done": 0, "total": 0})
            raise

    async def start(self) -> None:
        async with self._sm() as s:
            self.paused = await pause_service.state(s) is not None  # a pause holds across the restart it was made for
            await self.runners.load(s)
            for runner_id in self.runners.in_use()[1:]:
                self.runners.away_since.setdefault(runner_id, time.monotonic())  # until it connects
        await self._recover_interrupted()
        await self.settle_open_assignments()  # an assignment that waited for a job when the app stopped must not wait forever
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="run-dispatcher")
        self._wake.set()

    async def _recover_interrupted(self) -> None:
        async with self._sm() as s:
            now = utcnow()
            interrupted = list((await s.scalars(select(Run).where(Run.status == RunStatus.RUNNING))).all())
            resumed: set[int] = set()
            for run in interrupted:
                run.status, run.error, run.finished_at = RunStatus.FAILED, RESTARTED, now
                if run.assignment_id and (a := await s.get(Assignment, run.assignment_id)):
                    # A restart is not a failure of the work: the assignment stays open and its agent
                    # goes on in the same session. Only one that keeps being cut off goes to the manager.
                    earlier = (await s.execute(select(Run.started_at, Run.finished_at).where(Run.assignment_id == a.id, Run.error == RESTARTED, Run.id != run.id))).all()
                    cut_short = sum(began is None or ((ended or now) - began).total_seconds() < CUT_SHORT_SECONDS for began, ended in [*earlier, (run.started_at, now)])
                    if a.status == AssignmentStatus.RUNNING and cut_short < MAX_RESTART_RESUMES:
                        resumed.add(run.id)
                    else:
                        a.status, a.result, a.finished_at = AssignmentStatus.FAILED, RESTARTED, now
            for run in (await s.scalars(select(Run).where(Run.status == RunStatus.QUEUED, Run.trigger == RunTrigger.ASK))).all():
                run.status, run.error, run.finished_at = RunStatus.CANCELLED, "Asking agent is gone (restart)", now
            await s.commit()
            for run in interrupted:
                try:
                    await flow_log.record(
                        s, run.project_id, "run.interrupted", agent_id=run.agent_id, run_id=run.id, assignment_id=run.assignment_id, task_id=run.task_id,
                        trigger=str(run.trigger), resumed=run.id in resumed,
                    )  # fmt: skip
                    if run.id in resumed:
                        held = await scratchpad_service.current(s, run.agent_id, run.assignment_id)
                        await inbox.push(s, run.agent_id, "\n".join(filter(None, (prompts.RESTART_NOTE, scratchpad_service.delivered(held and held.scratchpad)))))
                        await self._settle(s, run.agent_id)
                    else:
                        await self._report_interrupted(s, run)
                except Exception:  # a failed report must not keep the app from starting
                    log.exception("Could not report interrupted run %s", run.id)
            await self._adopt_orphans(s)

    async def _adopt_orphans(self, s: AsyncSession) -> None:
        """An assignment that waits but has no turn would wait for ever, and block further work on its
        ticket (it counts as open work). Give it its turn. (Before, ordering work from a paused agent
        left such an assignment behind.)"""
        has_turn = select(Run.id).where(Run.assignment_id == Assignment.id).exists()
        for assignment in list(await s.scalars(select(Assignment).where(Assignment.status == AssignmentStatus.QUEUED, ~has_turn))):
            try:
                agent = await s.get(Agent, assignment.agent_id)
                if agent is None:
                    continue
                creator = await s.get(Agent, assignment.created_by_agent_id) if assignment.created_by_agent_id else None
                task = await s.get(Task, assignment.task_id) if assignment.task_id else None
                from_name = creator.name if creator else "PlainBoard" if assignment.landing_id or assignment.train_id else "the user"
                state = "" if assignment.landing_id or assignment.train_id else await ticket_state.text_for(s, assignment.task_id)
                run = await self.enqueue(
                    s, agent, RunTrigger.ASSIGNMENT, prompts.assignment_prompt(assignment, from_name, task, state),
                    task_id=assignment.task_id, assignment_id=assignment.id, source_agent_id=assignment.created_by_agent_id, effort=assignment.effort, paused_ok=True,
                )  # fmt: skip
                assignment.run_id = run.id
                await s.commit()
                await flow_log.record(s, assignment.project_id, "assignment.adopted", assignment_id=assignment.id, agent_id=agent.id, task_id=assignment.task_id)
            except Exception:
                log.exception("Could not give assignment %s its turn", assignment.id)

    async def _report_interrupted(self, s: AsyncSession, run: Run) -> None:
        """An interrupted turn outside an open assignment is not retried (it may be half done): tell whoever can decide."""
        agent = await s.get(Agent, run.agent_id)
        project = await s.get(Project, run.project_id)
        if agent is None or project is None:
            return
        assignment = await s.get(Assignment, run.assignment_id) if run.assignment_id else None
        if assignment:
            ticket = await s.get(Task, assignment.task_id) if assignment.task_id else None
            ref = f", ticket #{ticket.number}" if ticket else ""
            recipient = assignment.created_by_agent_id or (await agent_service.get_manager(s, project.id)).id
            await inbox.push(
                s, recipient, f'Assignment #{assignment.id} "{assignment.title}" ({agent.name}{ref}) was interrupted by a restart and may be half done. Check its state and delegate again if needed.'
            )
            await self._maybe_wake(s, project, recipient)
        elif agent.is_manager:
            await inbox.push(s, agent.id, f"Your previous turn was interrupted by a restart and may be incomplete: {truncate(run.input_text or 'processing updates', 500)}")
            await self._maybe_wake(s, project, agent.id)

    async def stop(self) -> None:
        if self._dispatcher:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
        try:
            await self.mark_seen(every=0)  # the connections end with the app: their disconnect is not written
        except Exception:
            log.exception("Runners not marked seen at shutdown")
        # Everything started in the background ends before the app disposes of its database engine: a task
        # still holding a connection then keeps it open, in the middle of a transaction even, with its locks.
        # A task that ends may start another (a turn that ends rewrites its ticket's state), so until none is left.
        for _ in range(10):
            tasks = [
                task
                for task in (
                    self._limits_task,
                    self.updates._task,
                    *self._running.values(),
                    *self._copying.values(),
                    *self._state_tasks.values(),
                    *self._resting,
                    *self._orphan_checks,
                )
                if task is not None and not task.done()
            ]
            ending = [task for task in self._run_tasks if not task.done() and task not in tasks]  # writing their turn's end
            if not tasks and not ending:
                break
            for task in tasks:
                task.cancel()
            if ending:  # not cut off, unless the runner they ask takes too long
                await asyncio.wait(ending, timeout=STOP_WAIT_SECONDS)
                for task in ending:
                    task.cancel()
            await asyncio.gather(*tasks, *ending, return_exceptions=True)

    def context_for_token(self, token: str, run_id: int) -> RunContext | None:
        ctx = self._tokens.get(token)
        return ctx if ctx and ctx.run_id == run_id else None

    # ------------------------------------------------------------------ enqueueing

    async def enqueue(
        self,
        s: AsyncSession,
        agent: Agent,
        trigger: RunTrigger,
        text: str,
        *,
        task_id: int | None = None,
        assignment_id: int | None = None,
        schedule_id: int | None = None,
        source_agent_id: int | None = None,
        parent_run_id: int | None = None,
        effort: str | None = None,
        attachment_ids: list[int] | None = None,
        stopping: bool = False,
        paused_ok: bool = False,
        author_name: str | None = None,
        parts: list[dict] | None = None,
    ) -> Run:
        """``paused_ok``: a turn for a paused agent is kept: it waits in the queue until the agent is enabled again."""
        if not agent.enabled and not stopping and not paused_ok:
            raise ServiceError(f"Agent '{agent.name}' is disabled")
        run = Run(
            project_id=agent.project_id,
            agent_id=agent.id,
            trigger=trigger,
            input_text=text,
            task_id=task_id,
            assignment_id=assignment_id,
            schedule_id=schedule_id,
            source_agent_id=source_agent_id,
            parent_run_id=parent_run_id,
            effort=effort,
            attachment_ids=attachment_ids or [],
            stop_requested_at=utcnow() if stopping else None,
            author_name=author_name,
            parts=parts,
        )
        s.add(run)
        await s.commit()
        bus.publish(agent.project_id, "run.upserted", RunOut.model_validate(run))
        await agent_service.publish(s, agent)
        self._wake.set()
        return run

    def recheck(self) -> None:
        """Look at the waiting turns again, e.g. after the number of parallel agents changed."""
        self._wake.set()

    async def chat(
        self, s: AsyncSession, agent: Agent, text: str, attachment_ids: list[int] | None = None, *, author: str | None = None, several: bool = False
    ) -> Run:
        """``author``: the person who writes; the chat shows the name and picture with the message.
        ``several``: several people work in the project (``users.author``). Then the name also stands
        in front of the text, which is what the agent reads: it has to tell them apart."""
        if attachment_ids and len(await board_service.chat_attachments(s, agent.project_id, attachment_ids)) != len(set(attachment_ids)):
            raise ServiceError("Unknown attachment")
        part = {"id": 1, "author": author, "text": text, "named": bool(author and several and text.strip()), "attachment_ids": list(attachment_ids or []), "at": _iso(utcnow())}
        text = _part_input(part)
        if part["text"].strip() or attachment_ids:
            await self.hold_for_message(s, agent, author or "the user", part["text"])  # before its turn can start: that turn reads it
        async with self._chat_locks.setdefault(agent.id, asyncio.Lock()):
            await s.commit()  # what is read next is the latest state, not that of a transaction begun before the wait
            run = await self._join_waiting_chat(s, agent, part) or await self.enqueue(
                s, agent, RunTrigger.CHAT, text, attachment_ids=attachment_ids, author_name=author, parts=[part]
            )
        # A working agent reads the message after its current step instead of after its whole turn.
        # Nothing is interrupted, and the message stays queued as its next turn.
        busy_run = await s.scalar(select(Run.id).where(Run.agent_id == agent.id, Run.status == RunStatus.RUNNING).limit(1))
        if run.read_at is not None:  # more was added to a message the agent had read: it has not read this part
            run.read_at = None
            await s.commit()
            bus.publish(agent.project_id, "run.upserted", RunOut.model_validate(run))
        if busy_run and (text.strip() or attachment_ids):
            if await self.runners.of(agent.project_id).notify_run(busy_run, prompts.chat_notice(text, len(attachment_ids or []))):
                self._unread.setdefault(busy_run, set()).add(run.id)  # it reads it after the step it is in
            if ended := self._end_waits(busy_run, "the user wrote to you"):  # no wait is worth more than the user
                await flow_log.record(s, agent.project_id, "wait.ended", agent_id=agent.id, run_id=busy_run, waits=ended, by="user")
        return run

    async def _wait_in_turn(self, run_id: int | None, kind: str, future: asyncio.Future[str], seconds: float) -> str:
        """Wait in a turn for a result. Raises TimeoutError, or WaitEnded when someone needs the agent now.
        Either way the future is left alone: what the turn waited for goes on."""
        ended: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        waits = self._turn_waits.setdefault(run_id, []) if run_id is not None else []
        waits.append((kind, ended))
        if kind == "job":
            self._wake.set()  # its slot is free while it waits: someone else can start
        try:
            await asyncio.wait({future, ended}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waits.remove((kind, ended))
            if not waits:
                self._turn_waits.pop(run_id, None)
        if future.done():
            return future.result()
        if ended.done():
            raise WaitEnded(ended.result())
        raise TimeoutError

    async def _mark_read(self, s: AsyncSession, project_id: int, run_ids: set[int]) -> None:
        """Waiting chat messages their agent has just read in its running turn."""
        now = utcnow()
        for run in await s.scalars(select(Run).where(Run.id.in_(run_ids), Run.status == RunStatus.QUEUED, Run.read_at.is_(None))):
            run.read_at = now
            await s.commit()
            bus.publish(project_id, "run.upserted", RunOut.model_validate(run))

    def waiting_for_job(self) -> set[int]:
        """Turns that do nothing but wait for a job right now. The limit is about agents that work
        (it protects the token quota), so they take no slot: another agent starts meanwhile. When
        the job ends they go on at once, which can put one more agent to work than the limit for
        the rest of that turn; nothing new starts until that is over."""
        return {run_id for run_id, waits in self._turn_waits.items() if any(kind == "job" for kind, _ in waits)}

    def _end_waits(self, run_id: int, reason: str, *, asks: bool = True) -> list[str]:
        """End what the run's turn is waiting for, so that its agent gets to the message it was just sent.
        Returns what was ended (job, ask)."""
        done = []
        for kind, ended in self._turn_waits.get(run_id, ()):
            if (asks or kind == "job") and not ended.done():
                ended.set_result(reason)
                done.append(kind)
        return done

    async def _join_waiting_chat(self, s: AsyncSession, agent: Agent, part: dict) -> Run | None:
        """Add a message to the agent's chat run that has not started yet, so it answers everything at once.
        Each message stays a part of its own (writer, text, files, time): the chat shows them one by one."""
        waiting = await s.scalar(
            select(Run)
            .where(Run.agent_id == agent.id, Run.trigger == RunTrigger.CHAT, Run.status == RunStatus.QUEUED)
            .order_by(Run.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        if waiting is None or not agent.enabled:
            return None
        parts = chat_parts(waiting)
        parts = [*parts, {**part, "id": max(p["id"] for p in parts) + 1}]
        # The status check matters: the dispatcher may start this run at any moment.
        result = await s.execute(
            update(Run)
            .where(Run.id == waiting.id, Run.status == RunStatus.QUEUED)
            .values(input_text=_chat_input(parts), attachment_ids=[*waiting.attachment_ids, *part["attachment_ids"]], parts=parts)
        )
        await s.commit()
        if not result.rowcount:
            return None
        await s.refresh(waiting)
        bus.publish(agent.project_id, "run.upserted", RunOut.model_validate(waiting))
        self._wake.set()
        return waiting

    async def withdraw_chat(self, s: AsyncSession, run_id: int, part_id: int | None = None) -> None:
        """Delete a chat message that the agent has not received yet (its run has not started).
        ``part_id``: only that one of the messages waiting for the run; the last one deletes the run."""
        run = await s.get(Run, run_id)
        if run is None:
            raise NotFound("Message not found")
        async with self._chat_locks.setdefault(run.agent_id, asyncio.Lock()):
            await s.commit()
            await s.refresh(run)  # a message may have joined meanwhile
            await self._withdraw_chat(s, run, part_id)

    async def _withdraw_chat(self, s: AsyncSession, run: Run, part_id: int | None) -> None:
        run_id = run.id
        if part_id is not None and run.parts and not any(p["id"] == part_id for p in run.parts):
            raise NotFound("Message not found")
        if part_id is not None and run.parts and len(run.parts) > 1:
            await self._withdraw_part(s, run, part_id)
            return
        project_id, agent_id, attachment_ids = run.project_id, run.agent_id, list(run.attachment_ids)
        # The status check matters: the dispatcher may start this run at any moment.
        result = await s.execute(sql_delete(Run).where(Run.id == run_id, Run.trigger == RunTrigger.CHAT, Run.status == RunStatus.QUEUED))
        await s.commit()
        if not result.rowcount:
            raise ServiceError("The agent has already received this message", 409)
        for attachment in await board_service.chat_attachments(s, project_id, attachment_ids):
            await board_service.delete_attachment(s, attachment)
        bus.publish(project_id, "run.deleted", {"id": run_id, "agent_id": agent_id})
        await agent_service.publish_by_id(s, agent_id)
        # Updates that waited for this turn still need one.
        project = await s.get(Project, project_id)
        if project and await inbox.pending_count(s, agent_id):
            await self._maybe_wake(s, project, agent_id)

    async def _withdraw_part(self, s: AsyncSession, run: Run, part_id: int) -> None:
        gone = next(p for p in run.parts or [] if p["id"] == part_id)
        rest = [p for p in run.parts or [] if p["id"] != part_id]
        result = await s.execute(
            update(Run)
            .where(Run.id == run.id, Run.trigger == RunTrigger.CHAT, Run.status == RunStatus.QUEUED)
            .values(
                parts=rest,
                input_text=_chat_input(rest),
                attachment_ids=[i for p in rest for i in p["attachment_ids"]],
                author_name=rest[0]["author"],
            )
        )
        await s.commit()
        if not result.rowcount:
            raise ServiceError("The agent has already received this message", 409)
        for attachment in await board_service.chat_attachments(s, run.project_id, gone["attachment_ids"]):
            await board_service.delete_attachment(s, attachment)
        await s.refresh(run)
        bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))

    async def hand_ticket(self, s: AsyncSession, task: Task, author: str | None = None) -> Run:
        """Give a ticket to the project manager, who plans and delegates it. ``author``: who did, where
        several people work in the project (as for ``decision_answered`` and ``user_action_done``)."""
        manager = await agent_service.get_manager(s, task.project_id)
        busy = await s.scalar(
            select(Run.id).where(Run.agent_id == manager.id, Run.task_id == task.id, Run.trigger == RunTrigger.TICKET, Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)))
        )
        if busy:
            raise ServiceError(f"Ticket #{task.number} is already with the manager")
        if task.status in (TaskStatus.BACKLOG, TaskStatus.TODO):
            await board_service.update_task(s, task, TaskUpdate(status="in_progress"))
        brief = await board_service.ticket_text(s, task)
        run = await self.enqueue(s, manager, RunTrigger.TICKET, prompts.ticket_handoff(brief, author), task_id=task.id)
        await board_service.publish_task(s, task)
        return run

    async def delegate(
        self,
        s: AsyncSession,
        caller: Agent | None,
        target: Agent,
        title: str,
        instructions: str,
        task: Task | None,
        effort: str | None = None,
        landing_id: int | None = None,
        train_id: int | None = None,
    ) -> Assignment:
        if task is not None and task.project_id != target.project_id:
            raise ServiceError("Ticket belongs to another project")
        if task is not None and task.status in (TaskStatus.BACKLOG, TaskStatus.TODO, TaskStatus.REVIEW):
            await board_service.update_task(s, task, TaskUpdate(status="in_progress"))
        assignment = Assignment(
            project_id=target.project_id,
            task_id=task.id if task else None,
            agent_id=target.id,
            created_by_agent_id=caller.id if caller else None,
            title=title,
            instructions=instructions,
            effort=effort,
            landing_id=landing_id,
            train_id=train_id,
        )
        s.add(assignment)
        await s.commit()
        from_name = caller.name if caller else "PlainBoard" if landing_id or train_id else "the user"
        run = await self.enqueue(
            s,
            target,
            RunTrigger.ASSIGNMENT,
            # A repair is about its branches, not about the ticket's history.
            prompts.assignment_prompt(assignment, from_name, task, "" if landing_id or train_id else await ticket_state.text_for(s, assignment.task_id)),
            task_id=assignment.task_id,
            assignment_id=assignment.id,
            source_agent_id=caller.id if caller else None,
            effort=effort,
            paused_ok=True,  # an assignment for a paused agent waits for it, like the ones it had before the pause
        )
        assignment.run_id = run.id
        await s.commit()
        bus.publish(target.project_id, "assignment.upserted", AssignmentOut.model_validate(assignment))
        await board_service.publish_task_id(s, assignment.task_id)
        return assignment

    async def message(self, s: AsyncSession, caller: Agent, target: Agent, text: str) -> None:
        """A message is an update for the target's next turn, not a turn of its own: several arrive
        together, and a turn that is about to start anyway brings them along."""
        if caller.id == target.id:
            raise ServiceError("You cannot message yourself")
        if not target.enabled:
            raise ServiceError(f"Agent '{target.name}' is disabled")
        await inbox.push(s, target.id, prompts.message_update(caller, text))
        await self.hold_for_message(s, target, caller.name, text)
        await self.request_wake(s, target.id)

    async def hold_for_message(self, s: AsyncSession, target: Agent, sender: str, text: str) -> list[str]:
        """A member got a message (a teammate's, a question, the user's) while branches it handed over have
        not landed: they are held until it has read the message and hands them over again or confirms
        them (#96). Its next turn says so, right after the message."""
        if target.is_manager:
            return []
        held = await landing_service.hold_for_message(s, target.id, text, f"message from {sender}", utcnow())
        if held:
            branches = [landing.branch for landing in held]
            await inbox.push(s, target.id, prompts.held_for_message(branches))
            await flow_log.record(s, target.project_id, "landing.held_for_message", agent_id=target.id, branches=branches, by=sender)
        return [landing.branch for landing in held]

    async def cancel_assignment(self, s: AsyncSession, assignment: Assignment) -> bool:
        """Cancel an assignment, also one that has no turn right now because it waits for its job."""
        if assignment.status not in (AssignmentStatus.QUEUED, AssignmentStatus.RUNNING):
            return False
        active = list(await s.scalars(select(Run.id).where(Run.assignment_id == assignment.id, Run.status.in_(ACTIVE_RUN_STATUSES))))
        for job in await job_service.running_for_assignment(s, assignment.id):
            await self.stop_job(s, job, settle=False)
        if active:  # the end of its turn closes it
            for run_id in active:
                await self.cancel_run(run_id)
            return True
        assignment.status, assignment.result, assignment.finished_at = AssignmentStatus.CANCELLED, "Cancelled", utcnow()
        await s.commit()
        bus.publish(assignment.project_id, "assignment.upserted", AssignmentOut.model_validate(assignment))
        await board_service.publish_task_id(s, assignment.task_id)
        await agent_service.publish_by_id(s, assignment.agent_id)
        self._wake.set()  # the agent's next assignment may start now
        return True

    async def _held_assignment(self, s: AsyncSession, agent_id: int, *, except_run_id: int | None = None) -> Assignment | None:
        """The agent's assignment that is open but has no turn: it waits for its job or for its next turn."""
        for assignment in await s.scalars(select(Assignment).where(Assignment.agent_id == agent_id, Assignment.status == AssignmentStatus.RUNNING).order_by(Assignment.id)):
            turns = select(Run.id).where(Run.assignment_id == assignment.id, Run.status.in_(ACTIVE_RUN_STATUSES))
            if except_run_id is not None:
                turns = turns.where(Run.id != except_run_id)
            if not await s.scalar(turns.limit(1)):
                return assignment
        return None

    async def _settle(self, s: AsyncSession, agent_id: int) -> None:
        """Make sure an open assignment that waits for nothing any more gets the turn that finishes it."""
        held = await self._held_assignment(s, agent_id)
        if held is None or await job_service.running_for_assignment(s, held.id):
            return
        if await s.scalar(select(Run.id).where(Run.agent_id == agent_id, Run.trigger == RunTrigger.WAKE, Run.status == RunStatus.QUEUED).limit(1)):
            return  # that turn takes it up
        agent = await s.get(Agent, agent_id)
        if agent and agent.enabled:
            await self.enqueue(s, agent, RunTrigger.WAKE, "", assignment_id=held.id, task_id=held.task_id)

    async def ask(self, ctx: RunContext, caller: Agent, target: Agent, question: str) -> str:
        if caller.id == target.id:
            raise ServiceError("You cannot ask yourself")
        if await self._depth(ctx.run_id) >= MAX_ASK_DEPTH:
            raise ServiceError("Too many nested questions; answer with what you know or use message_agent")
        if self._would_deadlock(caller.id, target.id):
            raise ServiceError(f"{target.name} is waiting on you (directly or indirectly). Use message_agent instead.")
        async with self._sm() as s:
            busy_run = await s.scalar(select(Run.id).where(Run.agent_id == target.id, Run.status == RunStatus.RUNNING).limit(1))
            await self.hold_for_message(s, target, caller.name, question)  # before its turn can start: that turn reads it
            run = await self.enqueue(
                s, target, RunTrigger.ASK, prompts.ask_prompt(caller, question), source_agent_id=caller.id, parent_run_id=ctx.run_id
            )
        if busy_run:
            await self.runners.of(target.project_id).notify_run(busy_run, prompts.ask_notice(caller, question))
            if ended := self._end_waits(busy_run, f"{caller.name} is waiting for your answer", asks=False):  # a job's result comes by itself
                async with self._sm() as s:
                    await flow_log.record(s, target.project_id, "wait.ended", agent_id=target.id, run_id=busy_run, waits=ended, by=caller.name)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters[run.id] = future
        self._add_wait(caller.id, target.id)
        try:
            return await self._wait_in_turn(ctx.run_id, "ask", future, ASK_BUSY_TIMEOUT_SECONDS if busy_run else ASK_TIMEOUT_SECONDS)
        except (TimeoutError, WaitEnded) as exc:
            # Nobody waits any more: the answer becomes an update.
            self._waiters.pop(run.id, None)
            self._orphan_asks.add(run.id)
            why = f"Stopped waiting for {target.name}: {exc}." if isinstance(exc, WaitEnded) else f"{target.name} has not answered yet."
            return f"{why} The answer will reach you as an update in a later turn."
        finally:
            self._remove_wait(caller.id, target.id)

    async def request_wake(self, s: AsyncSession, agent_id: int) -> None:
        """Queue one turn to process inbox updates, unless a queued run will consume them anyway."""
        if await s.scalar(select(Run.id).where(Run.agent_id == agent_id, Run.status == RunStatus.QUEUED)):
            return
        agent = await s.get(Agent, agent_id)
        if agent and agent.enabled:
            await self.enqueue(s, agent, RunTrigger.WAKE, "")

    async def cancel_run(self, run_id: int) -> bool:
        async with self._sm() as s:
            run = await s.get(Run, run_id)
            if run is None or run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
                return False
            project_id = run.project_id
            if run.status == RunStatus.QUEUED:
                run.status, run.finished_at, run.error = RunStatus.CANCELLED, utcnow(), "Cancelled before start"
                await s.commit()
                await self._after_run(s, run)
                bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))
                await agent_service.publish_by_id(s, run.agent_id)
                return True
        self._cancel_requested.add(run_id)
        await self.runners.of(project_id).cancel(run_id)
        asyncio.get_running_loop().call_later(CANCEL_GRACE_SECONDS, self._force_cancel, run_id)
        return True

    def refresh_ticket_state(self, task_id: int) -> None:
        """Rewrite where a ticket stands if enough new reports have come in. In the background: no
        agent waits for it, and a failure only means it is tried again with the next report."""
        running = self._state_tasks.get(task_id)
        if running is None or running.done():
            self._state_tasks[task_id] = asyncio.create_task(self._refresh_ticket_state(task_id), name=f"ticket-state-{task_id}")

    async def refresh_closed_tickets(self, s: AsyncSession) -> None:
        """Tickets that were moved to done after their state was last written get it written once
        more, a few per look: also the ones from before this existed. One that fails (no model to
        ask) is not asked for again within the hour."""
        if self.paused:
            return
        now = time.monotonic()
        waiting = [task_id for task_id in await ticket_state.behind_closed(s, CLOSED_STATES_LOOKED_AT) if now - self._state_tried.get(task_id, -CLOSED_STATE_RETRY_SECONDS) >= CLOSED_STATE_RETRY_SECONDS]
        for task_id in waiting[: max(0, CLOSED_STATES_AT_ONCE - len(self._state_tasks))]:
            self._state_tried[task_id] = now
            self.refresh_ticket_state(task_id)

    async def _refresh_ticket_state(self, task_id: int) -> None:
        try:
            async with self._sm() as s:
                task = await s.get(Task, task_id)
                closed = await ticket_state.closed(s, task) if task else ""
                reports = await ticket_state.due(s, task, final=bool(closed)) if task else []
                if not task or not (reports or closed):
                    return
                manager = await agent_service.get_manager(s, task.project_id)
                account = await account_service.pick(s, manager.provider, manager, model=manager.summary_model, where=self.runners.where(task.project_id))
                values = await settings_service.get_all(s)
                credentials = settings_service.credentials(values, account=account_service.fields(account))
                before = await ticket_state.text_for(s, task.id)
                names = await board_service.agent_names(s, task.project_id)
                spec = CompletionSpec(
                    provider=manager.provider,
                    model=manager.summary_model,
                    system_prompt=ticket_state.SYSTEM,
                    prompt=ticket_state.prompt(task, before, reports, names, closed),
                    credentials=credentials,
                    effort=manager.summary_effort or values["default_summary_effort"] or None,
                )
                await s.commit()  # no transaction is held while the model writes
                result = await self.runners.of(task.project_id).complete(spec)
                if not result.text.strip():
                    return
                until = reports[-1].id if reports else (await ticket_state.get(s, task.id)).until_assignment_id
                state = await ticket_state.store(s, task, result.text, until)
                self._state_tried.pop(task_id, None)  # written: only a failure is held back
                await flow_log.record(
                    s, task.project_id, "ticket.state_written", task_id=task.id, reports=len(reports), until=state.until_assignment_id, chars=len(state.text), cost_usd=result.cost_usd, closed=bool(closed)
                )  # fmt: skip
                await board_service.publish_task_id(s, task.id)
        except Exception:
            log.exception("Could not write the state of ticket %s", task_id)
        finally:
            self._state_tasks.pop(task_id, None)

    def refresh_limits(self) -> None:
        """Ask for the plan limits in the background, at most every five minutes, and only accounts
        that have not just reported them. Uses no tokens, or a handful (see services/limits.py)."""
        if self._limits_task and not self._limits_task.done() or (self._limits_asked and time.monotonic() - self._limits_asked < 300):
            return
        self._limits_asked = time.monotonic()
        self._limits_task = asyncio.create_task(self._refresh_limits(), name="limits-refresh")

    async def _refresh_limits(self) -> None:
        async with self._sm() as s:
            values = await settings_service.get_all(s)
            # An account in use reports its limits with every turn, but not its plan.
            reported, planned = await limits_service.fresh(s), await limits_service.plan_checked(s)
            # One that lists its models (account_service.LISTED) is asked every hour even while in use: new models.
            listed = await limits_service.plan_checked(s, account_service.MODELS_CHECK_SECONDS)
            accounts = [
                a
                for a in await account_service.list_accounts(s)
                if a.enabled
                and account_service.has_plan(a)
                and not (a.key in reported and a.key in (listed if a.provider in account_service.LISTED else planned) and (a.provider not in account_service.LISTED or a.models))
            ]
            found = await asyncio.gather(
                *(self.runner.limits(a.provider, settings_service.credentials(values, account=account_service.fields(a))) for a in accounts)
            )
            for account, report in zip(accounts, found):
                if report.windows:
                    await limits_service.record(s, account.key, report.windows, complete=report.complete)
                await limits_service.record_plan(s, account.key, report.plan, report.plan_size)
                if account_service.record_models(account, report.models):
                    await s.commit()
            if any(account_service.kept_login(a) for a in accounts):
                await self.keep_sign_ins(s, None)

    async def wind_down(self, s: AsyncSession, agent: Agent, reason: str = "", *, jobs_end: bool = False) -> bool:
        """Let an agent finish safely: drop what it has not started and tell its running turn to wrap up.
        An agent that only has background jobs left gets a turn for that, even if it is paused.
        A paused (disabled) agent keeps its assignments: they go on when it is enabled again (``resume``).
        ``jobs_end``: everything is paused, so its jobs are stopped for it (``_rest_jobs``).
        Returns whether the agent is still wrapping up."""
        rows = (await s.execute(select(Run.id, Run.status, Run.assignment_id).where(Run.agent_id == agent.id, Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING))))).all()
        running = False
        for run_id, status, assignment_id in rows:
            if status == RunStatus.QUEUED:
                if agent.enabled or assignment_id is None:
                    await self.cancel_run(run_id)
            elif await self.runners.of(agent.project_id).notify_run(run_id, prompts.wind_down_notice(reason, jobs_end), urgent=True):
                running = True
                run = await s.get(Run, run_id)
                run.stop_requested_at = run.stop_requested_at or utcnow()
                await s.commit()
                bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))
        if not running and (jobs := [job for job in await job_service.running(s) if job.agent_id == agent.id]):
            await self.enqueue(s, agent, RunTrigger.WAKE, prompts.wind_down_jobs_prompt(reason, [job_service.label(job) for job in jobs]), stopping=True)
            running = True
        await flow_log.record(s, agent.project_id, "agent.wound_down", agent_id=agent.id, paused=not agent.enabled, turns=len(rows), still_running=running, reason=reason)
        return running

    async def resume(self, s: AsyncSession, agent: Agent) -> None:
        """An agent was enabled again: what it had open when it was paused goes on by itself."""
        held = await self._held_assignment(s, agent.id)
        await flow_log.record(s, agent.project_id, "agent.resumed", agent_id=agent.id, assignment_id=held.id if held else None)
        if held:
            await inbox.push(s, agent.id, prompts.RESUME_NOTE)
            await self._settle(s, agent.id)
        self._wake.set()  # assignments that waited in the queue

    async def pause_all(self, s: AsyncSession, by: str) -> None:
        """Bring every project to rest (services/pause.py). Calling it again reaches what started meanwhile."""
        self.paused = True  # first: from here on nothing new starts
        reason = "PlainBoard is paused for maintenance. Your open work goes on by itself afterwards."
        members = await pause_service.begin(s, by)
        await self._rest_jobs(s)  # of everyone without a turn; a turn that still runs wraps up first
        for member in members:
            await self.wind_down(s, member, reason, jobs_end=True)
        # Managers stay switched on, and what waits for them stays in the queue. Their running turn wraps up too.
        turns = select(Run).join(Agent, Agent.id == Run.agent_id).where(Run.status == RunStatus.RUNNING, Run.stop_requested_at.is_(None), Agent.is_manager)
        for run in list(await s.scalars(turns)):
            if await self.runners.of(run.project_id).notify_run(run.id, prompts.wind_down_notice(reason, jobs_end=True), urgent=True):
                run.stop_requested_at = utcnow()
                await s.commit()
                bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))
        await self._pause_changed(s)

    async def pause_project(self, s: AsyncSession, project: Project, by: str) -> None:
        """One project comes to rest and stays there until a person resumes it. Nothing of it starts,
        whoever asks for it: the turns that run are told to wrap up, the background jobs are stopped,
        and what is queued or gets queued waits. Unlike "pause everything" it does not end by itself.
        Saved at once; telling the turns and stopping the jobs goes on in the background (``_rest_project``)."""
        project.paused_at, project.paused_by = project.paused_at or utcnow(), project.paused_by or by
        await s.commit()
        await flow_log.record(s, project.id, "project.paused", by=by)
        bus.publish(project.id, "project.updated", project_service.to_out(project))
        task = asyncio.create_task(self._rest_project(project.id, by), name=f"rest-project-{project.id}")
        self._resting.add(task)
        task.add_done_callback(self._resting.discard)

    async def _rest_project(self, project_id: int, by: str) -> None:
        """Every one of these is a call to the runner (an external one through its tunnel): together they
        took seconds, and the switch waited for them. Ends early when the project is resumed meanwhile."""
        reason = f"{by} paused this project. Your open work goes on by itself when it is resumed."
        try:
            async with self._sm() as s:
                turns = select(Run).where(Run.project_id == project_id, Run.status == RunStatus.RUNNING, Run.stop_requested_at.is_(None))
                for run in list(await s.scalars(turns)):
                    if not await self._still_paused(s, project_id):
                        return
                    if await self.runners.of(run.project_id).notify_run(run.id, prompts.wind_down_notice(reason, jobs_end=True), urgent=True):
                        run.stop_requested_at = utcnow()
                        await s.commit()
                        bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))
                if await self._still_paused(s, project_id):
                    await self._rest_jobs(s, project_id=project_id)  # of everyone without a turn; a turn that still runs wraps up first
        except Exception:
            log.exception("Pausing project %s: its turns and jobs were not all stopped", project_id)

    @staticmethod
    async def _still_paused(s: AsyncSession, project_id: int) -> bool:
        return await s.scalar(select(Project.paused_at).where(Project.id == project_id)) is not None

    async def resume_project(self, s: AsyncSession, project: Project) -> None:
        """End a project's pause: what waited starts, and open assignments go on with their next turn."""
        project.paused_at, project.paused_by = None, ""
        await s.commit()
        await flow_log.record(s, project.id, "project.resumed")
        bus.publish(project.id, "project.updated", project_service.to_out(project))
        self._wake.set()

    async def _rest_jobs(self, s: AsyncSession, agent_id: int | None = None, project_id: int | None = None) -> int:
        """Everything is paused: stop the background jobs of agents that have no turn (of one agent, or
        of all). A restart would end them anyway, and nobody should have to decide it: no turn is spent.
        The agent reads which ones with its next turn and starts again what it still needs.
        Returns how many were stopped."""
        stopped: dict[int, list[str]] = {}
        for job in await job_service.running(s):
            if (agent_id is not None and job.agent_id != agent_id) or (project_id is not None and job.project_id != project_id) or job.agent_id in self._busy_agents:
                continue
            try:
                if await self.stop_job(s, job, settle=False):
                    stopped.setdefault(job.agent_id, []).append(job_service.label(job))
            except ServiceError as exc:
                log.warning("Pause: job %s was not stopped: %s", job.id, exc)
        for owner, jobs in stopped.items():
            await inbox.push(s, owner, prompts.jobs_stopped_for_pause(jobs))
            await flow_log.record(s, (await s.get(Agent, owner)).project_id, "pause.jobs_stopped", agent_id=owner, jobs=len(jobs))
        return sum(len(jobs) for jobs in stopped.values())

    async def resume_all(self, s: AsyncSession) -> None:
        """End the pause: the members it switched off work on with what they had open, and what waited starts."""
        members = await pause_service.end(s)
        self.paused = False
        for member in members:
            await self.resume(s, member)
        self._wake.set()
        await self._pause_changed(s)

    async def _pause_changed(self, s: AsyncSession) -> None:
        for project_id in await s.scalars(select(Project.id)):
            bus.publish(project_id, "pause.changed", {"paused": self.paused})

    async def stop_agent(self, s: AsyncSession, agent: Agent) -> int:
        """Stop everything an agent is doing or about to do, at once: runs and background jobs."""
        run_ids = (await s.scalars(select(Run.id).where(Run.agent_id == agent.id, Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING))))).all()
        stopped = 0
        for run_id in run_ids:
            stopped += await self.cancel_run(run_id)
        # Also what has no turn right now: an assignment that waits for its job.
        for assignment in list(await s.scalars(select(Assignment).where(Assignment.agent_id == agent.id, Assignment.status == AssignmentStatus.RUNNING))):
            await self.cancel_assignment(s, assignment)
        for job in await job_service.running(s):
            if job.agent_id == agent.id:
                stopped += await self.stop_job(s, job, settle=False)
        return stopped

    async def check_turns(self) -> None:
        """Turns that only still run in the database: the runner does not have them any more (its
        stream broke off without an end), so no model works on them, nobody can tell them to wrap up,
        and a pause would wait for them for ever. One the runner has not listed for a minute is ended
        here, like one a restart cut off: its assignment goes on with the agent's next turn."""
        async with self._sm() as s:
            rows = (await s.execute(select(Run.id, Run.agent_id, Run.project_id).where(Run.status == RunStatus.RUNNING))).all()
        running = {run_id: agent_id for run_id, agent_id, _ in rows}
        if not running:
            self._missing.clear()
            return
        alive: set[int] = set()
        for runner_id in {self.runners.runner_id(project_id) for _, _, project_id in rows}:
            try:
                alive |= await self.runners.client(runner_id).active_runs()
            except RunnerError as exc:  # e.g. rebuilding, or an external runner away: decide when it is back
                log.info("Skipping turn check: %s", exc)
                alive |= {run_id for run_id, _, project_id in rows if self.runners.runner_id(project_id) == runner_id}
        now = time.monotonic()
        alive |= self._preparing  # the runner has not got them yet: copying a project's attachments can take minutes
        for run_id in list(self._missing):
            if run_id not in running or run_id in alive:
                del self._missing[run_id]
        for run_id in running.keys() - alive:
            since = max(self._missing.setdefault(run_id, now), self._streams.get(run_id, 0.0))  # a request still being made is not lost
            if now - since >= LOST_AFTER_SECONDS and run_id not in self._lost:
                del self._missing[run_id]
                await self._lose(run_id, running[run_id])

    async def settle_open_assignments(self) -> None:
        """No assignment stays open for good: one without a turn and without a running job gets the turn that
        finishes it, whatever path left it so."""
        async with self._sm() as s:
            for agent_id in set(await s.scalars(select(Assignment.agent_id).where(Assignment.status == AssignmentStatus.RUNNING))) - self._busy_agents:
                try:
                    await self._settle(s, agent_id)
                except Exception:
                    log.exception("Could not continue the open assignment of agent %s", agent_id)

    async def _lose(self, run_id: int, agent_id: int) -> None:
        async with self._sm() as s:
            run = await s.get(Run, run_id)
            if run is None or run.status != RunStatus.RUNNING:
                return
            await flow_log.record(s, run.project_id, "run.lost", agent_id=agent_id, run_id=run_id, assignment_id=run.assignment_id, task_id=run.task_id)
        task = self._running.get(run_id)
        if task and not task.done():
            self._lost.add(run_id)
            task.cancel()  # _execute ends it as lost
            return
        await self._finish(run_id, Outcome(error=LOST))  # no task here at all: only the row was left
        async with self._sm() as s:
            await self._settle(s, agent_id)
            await agent_service.publish_by_id(s, agent_id)
        self._wake.set()

    def _force_cancel(self, run_id: int) -> None:
        task = self._running.get(run_id)
        if task and not task.done():
            task.cancel()

    # ------------------------------------------------------------------ deadlock bookkeeping

    async def _depth(self, run_id: int) -> int:
        depth = 0
        async with self._sm() as s:
            current = await s.get(Run, run_id)
            while current and current.parent_run_id and depth <= MAX_ASK_DEPTH:
                depth += 1
                current = await s.get(Run, current.parent_run_id)
        return depth

    def _would_deadlock(self, caller_id: int, target_id: int) -> bool:
        stack, seen = [target_id], set()
        while stack:
            node = stack.pop()
            if node == caller_id:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(self._waits_for.get(node, {}))
        return False

    def _add_wait(self, caller_id: int, target_id: int) -> None:
        edges = self._waits_for.setdefault(caller_id, {})
        edges[target_id] = edges.get(target_id, 0) + 1

    def _remove_wait(self, caller_id: int, target_id: int) -> None:
        edges = self._waits_for.get(caller_id, {})
        if edges.get(target_id, 0) <= 1:
            edges.pop(target_id, None)
        else:
            edges[target_id] -= 1

    # ------------------------------------------------------------------ dispatch

    async def _dispatch_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._dispatch_once()
            except Exception:
                log.exception("Dispatcher error")

    async def _dispatch_once(self) -> None:
        async with self._sm() as s:
            queued = (await s.scalars(select(Run).where(Run.status == RunStatus.QUEUED).order_by(Run.id))).all()
            # Every project has its own limit, and counts its own members only. 0 pauses them.
            default = int(await settings_service.get(s, "max_parallel_runs")) if queued else 0
            limits = {pid: default if own is None else own for pid, own in (await s.execute(select(Project.id, Project.parallel_agents))).all()} if queued else {}
            # A person's limit of members at work holds across all the projects they own.
            owner_of, owner_limits = await permissions.caps(s) if queued else ({}, {})
            project_of = dict((await s.execute(select(Run.id, Run.project_id).where(Run.id.in_(list(self._running))))).all()) if queued and self._running else {}
            managers = set(await s.scalars(select(Agent.id).where(Agent.is_manager))) if queued else set()
            # Projects a person paused for good. Read from the database with every look: whoever queued
            # a turn for one of them (a chat, a schedule, a report), it does not start.
            resting = set(await s.scalars(select(Project.id).where(Project.paused_at.is_not(None)))) if queued else set()
            # A paused agent keeps its assignments in the queue; only its turn to wrap up still runs.
            paused = set(await s.scalars(select(Agent.id).where(~Agent.enabled))) if queued else set()
            # An agent finishes its open assignment before it starts another one.
            occupied = set(await s.scalars(select(Assignment.agent_id).where(Assignment.status == AssignmentStatus.RUNNING))) if queued else set()
            repairs = Assignment.landing_id.is_not(None) | Assignment.train_id.is_not(None)
            fixes = set(await s.scalars(select(Assignment.id).where(Assignment.status == AssignmentStatus.QUEUED, repairs))) if queued else set()

            def order(run: Run) -> tuple[int, int]:
                """Someone waiting for an answer goes first, then what the user wrote, then work that
                has been started (a job's result, a branch to repair for landing), then new work."""
                if run.parent_run_id is not None:
                    return 0, run.id
                if run.trigger == RunTrigger.CHAT:
                    return 1, run.id
                if run.trigger == RunTrigger.WAKE or run.assignment_id in fixes:
                    return 2, run.id
                return 3, run.id

            candidates = [
                (r.id, r.agent_id, r.project_id, r.parent_run_id is not None, r.agent_id in managers)
                for r in sorted(queued, key=order)
                # A repair for landing does not wait for the agent's open assignment: that one may wait for
                # exactly this landing, and the two would wait for each other for good.
                if not (r.trigger == RunTrigger.ASSIGNMENT and r.agent_id in occupied and r.assignment_id not in fixes) and not (r.agent_id in paused and r.assignment_id and not r.stop_requested_at)
                # Everything is paused: only what wraps up still starts, and the answer a turn that wraps up waits for.
                and not ((self.paused or r.project_id in resting) and not r.stop_requested_at and r.parent_run_id is None)
                # An external runner that is away: its turns wait for it in the queue. One that runs idle to
                # update itself still starts what a running turn waits for (an answer) and what wraps up.
                and (self.runners.ready(r.project_id) if r.parent_run_id is not None or r.stop_requested_at else self.runners.accepts(r.project_id))
            ]
            if candidates:
                held = await self._held_by_limits(s, [r for r in queued if r.id in {c[0] for c in candidates}])
                candidates = [c for c in candidates if c[0] not in held]
        in_use: dict[int, int] = {}  # project -> its members at work
        owner_use: dict[int, int] = {}  # owner with a limit -> members at work in their projects
        for run_id in self._running.keys() - self._unlimited - self.waiting_for_job():
            if (project_id := project_of.get(run_id)) is not None:
                in_use[project_id] = in_use.get(project_id, 0) + 1
                if (owner := owner_of.get(project_id)) is not None:
                    owner_use[owner] = owner_use.get(owner, 0) + 1
        for run_id, agent_id, project_id, nested, manager in candidates:
            if agent_id in self._busy_agents or run_id in self._running:
                continue
            # The limit is about members. A manager's turn never waits for a slot, not even at limit 0:
            # the user gets an answer, and the members' reports get read.
            unlimited = nested or manager
            owner = owner_of.get(project_id)
            if not unlimited and (in_use.get(project_id, 0) >= limits.get(project_id, 0) or (owner is not None and owner_use.get(owner, 0) >= owner_limits[owner])):
                continue
            self._busy_agents.add(agent_id)
            if unlimited:
                self._unlimited.add(run_id)
            else:
                in_use[project_id] = in_use.get(project_id, 0) + 1
                if owner is not None:
                    owner_use[owner] = owner_use.get(owner, 0) + 1
            task = self._running[run_id] = asyncio.create_task(self._execute(run_id, agent_id), name=f"run-{run_id}")
            self._run_tasks.add(task)  # also after it left _running: its end still writes (stop waits for it)
            task.add_done_callback(self._run_tasks.discard)

    async def _held_by_limits(self, s: AsyncSession, runs: list[Run]) -> set[int]:
        """Turns that wait because every subscription their agent may run on is used up: started now,
        they would only fail with the provider's limit message, and a failed report or wake-up queues
        the next turn that fails. Whoever waits for an answer (a chat, a question) is not held: they
        get that message. The rest starts when the first subscription is free again."""
        held: set[int] = set()
        until_of: dict[int, int | None] = {}
        for run in runs:
            if run.trigger == RunTrigger.CHAT or run.parent_run_id is not None or run.stop_requested_at:
                continue
            if run.agent_id not in until_of:
                agent = await s.get(Agent, run.agent_id)
                until_of[run.agent_id] = await account_service.used_up_until(s, agent) if agent else None
            if until := until_of[run.agent_id]:
                held.add(run.id)
                self._wake_at(until)
        return held

    def _wake_at(self, when: float) -> None:
        loop = asyncio.get_running_loop()
        at = loop.time() + max(1.0, when - time.time() + 5)
        if self._limit_timer and not self._limit_timer.cancelled() and loop.time() < self._limit_timer.when() <= at:
            return
        if self._limit_timer:
            self._limit_timer.cancel()
        self._limit_timer = loop.call_at(at, self._wake.set)

    # ------------------------------------------------------------------ execution

    async def _execute(self, run_id: int, agent_id: int) -> None:
        ctx: RunContext | None = None
        own_login = False
        self._preparing.add(run_id)
        try:
            prepared = await self._prepare(run_id)
            if prepared is None:
                return
            spec, ctx = prepared
            own_login = spec.credentials.login_files is not None
            await self._sync_uploads(ctx.project_id, spec.prompt)
            for attempt in range(RUNNER_TRIES):
                try:
                    self._streams[run_id] = time.monotonic()
                    self._preparing.discard(run_id)
                    outcome = await self._stream(spec, ctx)
                    break
                except RunnerUnavailable:
                    # Nothing ran there. After an update the runner comes up a moment after the app: a
                    # message sent in that moment waits for it instead of failing.
                    if attempt == RUNNER_TRIES - 1:
                        raise
                    await asyncio.sleep(RUNNER_RETRY_SECONDS)
            for _ in range(SWITCH_TRIES):
                if (switched := await self._switch_account(spec, ctx, outcome)) is None:
                    break
                spec = switched
                outcome = await self._stream(spec, ctx)
            if (outcome.empty or outcome.broke_early) and run_id not in self._cancel_requested:
                log.warning("Run %s ended without a single step (%s), it runs once more", run_id, outcome.error or "no result")
                if outcome.broke_early:
                    await asyncio.sleep(RUNNER_RETRY_SECONDS)  # the runner may still stop the first try
                outcome = await self._stream(spec, ctx)
                if outcome.empty:
                    outcome.error = EMPTY_TURN
            await self._finish(run_id, outcome)
        except asyncio.CancelledError:
            ended = Outcome(error=LOST) if run_id in self._lost else Outcome(error="Run cancelled", cancelled=True)
            await asyncio.shield(self._finish(run_id, ended))
            raise
        except Exception as exc:
            log.exception("Run %s failed", run_id)
            await self._finish(run_id, Outcome(error=str(exc)[:4000]))
        finally:
            if ctx:
                self._tokens.pop(ctx.token, None)
                if own_login:
                    self._take_sign_ins_soon(ctx.project_id)
            self._running.pop(run_id, None)
            self._unlimited.discard(run_id)
            self._cancel_requested.discard(run_id)
            self._streams.pop(run_id, None)
            self._preparing.discard(run_id)
            self._lost.discard(run_id)
            self._job_waits.pop(run_id, None)
            self._unread.pop(run_id, None)
            self._busy_agents.discard(agent_id)
            self._wake.set()
            try:
                async with self._sm() as s:
                    rests = select(Project.id).join(Agent, Agent.project_id == Project.id).where(Agent.id == agent_id, Project.paused_at.is_not(None))
                    if self.paused or await s.scalar(rests):  # its turn wrapped up: what it left running stops now
                        await self._rest_jobs(s, agent_id)
                    await self._settle(s, agent_id)
                    await agent_service.publish_by_id(s, agent_id)
            except Exception:
                log.exception("Could not publish agent state")

    async def _prepare(self, run_id: int) -> tuple[RunSpec, RunContext] | None:
        async with self._sm() as s:
            run = await s.get(Run, run_id)
            if run is None or run.status != RunStatus.QUEUED:
                return None
            agent = await s.get(Agent, run.agent_id)
            project = await s.get(Project, run.project_id)
            assert agent and project
            if not agent.enabled and not run.stop_requested_at:  # disabled while the run was waiting
                run.status, run.finished_at, run.error = RunStatus.CANCELLED, utcnow(), "Agent is disabled"
                await s.commit()
                await self._after_run(s, run)
                bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))
                return None
            # A wake-up while an assignment is open continues that assignment: its end decides whether
            # the assignment is finished, and its final message is the report then.
            held = await self._held_assignment(s, agent.id, except_run_id=run.id) if run.trigger == RunTrigger.WAKE else None
            if run.trigger == RunTrigger.WAKE and not run.input_text and not await inbox.pending_count(s, agent.id):
                if held is None or await job_service.running_for_assignment(s, held.id):
                    # Another turn that went first has already processed the updates this wake-up was for.
                    await s.delete(run)
                    await s.commit()
                    bus.publish(project.id, "run.deleted", {"id": run_id, "agent_id": agent.id})
                    return None
            if held is not None:
                run.assignment_id, run.task_id = held.id, held.task_id
            conv = await agent_service.conversation_for(s, agent.id)
            run.status, run.started_at = RunStatus.RUNNING, utcnow()
            await s.commit()
            await s.refresh(run)  # a chat message may have joined until now

            updates = await inbox.consume(s, agent.id)
            self._job_news.discard(agent.id)
            self._comment_news.pop(agent.id, None)
            files = await board_service.chat_attachments(s, project.id, run.attachment_ids) if run.attachment_ids else []
            if run.input_text or files:
                text = run.input_text
            elif held is not None and not await job_service.running_for_assignment(s, held.id):
                text = prompts.continue_prompt(held, (await timing_service.ledger(s, held)).text())
            else:
                text = prompts.wake_prompt(agent.is_manager)
            meta: dict = {"trigger": run.trigger}
            if run.task_id:
                meta["task_id"] = run.task_id
            if run.assignment_id:
                meta["assignment_id"] = run.assignment_id
            if run.source_agent_id:
                meta["source_agent_id"] = run.source_agent_id
            if run.author_name:
                meta["author"] = run.author_name
            if run.parts:
                meta["parts"] = [{k: p[k] for k in ("author", "text", "attachment_ids", "at")} for p in run.parts]
            if updates:
                meta["inbox"] = updates
            if files:
                meta["attachments"] = [{"id": a.id, "filename": a.filename, "content_type": a.content_type, "path": board_service.runner_path(a)} for a in files]
            message = Message(conversation_id=conv.id, role="user", kind="text", content=text, meta=meta, run_id=run.id)
            if run.trigger == RunTrigger.CHAT and run.read_at:
                # A busy agent read it before this turn and may have reacted in the turn it was in: the
                # chat shows it where it was read, above those replies. Otherwise it is read now.
                message.created_at = run.read_at
            s.add(message)
            assignment = await s.get(Assignment, run.assignment_id) if run.assignment_id else None
            if assignment:
                assignment.status = AssignmentStatus.RUNNING
            await s.commit()
            bus.publish(project.id, "message.created", MessageOut.model_validate(message))
            bus.publish(project.id, "run.upserted", RunOut.model_validate(run))
            if assignment:
                bus.publish(project.id, "assignment.upserted", AssignmentOut.model_validate(assignment))
            await agent_service.publish(s, agent)
            await board_service.publish_task_id(s, run.task_id)

            values = await settings_service.get_all(s)
            board = ""
            if agent.is_manager:
                limit = await project_service.parallel_limit(s, project)
                capacity = await board_service.capacity(
                    s, project.id, limit, except_run_id=run.id, waiting=self.waiting_for_job(), backlog=project.continue_with_backlog
                )
                team = [a for a in await agent_service.list_for_project(s, project.id) if a.enabled and not a.is_manager]
                open_todos = await todo_service.open_stats(s, [a.id for a in team])
                todo_counts = [(a.name, *_count_and_age(open_todos.get(a.id))) for a in team[:12]]
                checks = (
                    prompts.in_flight_block(*await board_service.in_flight(s, project.id)),
                    prompts.red_bases_block(await board_service.red_bases(s, project.id)),
                    prompts.board_check(await board_service.idle_in_progress(s, project.id)),
                    prompts.review_line(await board_service.unmarked_in_review(s, project.id)),
                    prompts.capacity_line(capacity),
                    prompts.team_todos_line(todo_counts),
                    prompts.ideas_line(await idea_service.count_open(s, project.id)),
                    timing_service.waiting_line(await timing_service.cached_overview(s, project.id, 24)),
                    prompts.language_note(project.language),
                )
                board = "\n\n".join(c for c in checks if c)
            elif run.trigger == RunTrigger.ASSIGNMENT and assignment:
                # Context belongs to the piece of work: another ticket starts without the earlier conversation.
                topic = f"task:{assignment.task_id}" if assignment.task_id else f"assignment:{assignment.id}"
                fresh = conv.session_topic != topic
                if fresh:
                    marker = await self.context.new_topic(s, conv, topic, before_id=message.id)
                    if marker:
                        bus.publish(project.id, "message.created", MessageOut.model_validate(marker))
                earlier = []
                if fresh:
                    done = select(Assignment).where(Assignment.agent_id == agent.id, Assignment.id != assignment.id, Assignment.finished_at.is_not(None))
                    earlier = list(await s.scalars(done.order_by(Assignment.id.desc()).limit(3)))
                facts = timing_service.facts_text(await timing_service.cached_overview(s, project.id, timing_service.FACTS_DAYS * 24))
                board = prompts.assignment_opening(facts, earlier, fresh)
                if fresh and assignment.task_id and (hint := await _related_hint(s, project, assignment)):
                    board = f"{board}\n\n{hint}" if board else hint
                if archived := await scratchpad_service.archived_block(s, assignment):
                    board = f"{board}\n\n{archived}" if board else archived
            subscriptions = prompts.subscriptions_line(values["timezone"], await account_service.used_up_until(s, agent), await _limit_since_answer(s, conv.id))
            full_text = prompts.with_preface(
                prompts.with_attachments(text, meta.get("attachments")), now=prompts.now_line(values["timezone"]), inbox=updates, board=board, subscriptions=subscriptions
            )
            pad = None if agent.is_manager else await scratchpad_service.current(s, agent.id, run.assignment_id)
            turn = await self.context.build_turn(
                s, project=project, agent=agent, conv=conv, text=full_text, message_id=message.id, scratchpad=pad.scratchpad if pad else None
            )
            if turn.briefing_changes:
                message.meta = {**message.meta, "briefing_update": turn.briefing_changes}
                await s.commit()
                bus.publish(project.id, "message.updated", MessageOut.model_validate(message))
            where = self.runners.where(project.id)
            account = await account_service.pick(s, agent.provider, agent, conv, fresh=turn.resume_session_id is None, where=where)
            if account is None and (why := await account_service.shut_out(s, agent.provider, agent) or await account_service.not_signed_in(s, agent.provider, agent, where)):
                raise RuntimeError(why)  # the turn fails with the reason, instead of at the provider for a missing key
            if account:
                conv.account_key, run.account_key = account.key, account.key
                await s.commit()
            credentials = await permissions.credentials(s, project, values, account_service.fields(account))
            ctx = RunContext(
                run_id=run.id,
                project_id=project.id,
                agent_id=agent.id,
                token=secrets.token_urlsafe(32),
                redact=secret_service.redactor(credentials.secrets),
                account_key=account.key if account else None,
                assignment_id=run.assignment_id,
                task_id=run.task_id,
            )
            self._tokens[ctx.token] = ctx
            spec = RunSpec(
                run_id=str(run.id),
                provider=agent.provider,
                model=agent.model,
                effort=run.effort or agent.effort or None,
                toolset=agent.toolset,  # type: ignore[arg-type]
                system_prompt=turn.system_prompt,
                prompt=turn.prompt,
                resume_session_id=turn.resume_session_id,
                max_turns=agent.max_turns,
                workspace=WorkspaceSpec(
                    path=workspace_path(project, agent),
                    repos=await repo_service.specs_for_agent(s, agent),
                    shared=bool(project.workspace_path),
                    setup_script=project.setup_script,
                ),
                credentials=credentials,
                bridge=ToolBridgeSpec(
                    base_url=get_config().internal_url, run_id=str(run.id), run_token=ctx.token, tools=registry.tool_defs_for(agent, await _features(s, project))
                ),
                mcp_servers=await mcp_service.specs_for_agent(s, agent),
                skills=await skill_service.specs_for_agent(s, agent),
            )
            return spec, ctx

    async def _events(self, spec: RunSpec, outcome: Outcome, project_id: int) -> AsyncIterator[RunEvent]:
        """The runner's events of a turn. A stream that breaks ends the turn with that error (``outcome.broken``)."""
        try:
            async for event in self.runners.of(project_id).stream_run(spec):
                yield event
        except RunnerStreamBroken as exc:
            # A turn an external runner no longer has (it restarted, e.g. for its self-update) is lost, not failed:
            # its assignment goes on in the same session (_assignment_turn_ended), like after a restart of the app.
            if exc.lost:
                log.warning("Run %s lost on its runner: %s", spec.run_id, exc)
            outcome.error, outcome.broken = (LOST if exc.lost else str(exc)), True

    async def _stream(self, spec: RunSpec, ctx: RunContext) -> Outcome:
        outcome = Outcome(resumed=spec.resume_session_id is not None)
        fresh_session = spec.resume_session_id is None
        async with self._sm() as s:
            conv = await agent_service.conversation_for(s, ctx.agent_id)
            base = {"agent_id": ctx.agent_id, "conversation_id": conv.id, "run_id": ctx.run_id}
            async for event in self._events(spec, outcome, ctx.project_id):
                if event.type == "text_delta":
                    bus.publish(ctx.project_id, "message.delta", {**base, "text": ctx.redact(event.text)})
                elif event.type == "session":
                    conv.provider_session_id = event.session_id
                    await s.commit()
                elif event.type == "assistant_text":
                    outcome.last_text = event.text
                    msg = await self._add_message(s, ctx, conv.id, role="assistant", kind="text", content=ctx.redact(event.text))
                    if mention_service.usernames(msg.content):
                        await mention_service.record(
                            s, ctx.project_id, msg.content, kind="chat", author=await mention_service.agent_name(s, ctx.agent_id), task_id=ctx.task_id, agent_id=ctx.agent_id
                        )
                elif event.type == "tool_use":
                    meta = {"name": event.name, "tool_use_id": event.id, "input": truncate(ctx.redact(json.dumps(event.input, ensure_ascii=False)), 1500)}
                    if code := tool_code.code_meta(event.name, event.input, ctx.redact):
                        meta["code"] = code
                    msg = await self._add_message(s, ctx, conv.id, role="assistant", kind="tool", content=event.name, meta=meta)
                    outcome.tool_messages[event.id] = msg.id
                elif event.type == "tool_result":
                    msg_id = outcome.tool_messages.get(event.tool_use_id)
                    if msg_id and (msg := await s.get(Message, msg_id)):
                        result, cut = tool_code.cap_text(ctx.redact(event.content), tool_code.RESULT_CAP) if msg.content == "Bash" else (ctx.redact(event.content), False)
                        msg.meta = {**msg.meta, "result": result, "is_error": event.is_error, **({"result_truncated": True} if cut else {})}
                        await s.commit()
                        bus.publish(ctx.project_id, "message.updated", MessageOut.model_validate(msg))
                    # A step ended: this is where the agent gets what was sent to it meanwhile (the runner's notices).
                    if told := self._unread.pop(ctx.run_id, None):
                        await self._mark_read(s, ctx.project_id, told)
                elif event.type == "usage":
                    if fresh_session:  # first call of a new session = fixed base context
                        conv.session_base_tokens = event.context_tokens
                        fresh_session = False
                    conv.context_tokens = event.context_tokens
                    await s.commit()
                    bus.publish(ctx.project_id, "conversation.updated", {"agent_id": ctx.agent_id, "context_tokens": event.context_tokens})
                elif event.type == "limits" and ctx.account_key:
                    await limits_service.record(s, ctx.account_key, event.windows, complete=event.complete)
                    outcome.limited = outcome.limited or any(w.status == "rejected" for w in event.windows)
                elif event.type == "compaction":
                    marker = self.context.provider_compacted(conv, event.tokens_before)
                    marker.run_id = ctx.run_id
                    s.add(marker)
                    await s.commit()
                    bus.publish(ctx.project_id, "message.created", MessageOut.model_validate(marker))
                    # The scratchpad is not part of the compacted conversation: give the agent its content again.
                    held = await scratchpad_service.current(s, ctx.agent_id, ctx.assignment_id)
                    if held and held.scratchpad:
                        await self.runners.of(ctx.project_id).notify_run(ctx.run_id, scratchpad_service.delivered(held.scratchpad))
                elif event.type == "result":
                    outcome.result = event
                elif event.type == "error":
                    outcome.error, outcome.cancelled = event.message, event.cancelled
        return outcome

    async def _switch_account(self, spec: RunSpec, ctx: RunContext, outcome: Outcome) -> RunSpec | None:
        """A turn that failed because the account's plan limit is used up (a 5-hour limit, say): the
        same turn for another account of the provider that still has room, which the following turns
        then keep. None if the turn failed otherwise or no other account has room: then it fails with
        the provider's message, which says when the limit resets."""
        failed = outcome.error or outcome.result is None or outcome.result.is_error
        if not (outcome.limited and failed) or outcome.cancelled or ctx.run_id in self._cancel_requested or not ctx.account_key:
            return None
        async with self._sm() as s:
            run, agent = await s.get(Run, ctx.run_id), await s.get(Agent, ctx.agent_id)
            if run is None or agent is None or run.status != RunStatus.RUNNING:
                return None
            conv = await agent_service.conversation_for(s, agent.id)
            account = await account_service.other_with_room(s, agent.provider, agent, conv, ctx.account_key, where=self.runners.where(agent.project_id))
            if account is None:
                return None
            log.info("Run %s: account %s is used up, the turn runs again on %s", run.id, ctx.account_key, account.key)
            conv.account_key, run.account_key, ctx.account_key = account.key, account.key, account.key
            await s.commit()
            bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))
        return spec.model_copy(update={"credentials": spec.credentials.model_copy(update=account_service.fields(account))})

    async def _add_message(self, s: AsyncSession, ctx: RunContext, conv_id: int, **fields) -> Message:
        msg = Message(conversation_id=conv_id, run_id=ctx.run_id, **fields)
        s.add(msg)
        await s.commit()
        bus.publish(ctx.project_id, "message.created", MessageOut.model_validate(msg))
        return msg

    async def _finish(self, run_id: int, outcome: Outcome) -> None:
        async with self._sm() as s:
            run = await s.get(Run, run_id)
            if run is None or run.status not in (RunStatus.RUNNING, RunStatus.QUEUED):
                return
            agent = await s.get(Agent, run.agent_id)
            conv = await s.scalar(select(Conversation).where(Conversation.agent_id == run.agent_id))
            res = outcome.result
            if outcome.cancelled or run_id in self._cancel_requested:
                run.status = RunStatus.CANCELLED
                run.error = "Cancelled"
            elif outcome.error or res is None or res.is_error:
                run.status = RunStatus.FAILED
                run.error = outcome.error or (res.error if res else None) or "Run ended without a result"
            else:
                run.status = RunStatus.SUCCEEDED
            run.result_text = (res.text if res and res.text else None) or outcome.last_text
            run.finished_at = utcnow()
            run.job_wait_seconds = int(self._job_waits.pop(run_id, 0))
            if res:
                run.input_tokens, run.output_tokens = res.input_tokens, res.output_tokens
                run.cache_read_tokens, run.cache_creation_tokens = res.cache_read_tokens, res.cache_creation_tokens
                run.cost_usd, run.num_turns, run.context_tokens = res.cost_usd, res.num_turns, res.context_tokens
                if res.session_cost_usd is not None and conv:
                    before = conv.session_cost_usd if outcome.resumed else 0.0
                    run.cost_usd = res.session_cost_usd - before if res.session_cost_usd >= before else res.session_cost_usd
                    conv.session_cost_usd = res.session_cost_usd
            if conv:
                conv.total_input_tokens += run.input_tokens + run.cache_read_tokens + run.cache_creation_tokens
                conv.total_output_tokens += run.output_tokens
                conv.total_cost_usd += run.cost_usd
                if res and res.session_id:
                    conv.provider_session_id = res.session_id
                if res and res.context_tokens:
                    conv.context_tokens = res.context_tokens
                if run.status == RunStatus.FAILED and _session_lost(run.error):
                    conv.provider_session_id = None
            await s.commit()
            if run.status != RunStatus.SUCCEEDED and conv:
                await self._add_message(
                    s,
                    RunContext(run.id, run.project_id, run.agent_id, ""),
                    conv.id,
                    role="system",
                    kind="error",
                    content=run.error or "Failed",
                    meta={"limit": True} if outcome.limited else {},
                )
            bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))
            try:
                await self._after_run(s, run)
            except Exception:
                log.exception("Post-run hook failed for run %s", run_id)
            if agent and conv and run.status == RunStatus.SUCCEEDED:
                try:
                    await self.context.maybe_compress(s, agent, conv)
                except Exception as exc:
                    log.exception("Compression failed for agent %s", agent.id)
                    await self._add_message(
                        s, RunContext(run.id, run.project_id, run.agent_id, ""), conv.id, role="system", kind="error", content=f"Context compression failed: {exc}"
                    )

    async def _after_run(self, s: AsyncSession, run: Run) -> None:
        """Route results: answers to waiting askers, reports to requesters, comments to tickets."""
        ok = run.status == RunStatus.SUCCEEDED
        text = run.result_text or ""
        agent = await s.get(Agent, run.agent_id)
        project = await s.get(Project, run.project_id)
        if agent is None or project is None:
            return
        outcome_text = truncate(text, 2000) if ok else f"failed: {run.error}"

        if run.trigger == RunTrigger.ASK:
            future = self._waiters.pop(run.id, None)
            answer = text if ok else f"{agent.name} could not answer: {run.error}"
            if future and not future.done():
                future.set_result(answer or "(no answer)")
            elif run.id in self._orphan_asks and run.source_agent_id:
                self._orphan_asks.discard(run.id)
                await inbox.push(s, run.source_agent_id, f"Late answer from {agent.name}: {truncate(answer, 2000)}")
                await self._maybe_wake(s, project, run.source_agent_id)

        elif run.assignment_id and run.trigger in (RunTrigger.ASSIGNMENT, RunTrigger.WAKE):
            assignment = await s.get(Assignment, run.assignment_id)
            if assignment and assignment.status in (AssignmentStatus.QUEUED, AssignmentStatus.RUNNING):
                await self._assignment_turn_ended(s, run, assignment, agent, project, outcome_text)

        elif run.error == LOST and agent.is_manager and not run.assignment_id:
            await self._report_interrupted(s, run)  # the manager's own turn: it hears it and goes on, as after a restart

        elif run.trigger == RunTrigger.TICKET and not ok and run.task_id:
            ticket = await s.get(Task, run.task_id)
            if ticket:
                await board_service.add_comment(s, ticket, f"Manager could not process the ticket: {run.error}", author_kind="system")

        # Any other turn's final message reaches nobody: what someone needs to know is sent explicitly
        # (message_agent), so no turn is spent on an update nobody asked for.
        await board_service.publish_task_id(s, run.task_id)

    async def _waits_for_job(self, s: AsyncSession, assignment: Assignment) -> bool:
        """Whether an assignment has a job running, or a job result or ticket comment its agent has not read yet."""
        if await job_service.running_for_assignment(s, assignment.id):
            return True
        unread = assignment.agent_id in self._job_news or assignment.id in self._comment_news.get(assignment.agent_id, ())
        return unread and bool(await inbox.pending_count(s, assignment.agent_id))

    async def _assignment_turn_ended(self, s: AsyncSession, run: Run, assignment: Assignment, agent: Agent, project: Project, outcome_text: str) -> None:
        """Close the assignment and send its one report, unless it still waits for a job of its own:
        then it stays open, and the turn that the job's result starts continues it."""
        ok = run.status == RunStatus.SUCCEEDED
        text = run.result_text or ""
        if ok and project.auto_wake_manager and await self._waits_for_job(s, assignment):
            bus.publish(project.id, "assignment.upserted", AssignmentOut.model_validate(assignment))
            return
        if ok and run.stop_requested_at and not agent.enabled:
            # Paused, not finished: the assignment stays open and goes on when the agent is enabled again.
            bus.publish(project.id, "assignment.upserted", AssignmentOut.model_validate(assignment))
            return
        if run.error == LOST:
            # Not a failure of the work, like a restart: the agent goes on in its session with its next
            # turn (_settle), or when it is enabled again. Only one that keeps breaking off goes to the manager.
            earlier = await s.scalar(select(func.count(Run.id)).where(Run.assignment_id == assignment.id, Run.error == LOST))
            if earlier <= MAX_RESTART_RESUMES:
                await inbox.push(s, agent.id, "\n".join(filter(None, (prompts.LOST_NOTE, scratchpad_service.delivered(assignment.scratchpad)))))
                bus.publish(project.id, "assignment.upserted", AssignmentOut.model_validate(assignment))
                return
        assignment.status = {
            RunStatus.SUCCEEDED: AssignmentStatus.DONE,
            RunStatus.CANCELLED: AssignmentStatus.CANCELLED,
        }.get(run.status, AssignmentStatus.FAILED)  # type: ignore[call-overload]
        assignment.result = text if ok else run.error
        assignment.finished_at = utcnow()
        await s.commit()
        timing_service.forget_cache()
        took = (await timing_service.ledger(s, assignment)).text()
        bus.publish(project.id, "assignment.upserted", AssignmentOut.model_validate(assignment))
        # The report stays with the assignment, which the ticket shows: it is not repeated as a comment.
        ticket = await s.get(Task, assignment.task_id) if assignment.task_id else None
        if ticket:
            self.refresh_ticket_state(ticket.id)
        if ok and (assignment.landing_id or assignment.train_id):
            # A repair for landing: PlainBoard goes on by itself, so it costs nobody a turn. The repair of
            # a red base branch is told to the manager with its next turn: what broke and what fixed it is
            # how it sees a pattern (the same check again and again), and what it can tell the user.
            train = await s.get(Train, assignment.train_id) if assignment.train_id else None
            if train is not None and train.direct:
                repo = await s.get(Repository, train.repository_id)
                checks = ", ".join(train.failed_checks.split("\n")[:6]) or "its checks"
                manager = await agent_service.get_manager(s, project.id)
                await inbox.push(
                    s,
                    manager.id,
                    f"{train.base} of {repo.name if repo else '?'} was red after a landing ({checks}); {agent.name} repaired it in {took} (assignment #{assignment.id}), "
                    f"and the fix lands like any branch: {truncate(text, 500)}",
                )
            return
        ref = f", ticket #{ticket.number}" if ticket else ""
        recipient = assignment.created_by_agent_id or (await agent_service.get_manager(s, project.id)).id
        if recipient != agent.id:
            still_open = (await todo_service.open_stats(s, [agent.id])).get(agent.id, (0, None))[0]
            todos_note = f" [{agent.name} still has {still_open} open task(s) on its list: check that they are still right.]" if still_open else ""
            # A member shows a file in its own chat, and the user reads the manager's: without this the file is
            # somewhere the user does not look, and the manager cannot say where.
            shown = await agent_service.files_shown(s, assignment.id)
            listed = ", ".join(f"{name} (attachment {attachment_id})" for attachment_id, name in shown[:8])
            files_note = f" [{agent.name} showed files in its own chat, which the user may not have open: {listed}. What is for the user, show in your chat: send_files attachment_ids.]" if shown else ""
            await inbox.push(
                s, recipient, f'Assignment #{assignment.id} "{assignment.title}" ({agent.name}{ref}) {assignment.status} after {took}: {outcome_text}{todos_note}{files_note}'
            )
            await self._maybe_wake(s, project, recipient)

    async def decision_answered(self, s: AsyncSession, decision: Decision, author: str | None = None) -> None:
        """Tell the agent that asked (or the manager, if it is gone) what the user decided."""
        project = await s.get(Project, decision.project_id)
        task = await s.get(Task, decision.task_id)
        if project is None or task is None:
            return
        recipient = decision.requested_by_agent_id or (await agent_service.get_manager(s, project.id)).id
        await inbox.push(s, recipient, f'{prompts.person(author)} decided on ticket #{task.number} ("{decision.question}"): {decision_service.answer_summary(decision)}')
        await self._maybe_wake(s, project, recipient)

    async def comment_added(self, s: AsyncSession, task: Task, comment: Comment) -> None:
        """A comment reaches the agents with an open assignment on its ticket (not its author), and the manager
        when the user wrote it. The assignment it arrives during stays open until its agent has read it."""
        project = await s.get(Project, task.project_id)
        if project is None:
            return
        author_agent = await s.get(Agent, comment.author_agent_id) if comment.author_agent_id else None
        author = author_agent.name if author_agent else prompts.person(comment.author_name)
        text = prompts.comment_update(author, task.number, comment.body)
        recipients: list[int] = []
        open_ = (AssignmentStatus.QUEUED, AssignmentStatus.RUNNING)
        for assignment in await s.scalars(select(Assignment).where(Assignment.task_id == task.id, Assignment.status.in_(open_)).order_by(Assignment.id)):
            if assignment.agent_id != comment.author_agent_id:
                self._comment_news.setdefault(assignment.agent_id, set()).add(assignment.id)
                if assignment.agent_id not in recipients:
                    recipients.append(assignment.agent_id)
        if comment.author_kind == "user":
            manager = await agent_service.get_manager(s, project.id)
            if manager.id not in recipients:
                recipients.append(manager.id)
        for agent_id in recipients:
            await inbox.push(s, agent_id, text)
            await self._maybe_wake(s, project, agent_id)

    async def user_action_done(self, s: AsyncSession, task: Task, action: str, note: str, author: str | None = None) -> None:
        """Tell the manager that the user did what the ticket was waiting for."""
        project = await s.get(Project, task.project_id)
        manager = await agent_service.get_manager(s, task.project_id)
        await inbox.push(s, manager.id, f'{prompts.person(author)} finished "{action}" on ticket #{task.number}' + (f": {note.strip()}" if note.strip() else "."))
        await self._maybe_wake(s, project, manager.id)

    # ------------------------------------------------------------------ background jobs

    async def start_job(self, s: AsyncSession, project: Project, agent: Agent, command: str, assignment_id: int | None = None) -> Job:
        """``assignment_id``: the assignment that needs this job's result and stays open until it ends."""
        values = await settings_service.get_all(s)
        spec = JobSpec(
            id=secrets.token_hex(8),
            command=command,
            workspace=workspace_path(project, agent),
            credentials=await permissions.credentials(s, project, values),
        )
        if not self.runners.ready(project.id):  # at once, not after the connect timeout
            raise ServiceError("Could not start the job: the runner this project runs on is not connected")
        if not self.runners.accepts(project.id):
            raise ServiceError("Could not start the job: the runner this project runs on is updating itself (a few minutes): start it in a later turn")
        try:
            state = await self.runners.of(project.id).start_job(spec)
        except RunnerError as exc:
            raise ServiceError(f"Could not start the job: {exc}") from exc
        # Only now, so that the watcher never looks for a job the runner has not accepted yet.
        return await job_service.add(s, project.id, agent.id, spec.id, command, state.log_path, assignment_id)

    async def stop_job(self, s: AsyncSession, job: Job, *, settle: bool = True, by: str = "") -> bool:
        """``by``: a person stopped it in the UI. Its agent reads that with its next turn, so it does not
        hand out the link of a server that is gone."""
        try:
            if not await self.runners.of(job.project_id).stop_job(job.runner_id):
                return False  # already over: the watcher reports it
        except RunnerError as exc:
            raise ServiceError(f"Could not stop the job: {exc}") from exc
        await s.refresh(job)
        if job.status == JobStatus.RUNNING:
            await job_service.finish(s, job, JobStatus.STOPPED)
            if settle and job.assignment_id and job.agent_id not in self._busy_agents:
                # Its assignment waited for it: the agent has to hear that it will not end by itself.
                await inbox.push(s, job.agent_id, f"{job_service.label(job)} was stopped before it ended. Log: {job.log_path}")
                await self._settle(s, job.agent_id)
            elif by:
                await inbox.push(s, job.agent_id, f"{job_service.label(job)} was stopped by {by} in PlainBoard.")
        return True

    async def wait_job(self, job_id: int, seconds: float, run_id: int | None = None) -> str | None:
        """The job's result if it ends within the time: then it is not sent as an update as well.
        Raises WaitEnded when someone needs the agent before that."""
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._job_waiters[job_id] = future
        self._job_waiter_runs[job_id] = run_id
        started = time.monotonic()
        try:
            return await self._wait_in_turn(run_id, "job", future, seconds)
        except TimeoutError:
            return None
        finally:
            if run_id is not None:  # waiting is not work: the assignment's time is split accordingly
                self._job_waits[run_id] = self._job_waits.get(run_id, 0.0) + time.monotonic() - started
            if self._job_waiters.get(job_id) is future:
                del self._job_waiters[job_id]
                self._job_waiter_runs.pop(job_id, None)

    async def job_ended(self, s: AsyncSession, job: Job, state: JobState | None) -> None:
        """Record the end of a job and tell the agent that started it. ``state`` None = job lost."""
        project = await s.get(Project, job.project_id)
        if state is None:
            await job_service.finish(s, job, JobStatus.LOST)
            text = f"{job_service.label(job)} was lost because its container restarted, and may be half done. Check its state and start it again if needed. Log: {job.log_path}"
        else:
            await job_service.finish(s, job, JobStatus.SUCCEEDED if state.exit_code == 0 else JobStatus.FAILED, state.exit_code)
            redact = secret_service.redactor(secret_service.env(await permissions.secrets(s, project)) if project else {})
            took = timing_service.duration((job.finished_at - job.created_at).total_seconds())  # the agent has no clock: say it
            text = f"{job_service.label(job)} finished with exit code {state.exit_code} after {took}. Log: {job.log_path}\nLast output:\n{redact(state.tail)}"
        timing_service.forget_cache()
        waiter = self._job_waiters.pop(job.id, None)
        waiter_run = self._job_waiter_runs.pop(job.id, None)
        if waiter and not waiter.done():
            waiter.set_result(text)
            if waiter_run is None or waiter_run in self._running:
                return  # its turn is still running and takes the result from here
            # The tool call outlived its turn (the agent's side gave up on it, e.g. a proxy's timeout): nobody
            # reads that result, and the assignment that stayed open for this job must still get its turn.
        await inbox.push(s, job.agent_id, text)
        if job.assignment_id:
            self._job_news.add(job.agent_id)
        if project:
            await self._maybe_wake(s, project, job.agent_id)
            if project.auto_wake_manager and job.agent_id not in self._busy_agents:
                await self._settle(s, job.agent_id)  # a turn already waiting may not be the one that continues the assignment

    async def _maybe_wake(self, s: AsyncSession, project: Project, agent_id: int) -> None:
        if project.auto_wake_manager:
            await self.request_wake(s, agent_id)

    # ------------------------------------------------------------------ tools

    async def execute_tool(self, ctx: RunContext, name: str, args: dict) -> ToolCallResult:
        if ctx.tools is not None and name not in ctx.tools:
            return ToolCallResult(text=f"Tool {name} is not available to you", is_error=True)
        async with self._sm() as s:
            agent = await s.get(Agent, ctx.agent_id)
            project = await s.get(Project, ctx.project_id)
            if agent is None or project is None:
                return ToolCallResult(text="Agent no longer exists", is_error=True)
            tool_ctx = registry.ToolContext(session=s, project=project, agent=agent, run=ctx, runs=self)
            return await registry.execute(name, tool_ctx, args)


def _session_lost(error: str | None) -> bool:
    text = (error or "").lower()
    return "no conversation found" in text or ("session" in text and "not found" in text)


async def _features(s: AsyncSession, project: Project) -> frozenset[str]:
    """What the project has of the tools that depend on something (``registry.tool(feature=...)``)."""
    return frozenset({registry.SEARCH}) if await search_service.enabled(s, project) else frozenset()


async def _related_hint(s: AsyncSession, project: Project, assignment: Assignment) -> str:
    """At most one line, at the start of new work on a ticket: what the project's history has that is
    very close to it. Never more than a short wait, never a failed turn."""
    try:
        task = await s.get(Task, assignment.task_id)
        if task is None:
            return ""
        text = f"{task.title}. {assignment.title}. {truncate(assignment.instructions, 800)}"
        who = {"agent_id": assignment.agent_id, "assignment_id": assignment.id, "task_id": task.id}
        return prompts.related_hint(search_service.label, await asyncio.wait_for(search_service.related(s, project, text, task.number, who), 5))
    except Exception:
        log.exception("The hint of related history failed")
        return ""

