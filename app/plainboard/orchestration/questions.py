"""A person's short question about a project (Ctrl+Enter in the chat), answered at once by a turn of its own.

The turn is outside the queue: it does not wait for the manager, nor count against the project's limit of
members at work. It runs on the project's runner and subscriptions like any turn, but only reads: the
runner gets the toolset "read" (files: Read, Glob, Grep; no shell, no editing) and only the board tools in
``TOOLS``, and the app refuses every other tool of the turn's token (``RunContext.tools``). Nothing of it
reaches the manager's chat or anyone else: the answer streams back to the person who asked, and a follow-up
goes on in the same provider session (``Thread``, in memory only). When it ends, a ``Run`` without its
text is stored for the statistics (trigger ``question``, on the manager's account of the project).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from plainboard_protocol import ProviderInfo, RunSpec, ToolBridgeSpec, WorkspaceSpec
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..models import Project, Run, RunStatus, RunTrigger, User, utcnow
from ..services import accounts as account_service
from ..services import agents as agent_service
from ..services import limits as limits_service
from ..services import permissions
from ..services import repositories as repo_service
from ..services import secrets as secret_service
from ..services import settings as settings_service
from ..services.errors import NotFound, ServiceError
from ..services.util import truncate
from ..tools import registry
from . import prompts

if TYPE_CHECKING:
    from .run_manager import RunContext, RunManager

log = logging.getLogger(__name__)

DEFAULT_PROVIDER = "claude_code"
DEFAULT_EFFORT = "high"
FALLBACK_MODEL = "claude-haiku-5-5"  # the runner's list of models is not at hand
# The board tools a question may use: each of them only reads.
TOOLS = frozenset(
    {
        "get_board_summary", "list_tasks", "get_task", "get_comments", "list_assignments", "list_runs", "list_jobs", "read_chat",
        "list_agents", "get_agent", "member_notes", "list_notes", "get_note", "list_todos", "get_todo", "list_schedules",
        "get_project", "list_repositories", "list_skills", "list_models", "search_history", "flow_check", "time_report", "list_ideas", "get_idea",
    }
)  # fmt: skip
MAX_TURNS = 40  # a question that needs more is not a short one
KEEP_SECONDS = 3600  # a conversation nobody asked anything for this long is forgotten
MAX_RUNNING = 3  # questions of one person at the same time
PING_SECONDS = 15.0  # something on the line, so that no proxy ends a long silence
FOLDER = ".ask"  # the read checkout, beside the agents' folders


@dataclass
class Thread:
    """One person's conversation of questions in a project: its provider session, for follow-ups."""

    id: str
    project_id: int
    user_id: int | None
    provider: str
    model: str
    effort: str | None
    session_id: str | None = None
    account_key: str | None = None
    session_cost: float = 0.0
    busy: bool = False
    used: float = field(default_factory=time.monotonic)


def default_model(provider: str, catalog: list[ProviderInfo]) -> str:
    """The provider's current small model: Claude Haiku for Claude."""
    found = next((p for p in catalog if p.name == provider), None)
    models = found.models if found else []
    small = [m for m in models if m.tier == "small"]
    pick = next((m for m in small if "haiku" in m.id), None) or (small[0] if small else None) or (models[-1] if models else None)
    return pick.id if pick else FALLBACK_MODEL


def read_folder(project: Project) -> tuple[str, bool]:
    """Where a question reads the project's code, and whether that folder is also used by others."""
    root = get_config().workspaces_root
    if project.workspace_path:
        return f"{root}/{project.workspace_path}", True
    return f"{root}/{project.slug}/{FOLDER}", False


