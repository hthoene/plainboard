"""Run orchestration: queue, dispatch, execution, inter-agent communication and run lifecycle.

Rules:
- One run per agent at a time; runs of different agents execute in parallel up to
  ``max_parallel_runs``. Nested runs (an agent waiting on ``ask_agent``) bypass that limit so
  a waiting agent can never starve the run it waits for.
- ``ask_agent`` blocks the caller; a wait-for graph between agents rejects cycles (deadlocks).
- Results of assignments/messages go to the requester's inbox; the manager is woken up once
  per batch of updates (coalesced) when the project's auto-wake is enabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field

from plainboard_protocol import JobSpec, JobState, ResultEvent, RunSpec, ToolBridgeSpec, ToolCallResult, WorkspaceSpec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import get_config
from ..events import bus
from ..models import (
    Agent,
    Assignment,
    AssignmentStatus,
    Conversation,
    Decision,
    Job,
    JobStatus,
    Message,
    Project,
    Run,
    RunStatus,
    RunTrigger,
    Task,
    TaskStatus,
    utcnow,
)
from ..schemas import AssignmentOut, MessageOut, RunOut, TaskUpdate
from ..services import agents as agent_service
from ..services import board as board_service
from ..services import decisions as decision_service
from ..services import jobs as job_service
from ..services import mcp as mcp_service
from ..services import projects as project_service
from ..services import repositories as repo_service
from ..services import limits as limits_service
from ..services import settings as settings_service
from ..services.errors import ServiceError
from ..services.util import truncate
from ..tools import registry
from . import inbox, prompts
from .context import ContextManager, workspace_path
from .runner_client import RunnerClient, RunnerError

log = logging.getLogger(__name__)

MAX_ASK_DEPTH = 3
ASK_TIMEOUT_SECONDS = 900
CANCEL_GRACE_SECONDS = 15


@dataclass
class RunContext:
    run_id: int
    project_id: int
    agent_id: int
    token: str


@dataclass
class Outcome:
    result: ResultEvent | None = None
    error: str | None = None
    cancelled: bool = False
    last_text: str | None = None
    tool_messages: dict[str, int] = field(default_factory=dict)
    resumed: bool = False


class RunManager:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runner: RunnerClient):
        self._sm = sessionmaker
        self.runner = runner
        self.context = ContextManager(runner)
        self._running: dict[int, asyncio.Task[None]] = {}
        self._nested: set[int] = set()
        self._busy_agents: set[int] = set()
        self._tokens: dict[str, RunContext] = {}
        self._waiters: dict[int, asyncio.Future[str]] = {}
        self._orphan_asks: set[int] = set()
        self._waits_for: dict[int, dict[int, int]] = {}
        self._cancel_requested: set[int] = set()
        self._wake = asyncio.Event()
        self._dispatcher: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self._recover_interrupted()
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="run-dispatcher")
        self._wake.set()

    async def _recover_interrupted(self) -> None:
        async with self._sm() as s:
            now = utcnow()
            interrupted = list((await s.scalars(select(Run).where(Run.status == RunStatus.RUNNING))).all())
            for run in interrupted:
                run.status, run.error, run.finished_at = RunStatus.FAILED, "Interrupted by a restart", now
                if run.assignment_id and (a := await s.get(Assignment, run.assignment_id)):
                    a.status, a.result, a.finished_at = AssignmentStatus.FAILED, "Interrupted by a restart", now
            for run in (await s.scalars(select(Run).where(Run.status == RunStatus.QUEUED, Run.trigger == RunTrigger.ASK))).all():
                run.status, run.error, run.finished_at = RunStatus.CANCELLED, "Asking agent is gone (restart)", now
            await s.commit()
            for run in interrupted:
                try:
                    await self._report_interrupted(s, run)
                except Exception:  # a failed report must not keep the app from starting
                    log.exception("Could not report interrupted run %s", run.id)

    async def _report_interrupted(self, s: AsyncSession, run: Run) -> None:
        """Nothing retries an interrupted run (it may be half done): tell whoever can decide."""
        agent = await s.get(Agent, run.agent_id)
        project = await s.get(Project, run.project_id)
        if agent is None or project is None:
            return
        assignment = await s.get(Assignment, run.assignment_id) if run.assignment_id else None
        if assignment:
            ticket = await s.get(Task, assignment.task_id) if assignment.task_id else None
            if ticket:
                await board_service.add_comment(s, ticket, f"Assignment #{assignment.id} ({agent.name}) was interrupted by a restart.", author_kind="system")
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
        for task in list(self._running.values()):
            task.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)

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
    ) -> Run:
        if not agent.enabled:
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
        )
        s.add(run)
        await s.commit()
        bus.publish(agent.project_id, "run.upserted", RunOut.model_validate(run))
        await agent_service.publish(s, agent)
        self._wake.set()
        return run

    async def chat(self, s: AsyncSession, agent: Agent, text: str, attachment_ids: list[int] | None = None) -> Run:
        if attachment_ids and len(await board_service.chat_attachments(s, agent.project_id, attachment_ids)) != len(set(attachment_ids)):
            raise ServiceError("Unknown attachment")
        return await self.enqueue(s, agent, RunTrigger.CHAT, text, attachment_ids=attachment_ids)

    async def hand_ticket(self, s: AsyncSession, task: Task) -> Run:
        """Give a ticket to the project manager, who plans and delegates it."""
        manager = await agent_service.get_manager(s, task.project_id)
        busy = await s.scalar(
            select(Run.id).where(Run.agent_id == manager.id, Run.task_id == task.id, Run.trigger == RunTrigger.TICKET, Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)))
        )
        if busy:
            raise ServiceError(f"Ticket #{task.number} is already with the manager")
        if task.status in (TaskStatus.BACKLOG, TaskStatus.TODO):
            await board_service.update_task(s, task, TaskUpdate(status="in_progress"))
        brief = await board_service.ticket_text(s, task)
        run = await self.enqueue(s, manager, RunTrigger.TICKET, prompts.ticket_handoff(brief), task_id=task.id)
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
    ) -> Assignment:
        if task is not None and task.project_id != target.project_id:
            raise ServiceError("Ticket belongs to another project")
        assignment = Assignment(
            project_id=target.project_id,
            task_id=task.id if task else None,
            agent_id=target.id,
            created_by_agent_id=caller.id if caller else None,
            title=title,
            instructions=instructions,
            effort=effort,
        )
        s.add(assignment)
        await s.commit()
        from_name = caller.name if caller else "the user"
        run = await self.enqueue(
            s,
            target,
            RunTrigger.ASSIGNMENT,
            prompts.assignment_prompt(assignment, from_name, task),
            task_id=assignment.task_id,
            assignment_id=assignment.id,
            source_agent_id=caller.id if caller else None,
            effort=effort,
        )
        assignment.run_id = run.id
        await s.commit()
        bus.publish(target.project_id, "assignment.upserted", AssignmentOut.model_validate(assignment))
        await board_service.publish_task_id(s, assignment.task_id)
        return assignment

    async def message(self, s: AsyncSession, caller: Agent, target: Agent, text: str) -> Run:
        if caller.id == target.id:
            raise ServiceError("You cannot message yourself")
        return await self.enqueue(s, target, RunTrigger.MESSAGE, prompts.message_prompt(caller, text), source_agent_id=caller.id)

    async def ask(self, ctx: RunContext, caller: Agent, target: Agent, question: str) -> str:
        if caller.id == target.id:
            raise ServiceError("You cannot ask yourself")
        if await self._depth(ctx.run_id) >= MAX_ASK_DEPTH:
            raise ServiceError("Too many nested questions; answer with what you know or use message_agent")
        if self._would_deadlock(caller.id, target.id):
            raise ServiceError(f"{target.name} is waiting on you (directly or indirectly). Use message_agent instead.")
        async with self._sm() as s:
            run = await self.enqueue(
                s, target, RunTrigger.ASK, prompts.ask_prompt(caller, question), source_agent_id=caller.id, parent_run_id=ctx.run_id
            )
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters[run.id] = future
        self._add_wait(caller.id, target.id)
        try:
            return await asyncio.wait_for(asyncio.shield(future), ASK_TIMEOUT_SECONDS)
        except TimeoutError:
            self._orphan_asks.add(run.id)
            return f"{target.name} has not answered yet. The answer will reach you as an update in a later turn."
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
            if run.status == RunStatus.QUEUED:
                run.status, run.finished_at, run.error = RunStatus.CANCELLED, utcnow(), "Cancelled before start"
                await s.commit()
                await self._after_run(s, run)
                bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))
                await agent_service.publish_by_id(s, run.agent_id)
                return True
        self._cancel_requested.add(run_id)
        await self.runner.cancel(run_id)
        asyncio.get_running_loop().call_later(CANCEL_GRACE_SECONDS, self._force_cancel, run_id)
        return True

    async def wind_down(self, s: AsyncSession, agent: Agent, reason: str = "") -> bool:
        """Let an agent finish safely: drop what it has not started and tell its running turn to wrap up.
        Returns whether a turn is still running."""
        rows = (await s.execute(select(Run.id, Run.status).where(Run.agent_id == agent.id, Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING))))).all()
        running = False
        for run_id, status in rows:
            if status == RunStatus.QUEUED:
                await self.cancel_run(run_id)
            else:
                running = await self.runner.notify_run(run_id, prompts.wind_down_notice(reason)) or running
        return running

    async def stop_agent(self, s: AsyncSession, agent: Agent) -> int:
        """Stop everything an agent is doing or about to do, at once: runs and background jobs."""
        run_ids = (await s.scalars(select(Run.id).where(Run.agent_id == agent.id, Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING))))).all()
        stopped = 0
        for run_id in run_ids:
            stopped += await self.cancel_run(run_id)
        for job in await job_service.running(s):
            if job.agent_id == agent.id:
                stopped += await self.stop_job(s, job)
        return stopped

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
            configured = await settings_service.get(s, "max_parallel_runs")
            max_parallel = 3 if configured is None else int(configured)  # 0 pauses new top-level runs
            queued = (await s.scalars(select(Run).where(Run.status == RunStatus.QUEUED).order_by(Run.id))).all()
            candidates = [(r.id, r.agent_id, r.parent_run_id is not None) for r in queued]
        for run_id, agent_id, nested in candidates:
            if agent_id in self._busy_agents or run_id in self._running:
                continue
            top_level = len(self._running) - len(self._nested)
            if not nested and top_level >= max_parallel:
                continue
            self._busy_agents.add(agent_id)
            if nested:
                self._nested.add(run_id)
            self._running[run_id] = asyncio.create_task(self._execute(run_id, agent_id), name=f"run-{run_id}")

    # ------------------------------------------------------------------ execution

    async def _execute(self, run_id: int, agent_id: int) -> None:
        ctx: RunContext | None = None
        try:
            prepared = await self._prepare(run_id)
            if prepared is None:
                return
            spec, ctx = prepared
            outcome = await self._stream(spec, ctx)
            await self._finish(run_id, outcome)
        except asyncio.CancelledError:
            await asyncio.shield(self._finish(run_id, Outcome(error="Run cancelled", cancelled=True)))
            raise
        except Exception as exc:
            log.exception("Run %s failed", run_id)
            await self._finish(run_id, Outcome(error=str(exc)[:4000]))
        finally:
            if ctx:
                self._tokens.pop(ctx.token, None)
            self._running.pop(run_id, None)
            self._nested.discard(run_id)
            self._cancel_requested.discard(run_id)
            self._busy_agents.discard(agent_id)
            self._wake.set()
            try:
                async with self._sm() as s:
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
            if not agent.enabled:  # disabled while the run was waiting
                run.status, run.finished_at, run.error = RunStatus.CANCELLED, utcnow(), "Agent is disabled"
                await s.commit()
                await self._after_run(s, run)
                bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))
                return None
            conv = await agent_service.conversation_for(s, agent.id)
            run.status, run.started_at = RunStatus.RUNNING, utcnow()
            await s.commit()

            updates = await inbox.consume(s, agent.id)
            files = await board_service.chat_attachments(s, project.id, run.attachment_ids) if run.attachment_ids else []
            text = run.input_text or ("" if files else prompts.wake_prompt())
            meta: dict = {"trigger": run.trigger}
            if run.task_id:
                meta["task_id"] = run.task_id
            if run.assignment_id:
                meta["assignment_id"] = run.assignment_id
            if run.source_agent_id:
                meta["source_agent_id"] = run.source_agent_id
            if updates:
                meta["inbox"] = updates
            if files:
                meta["attachments"] = [{"id": a.id, "filename": a.filename, "content_type": a.content_type, "path": board_service.runner_path(a)} for a in files]
            message = Message(conversation_id=conv.id, role="user", kind="text", content=text, meta=meta, run_id=run.id)
            s.add(message)
            if run.assignment_id and (assignment := await s.get(Assignment, run.assignment_id)):
                assignment.status = AssignmentStatus.RUNNING
            await s.commit()
            bus.publish(project.id, "message.created", MessageOut.model_validate(message))
            bus.publish(project.id, "run.upserted", RunOut.model_validate(run))
            if run.assignment_id and assignment:
                bus.publish(project.id, "assignment.upserted", AssignmentOut.model_validate(assignment))
            await agent_service.publish(s, agent)
            await board_service.publish_task_id(s, run.task_id)

            values = await settings_service.get_all(s)
            full_text = prompts.with_preface(prompts.with_attachments(text, meta.get("attachments")), now=prompts.now_line(values["timezone"]), inbox=updates)
            turn = await self.context.build_turn(s, project=project, agent=agent, conv=conv, text=full_text, message_id=message.id)
            if turn.briefing_changes:
                message.meta = {**message.meta, "briefing_update": turn.briefing_changes}
                await s.commit()
                bus.publish(project.id, "message.updated", MessageOut.model_validate(message))
            ctx = RunContext(run_id=run.id, project_id=project.id, agent_id=agent.id, token=secrets.token_urlsafe(32))
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
                credentials=settings_service.credentials(values, project_service.github_token(project)),
                bridge=ToolBridgeSpec(
                    base_url=get_config().internal_url, run_id=str(run.id), run_token=ctx.token, tools=registry.tool_defs_for(agent)
                ),
                mcp_servers=await mcp_service.specs_for_agent(s, agent),
            )
            return spec, ctx

    async def _stream(self, spec: RunSpec, ctx: RunContext) -> Outcome:
        outcome = Outcome(resumed=spec.resume_session_id is not None)
        fresh_session = spec.resume_session_id is None
        async with self._sm() as s:
            conv = await agent_service.conversation_for(s, ctx.agent_id)
            base = {"agent_id": ctx.agent_id, "conversation_id": conv.id, "run_id": ctx.run_id}
            async for event in self.runner.stream_run(spec):
                if event.type == "text_delta":
                    bus.publish(ctx.project_id, "message.delta", {**base, "text": event.text})
                elif event.type == "session":
                    conv.provider_session_id = event.session_id
                    await s.commit()
                elif event.type == "assistant_text":
                    outcome.last_text = event.text
                    await self._add_message(s, ctx, conv.id, role="assistant", kind="text", content=event.text)
                elif event.type == "tool_use":
                    meta = {"name": event.name, "tool_use_id": event.id, "input": truncate(json.dumps(event.input, ensure_ascii=False), 1500)}
                    msg = await self._add_message(s, ctx, conv.id, role="assistant", kind="tool", content=event.name, meta=meta)
                    outcome.tool_messages[event.id] = msg.id
                elif event.type == "tool_result":
                    msg_id = outcome.tool_messages.get(event.tool_use_id)
                    if msg_id and (msg := await s.get(Message, msg_id)):
                        msg.meta = {**msg.meta, "result": event.content, "is_error": event.is_error}
                        await s.commit()
                        bus.publish(ctx.project_id, "message.updated", MessageOut.model_validate(msg))
                elif event.type == "usage":
                    if fresh_session:  # first call of a new session = fixed base context
                        conv.session_base_tokens = event.context_tokens
                        fresh_session = False
                    conv.context_tokens = event.context_tokens
                    await s.commit()
                    bus.publish(ctx.project_id, "conversation.updated", {"agent_id": ctx.agent_id, "context_tokens": event.context_tokens})
                elif event.type == "limits":
                    await limits_service.record(s, event.windows)
                elif event.type == "compaction":
                    marker = self.context.provider_compacted(conv, event.tokens_before)
                    marker.run_id = ctx.run_id
                    s.add(marker)
                    await s.commit()
                    bus.publish(ctx.project_id, "message.created", MessageOut.model_validate(marker))
                elif event.type == "result":
                    outcome.result = event
                elif event.type == "error":
                    outcome.error, outcome.cancelled = event.message, event.cancelled
        return outcome

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
                    s, RunContext(run.id, run.project_id, run.agent_id, ""), conv.id, role="system", kind="error", content=run.error or "Failed"
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

        elif run.trigger == RunTrigger.MESSAGE and run.source_agent_id:
            await inbox.push(s, run.source_agent_id, f"Reply from {agent.name}: {outcome_text}")
            await self._maybe_wake(s, project, run.source_agent_id)

        elif run.trigger == RunTrigger.ASSIGNMENT and run.assignment_id:
            assignment = await s.get(Assignment, run.assignment_id)
            if assignment:
                assignment.status = {
                    RunStatus.SUCCEEDED: AssignmentStatus.DONE,
                    RunStatus.CANCELLED: AssignmentStatus.CANCELLED,
                }.get(run.status, AssignmentStatus.FAILED)  # type: ignore[call-overload]
                assignment.result = text if ok else run.error
                assignment.finished_at = utcnow()
                await s.commit()
                bus.publish(project.id, "assignment.upserted", AssignmentOut.model_validate(assignment))
                ticket = await s.get(Task, assignment.task_id) if assignment.task_id else None
                if ticket:
                    if ok:
                        await board_service.add_comment(s, ticket, f"**Assignment #{assignment.id}: {assignment.title}**\n\n{text}", author_kind="agent", agent_id=agent.id)
                    else:
                        await board_service.add_comment(s, ticket, f"Assignment #{assignment.id} ({agent.name}) {assignment.status}: {run.error}", author_kind="system")
                ref = f", ticket #{ticket.number}" if ticket else ""
                recipient = assignment.created_by_agent_id or (await agent_service.get_manager(s, project.id)).id
                if recipient != agent.id:
                    await inbox.push(s, recipient, f'Assignment #{assignment.id} "{assignment.title}" ({agent.name}{ref}) {assignment.status}: {outcome_text}')
                    await self._maybe_wake(s, project, recipient)

        elif run.trigger == RunTrigger.TICKET and not ok and run.task_id:
            ticket = await s.get(Task, run.task_id)
            if ticket:
                await board_service.add_comment(s, ticket, f"Manager could not process the ticket: {run.error}", author_kind="system")

        elif run.trigger == RunTrigger.WAKE and ok and text and not agent.is_manager:
            # e.g. a worker's job finished after its assignment was reported: nobody else reads this.
            manager = await agent_service.get_manager(s, project.id)
            await inbox.push(s, manager.id, f"Update from {agent.name}: {truncate(text, 2000)}")
            await self._maybe_wake(s, project, manager.id)

        await board_service.publish_task_id(s, run.task_id)

    async def decision_answered(self, s: AsyncSession, decision: Decision) -> None:
        """Tell the agent that asked (or the manager, if it is gone) what the user decided."""
        project = await s.get(Project, decision.project_id)
        task = await s.get(Task, decision.task_id)
        if project is None or task is None:
            return
        recipient = decision.requested_by_agent_id or (await agent_service.get_manager(s, project.id)).id
        await inbox.push(s, recipient, f'User decided on ticket #{task.number} ("{decision.question}"): {decision_service.answer_summary(decision)}')
        await self._maybe_wake(s, project, recipient)

    # ------------------------------------------------------------------ background jobs

    async def start_job(self, s: AsyncSession, project: Project, agent: Agent, command: str) -> Job:
        values = await settings_service.get_all(s)
        spec = JobSpec(
            id=secrets.token_hex(8),
            command=command,
            workspace=workspace_path(project, agent),
            credentials=settings_service.credentials(values, project_service.github_token(project)),
        )
        try:
            state = await self.runner.start_job(spec)
        except RunnerError as exc:
            raise ServiceError(f"Could not start the job: {exc}") from exc
        # Only now, so that the watcher never looks for a job the runner has not accepted yet.
        return await job_service.add(s, project.id, agent.id, spec.id, command, state.log_path)

    async def stop_job(self, s: AsyncSession, job: Job) -> bool:
        try:
            if not await self.runner.stop_job(job.runner_id):
                return False  # already over: the watcher reports it
        except RunnerError as exc:
            raise ServiceError(f"Could not stop the job: {exc}") from exc
        await s.refresh(job)
        if job.status == JobStatus.RUNNING:
            await job_service.finish(s, job, JobStatus.STOPPED)
        return True

    async def job_ended(self, s: AsyncSession, job: Job, state: JobState | None) -> None:
        """Record the end of a job and tell the agent that started it. ``state`` None = job lost."""
        project = await s.get(Project, job.project_id)
        if state is None:
            await job_service.finish(s, job, JobStatus.LOST)
            text = f"{job_service.label(job)} was lost because its container restarted, and may be half done. Check its state and start it again if needed. Log: {job.log_path}"
        else:
            await job_service.finish(s, job, JobStatus.SUCCEEDED if state.exit_code == 0 else JobStatus.FAILED, state.exit_code)
            text = f"{job_service.label(job)} finished with exit code {state.exit_code}. Log: {job.log_path}\nLast output:\n{state.tail}"
        await inbox.push(s, job.agent_id, text)
        if project:
            await self._maybe_wake(s, project, job.agent_id)

    async def _maybe_wake(self, s: AsyncSession, project: Project, agent_id: int) -> None:
        if project.auto_wake_manager:
            await self.request_wake(s, agent_id)

    # ------------------------------------------------------------------ tools

    async def execute_tool(self, ctx: RunContext, name: str, args: dict) -> ToolCallResult:
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