class Questions:
    def __init__(self, runs: RunManager) -> None:
        self.runs = runs
        self._threads: dict[str, Thread] = {}

    def _forget_old(self) -> None:
        now = time.monotonic()
        for key, thread in list(self._threads.items()):
            if not thread.busy and now - thread.used > KEEP_SECONDS:
                del self._threads[key]

    def thread(self, thread_id: str, project_id: int, user: User | None) -> Thread:
        thread = self._threads.get(thread_id)
        if thread is None or thread.project_id != project_id or thread.user_id != (user.id if user else None):
            raise NotFound("This conversation is over. Ask again.")
        return thread

    def close(self, thread_id: str, project_id: int, user: User | None) -> None:
        thread = self._threads.get(thread_id)
        if thread is not None and thread.project_id == project_id and thread.user_id == (user.id if user else None):
            del self._threads[thread_id]

    async def choice(self, s: AsyncSession, project: Project) -> tuple[str, str, str | None]:
        """Provider, model and effort of the project's questions: its own, else Claude Haiku with effort high."""
        provider = project.ask_provider or DEFAULT_PROVIDER
        model = project.ask_model
        catalog: list[ProviderInfo] = []
        if not model or not project.ask_effort:
            try:
                catalog = await account_service.catalog(s, await self.runs.runner.providers())
            except Exception:
                catalog = []
        model = model or default_model(provider, catalog)
        effort = project.ask_effort
        if not effort:
            efforts = next((p.efforts for p in catalog if p.name == provider), None)
            effort = DEFAULT_EFFORT if efforts is None or DEFAULT_EFFORT in efforts else ""
        return provider, model, effort or None

    async def start(self, s: AsyncSession, project: Project, user: User | None, text: str, thread_id: str | None) -> tuple[Thread, RunSpec, RunContext]:
        """Everything a question needs before it streams; what keeps it from running is raised here."""
        from .run_manager import RunContext, _features

        self._forget_old()
        text = text.strip()
        if not text:
            raise ServiceError("Ask something.")
        manager = await agent_service.get_manager(s, project.id)
        if thread_id:
            thread = self.thread(thread_id, project.id, user)
            if thread.busy:
                raise ServiceError("The answer to the last question is still coming.", 409)
        else:
            provider, model, effort = await self.choice(s, project)
            thread = Thread(id=secrets.token_urlsafe(16), project_id=project.id, user_id=user.id if user else None, provider=provider, model=model, effort=effort)
        if sum(t.busy for t in self._threads.values() if t.user_id == thread.user_id) >= MAX_RUNNING:
            raise ServiceError(f"At most {MAX_RUNNING} questions at the same time.", 429)

        where = self.runs.runners.where(project.id)
        account = await account_service.pick(s, thread.provider, manager, None, model=thread.model, fresh=thread.session_id is None, where=where)
        if account is None and (why := await account_service.shut_out(s, thread.provider, manager) or await account_service.not_signed_in(s, thread.provider, manager, where)):
            raise ServiceError(why)
        values = await settings_service.get_all(s)
        credentials = await permissions.credentials(s, project, values, account_service.fields(account))
        redact = secret_service.redactor(credentials.secrets)
        credentials = credentials.model_copy(update={"secrets": {}})  # nothing runs a command: the project's secrets stay home
        repos = await repo_service.list_for_project(s, project.id)
        path, shared = read_folder(project)
        run_id = -1 - secrets.randbelow(2**31)  # no row until it ends: the runner and the token need an id of their own
        ctx = RunContext(run_id=run_id, project_id=project.id, agent_id=manager.id, token=secrets.token_urlsafe(32), redact=redact, account_key=account.key if account else None, tools=TOOLS)
        tools = [t.definition() for t in registry.tools_for(manager, await _features(s, project)) if t.name in TOOLS]
        spec = RunSpec(
            run_id=str(run_id),
            provider=thread.provider,
            model=thread.model,
            effort=thread.effort,
            toolset="read",
            system_prompt=prompts.question_system(project.name, [r.name for r in repos]),
            prompt=prompts.with_preface(text, now=prompts.now_line(values["timezone"]), inbox=[]),
            resume_session_id=thread.session_id,
            max_turns=MAX_TURNS,
            workspace=WorkspaceSpec(path=path, repos=repo_service.specs(repos), shared=shared),
            credentials=credentials,
            bridge=ToolBridgeSpec(base_url=get_config().internal_url, run_id=str(run_id), run_token=ctx.token, tools=tools),
        )
        thread.busy, thread.used, thread.account_key = True, time.monotonic(), ctx.account_key
        self._threads[thread.id] = thread
        return thread, spec, ctx

    async def stream(self, thread: Thread, spec: RunSpec, ctx: RunContext) -> AsyncIterator[str]:
        """The answer as lines of JSON: ``thread``, then ``delta`` (text so far of the current step), ``tool``
        (a look-up: the text before it was a step, not the answer), and at the end ``done`` or ``error``."""
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        started = utcnow()
        self.runs._tokens[ctx.token] = ctx
        worker = asyncio.create_task(self._turn(thread, spec, ctx, queue, started), name=f"question-{spec.run_id}")
        try:
            yield json.dumps({"type": "thread", "id": thread.id, "model": thread.model}) + "\n"
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), PING_SECONDS)
                except TimeoutError:
                    yield json.dumps({"type": "ping"}) + "\n"
                    continue
                if item is None:
                    break
                yield json.dumps(item, ensure_ascii=False) + "\n"
        finally:
            if not worker.done():  # the person closed the window or went away: the turn ends with it
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                await self.runs.runners.of(ctx.project_id).cancel(ctx.run_id)
            self.runs._tokens.pop(ctx.token, None)
            thread.busy, thread.used = False, time.monotonic()

    async def _turn(self, thread: Thread, spec: RunSpec, ctx: RunContext, queue: asyncio.Queue, started) -> None:
        from .run_manager import _session_lost

        result = None
        error: str | None = None
        cancelled = False
        last_text = ""
        try:
            async for event in self.runs.runners.of(ctx.project_id).stream_run(spec):
                if event.type == "text_delta":
                    queue.put_nowait({"type": "delta", "text": ctx.redact(event.text)})
                elif event.type == "assistant_text":
                    last_text = ctx.redact(event.text)
                elif event.type == "tool_use":
                    queue.put_nowait({"type": "tool", "name": event.name.removeprefix("mcp__plainboard__"), "detail": _detail(event.input, ctx)})
                elif event.type == "session":
                    thread.session_id = event.session_id
                elif event.type == "limits" and ctx.account_key:
                    async with self.runs._sm() as s:
                        await limits_service.record(s, ctx.account_key, event.windows, complete=event.complete)
                elif event.type == "result":
                    result = event
                elif event.type == "error":
                    error, cancelled = event.message, event.cancelled
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            log.warning("Question %s failed: %s", spec.run_id, exc)
            error = str(exc) or type(exc).__name__
        finally:
            if result is not None and result.is_error and not error:
                error = result.error or "The answer failed"
            if result is None and not error and not cancelled:
                error = "The turn ended without an answer"
            if result is not None and result.session_id:
                thread.session_id = result.session_id
            if error and _session_lost(error):
                thread.session_id = None
            if not cancelled:
                if error:
                    queue.put_nowait({"type": "error", "message": ctx.redact(error)})
                else:
                    queue.put_nowait({"type": "done", "text": ctx.redact(result.text or "") or last_text})
                queue.put_nowait(None)
            try:
                await asyncio.shield(self._record(thread, ctx, started, result, error, cancelled))
            except Exception:
                log.exception("Question %s not stored for the statistics", spec.run_id)

    async def _record(self, thread: Thread, ctx: RunContext, started, result, error: str | None, cancelled: bool) -> None:
        """What it cost, for the statistics: a run of the manager's that holds no text."""
        cost = 0.0
        if result is not None:
            cost = result.cost_usd
            if result.session_cost_usd is not None:
                cost = result.session_cost_usd - thread.session_cost if result.session_cost_usd >= thread.session_cost else result.session_cost_usd
                thread.session_cost = result.session_cost_usd
        status = RunStatus.CANCELLED if cancelled else RunStatus.FAILED if error else RunStatus.SUCCEEDED
        async with self.runs._sm() as s:
            s.add(
                Run(
                    project_id=ctx.project_id,
                    agent_id=ctx.agent_id,
                    trigger=RunTrigger.QUESTION,
                    status=status,
                    input_text="",
                    error=truncate(error, 500) if error and not cancelled else None,
                    input_tokens=result.input_tokens if result else 0,
                    output_tokens=result.output_tokens if result else 0,
                    cache_read_tokens=result.cache_read_tokens if result else 0,
                    cache_creation_tokens=result.cache_creation_tokens if result else 0,
                    cost_usd=cost,
                    num_turns=result.num_turns if result else 0,
                    account_key=ctx.account_key,
                    created_at=started,
                    started_at=started,
                    finished_at=utcnow(),
                )
            )
            await s.commit()


def _detail(args: dict[str, Any], ctx: RunContext) -> str:
    """What a look-up is about, in a few words: a file, a pattern, a ticket, a query."""
    for key in ("file_path", "pattern", "query", "number", "path", "agent", "note_id", "todo_id"):
        if (value := args.get(key)) not in (None, ""):
            return truncate(ctx.redact(str(value)), 80)
    return ""
