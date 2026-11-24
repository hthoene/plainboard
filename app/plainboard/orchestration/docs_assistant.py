"""A doc site's assistant for visitors (#119): a visitor's question, answered at once by a short turn of its own.

Like a person's question (questions.py) it is outside the queue and not counted against the members at work; it runs
on the project's runner and subscriptions with the site's own model (``DocSite.assistant_*``; default Claude's small
one). Unlike it, the turn has no built-in tool at all (toolset "none"): only the site's tools in
``tools/docs_assistant.py`` (its pages; with ``assistant_code`` also the code in the read checkout and the board's
public texts), and the app refuses every other tool of its token. Every text bound for the visitor passes the secret
filter (``services/docs_assistant.filter_for``); errors reach the visitor only as a friendly sentence.

A conversation (``Chat``) lives in memory for a while, bound to its site and the client's address; a follow-up goes on
in the same provider session. When a turn ends, a ``Run`` without its text is stored on the site's doc agent (trigger
``doc_assistant``): the statistics, and the site's limits per day. Limits: services/docs_assistant.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from plainboard_protocol import ProviderInfo, RunSpec, ToolBridgeSpec, WorkspaceSpec
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..models import DocRelease, DocSite, Project, Run, RunStatus, RunTrigger, utcnow
from ..services import accounts as account_service
from ..services import agents as agent_service
from ..services import docs as docs_service
from ..services import docs_assistant as assistant
from ..services import limits as limits_service
from ..services import permissions
from ..services import repositories as repo_service
from ..services import settings as settings_service
from ..services.errors import NotFound, ServiceError
from ..services.util import truncate
from ..tools import docs_assistant as assistant_tools
from ..tools import registry
from . import questions

if TYPE_CHECKING:
    from .run_manager import RunContext, RunManager

log = logging.getLogger(__name__)

DEFAULT_PROVIDER = "claude_code"
MAX_TURNS = 8  # steps of one answer: a few look-ups, then the answer
KEEP_SECONDS = 1800  # a conversation nobody asked anything for this long is forgotten
PING_SECONDS = 15.0
SEND_SECONDS = 0.2  # the answer so far goes out at most this often
FOLDER = ".docs-assistant"  # the empty workspace of a site without the code switch
MAX_CHATS = 5000


@dataclass
class Chat:
    """A visitor's conversation with a site's assistant; ``RunContext.doc_chat`` of its turns."""

    id: str
    site_id: int
    project_id: int
    client: str
    lang: str
    base: str  # the address of the version the visitor reads
    code: bool
    provider: str
    model: str
    effort: str | None
    session_id: str | None = None
    session_cost: float = 0.0
    questions: int = 0
    busy: bool = False
    used: float = field(default_factory=time.monotonic)
    seen: dict[str, str] = field(default_factory=dict)  # url -> title of the pages it was given this turn
    release_id: int | None = None  # the version of the site the visitor reads (#126): what it answers from


class DocsAssistant:
    def __init__(self, runs: RunManager) -> None:
        self.runs = runs
        self._chats: dict[str, Chat] = {}

    def _forget_old(self) -> None:
        now = time.monotonic()
        for key, chat in list(self._chats.items()):
            if not chat.busy and now - chat.used > KEEP_SECONDS:
                del self._chats[key]

    def close(self, chat_id: str, site_id: int, client: str) -> None:
        chat = self._chats.get(chat_id)
        if chat is not None and chat.site_id == site_id and chat.client == client and not chat.busy:
            del self._chats[chat_id]

    async def _choice(self, s: AsyncSession, site: DocSite) -> tuple[str, str, str | None]:
        provider = site.assistant_provider or DEFAULT_PROVIDER
        model = site.assistant_model
        if not model:
            catalog: list[ProviderInfo] = []
            try:
                catalog = await account_service.catalog(s, await self.runs.runner.providers())
            except Exception:
                catalog = []
            model = questions.default_model(provider, catalog)
        return provider, model, site.assistant_effort or None

    async def start(
        self, s: AsyncSession, site: DocSite, client: str, text: str, chat_id: str | None, lang: str, base: str, rel: DocRelease | None = None
    ) -> tuple[Chat, RunSpec, RunContext]:
        """Everything a visitor's question needs before it streams; every limit is checked here (``ServiceError``)."""
        from .run_manager import RunContext

        if not site.assistant_enabled:
            raise NotFound("Not found")
        self._forget_old()
        text = assistant.check_question(text)
        chat = None
        if chat_id:
            chat = self._chats.get(chat_id)
            if chat is None or chat.site_id != site.id or chat.client != client:
                raise NotFound("This conversation is over. Ask again.")
            if chat.busy:
                raise ServiceError("The answer to your last question is still coming.", 409)
            if chat.questions >= assistant.MAX_QUESTIONS:
                raise ServiceError(assistant.LONG_CONVERSATION, 429)
        if sum(c.busy for c in self._chats.values() if c.site_id == site.id) >= assistant.AT_ONCE_PER_SITE or len(self._chats) >= MAX_CHATS:
            raise ServiceError(assistant.BUSY, 429)
        project = await s.get(Project, site.project_id)
        agent = await docs_service.doc_agent(s, site.id)
        if project is None or agent is None:
            raise NotFound("Not found")
        await assistant.check_site(s, site, agent)
        assistant.check_client(client)
        if chat is None:
            provider, model, effort = await self._choice(s, site)
            chat = Chat(
                id=secrets.token_urlsafe(16), site_id=site.id, project_id=project.id, client=client, lang=lang, base=base, code=site.assistant_code,
                provider=provider, model=model, effort=effort,
            )  # fmt: skip
        chat.lang, chat.base, chat.code, chat.seen = lang, base, site.assistant_code, {}
        chat.release_id = rel.id if rel is not None else None

        manager = await agent_service.get_manager(s, project.id)
        where = self.runs.runners.where(project.id)
        account = await account_service.pick(s, chat.provider, manager, None, model=chat.model, fresh=chat.session_id is None, where=where)
        if account is None and (why := await account_service.shut_out(s, chat.provider, manager) or await account_service.not_signed_in(s, chat.provider, manager, where)):
            log.warning("Doc assistant of site %s cannot run: %s", site.id, why)
            raise ServiceError(assistant.FAILED, 503)
        values = await settings_service.get_all(s)
        root = get_config().workspaces_root
        if chat.code:
            credentials = await permissions.credentials(s, project, values, account_service.fields(account))
            repos = await repo_service.list_for_project(s, project.id)
            path, shared = questions.read_folder(project)
            workspace = WorkspaceSpec(path=path, repos=repo_service.specs(repos), shared=shared)
        else:
            credentials = settings_service.credentials(values, account=account_service.fields(account))
            workspace = WorkspaceSpec(path=f"{root}/{project.slug}/{FOLDER}")
        keys = ("claude_oauth_token", "anthropic_api_key", "openai_api_key", "endpoint_key", "github_token", "git_ssh_key")
        known = {**credentials.secrets, **{k: v for k in keys if (v := getattr(credentials, k, None))}}
        shown = assistant.filter_for(known)
        credentials = credentials.model_copy(update={"secrets": {}})  # nothing runs a command: the project's secrets stay home
        pages, hits = await assistant.context_for(s, site, text, lang, base, rel)
        chat.seen.update({h["url"]: h["title"] for h in hits})
        names = assistant_tools.tools_for(chat.code)
        run_id = -1 - secrets.randbelow(2**31)
        ctx = RunContext(
            run_id=run_id, project_id=project.id, agent_id=agent.id, token=secrets.token_urlsafe(32), redact=shown,
            account_key=account.key if account else None, tools=frozenset(names), doc_chat=chat,
        )  # fmt: skip
        tools = [t.definition() for t in registry.all_tools() if t.name in names]
        spec = RunSpec(
            run_id=str(run_id),
            provider=chat.provider,
            model=chat.model,
            effort=chat.effort,
            toolset="none",
            system_prompt=assistant.system_prompt(site, project),
            prompt=assistant.question_prompt(text, lang, pages, rel.name if rel is not None and not rel.working else ""),
            resume_session_id=chat.session_id,
            max_turns=MAX_TURNS,
            workspace=workspace,
            credentials=credentials,
            bridge=ToolBridgeSpec(base_url=get_config().internal_url, run_id=str(run_id), run_token=ctx.token, tools=tools),
            env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(site.assistant_max_tokens)},
        )
        chat.busy, chat.used = True, time.monotonic()
        chat.questions += 1
        self._chats[chat.id] = chat
        return chat, spec, ctx

    async def stream(self, chat: Chat, spec: RunSpec, ctx: RunContext, max_tokens: int) -> AsyncIterator[str]:
        """The answer as lines of JSON: ``conversation`` {id}, ``text`` {text: the answer so far, replacing the last},
        ``tool`` {name: a look-up; the text before it was a step, not the answer}, ``ping``, and at the end ``done``
        {text, sources: [{title, url}], cut: bool} or ``error`` {message}."""
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        started = utcnow()
        self.runs._tokens[ctx.token] = ctx
        worker = asyncio.create_task(self._turn(chat, spec, ctx, queue, started, max_tokens * 6), name=f"doc-assistant-{spec.run_id}")
        try:
            yield json.dumps({"type": "conversation", "id": chat.id}) + "\n"
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
            if not worker.done():  # the visitor went away: the turn ends with it
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                await self.runs.runners.of(ctx.project_id).cancel(ctx.run_id)
            self.runs._tokens.pop(ctx.token, None)
            chat.busy, chat.used = False, time.monotonic()

    async def _turn(self, chat: Chat, spec: RunSpec, ctx: RunContext, queue: asyncio.Queue, started, max_chars: int) -> None:
        from .run_manager import _session_lost

        shown: Callable[[str], str] = ctx.redact
        result = None
        error: str | None = None
        cancelled = cut = False
        step = ""
        sent_at = 0.0
        try:
            async for event in self.runs.runners.of(ctx.project_id).stream_run(spec):
                if event.type == "text_delta":
                    step += event.text
                    if len(step) > max_chars:  # past the cap on its tokens: what it has is the answer
                        cut = True
                        break
                    now = time.monotonic()
                    if now - sent_at >= SEND_SECONDS:
                        sent_at = now
                        queue.put_nowait({"type": "text", "text": shown(_whole_words(step))})
                elif event.type == "tool_use":
                    step = ""
                    queue.put_nowait({"type": "tool", "name": event.name.removeprefix("mcp__plainboard__")})
                elif event.type == "session":
                    chat.session_id = event.session_id
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
            log.warning("Doc assistant turn %s failed: %s", spec.run_id, exc)
            error = str(exc) or type(exc).__name__
        finally:
            if cut:
                await self.runs.runners.of(ctx.project_id).cancel(ctx.run_id)
            elif result is not None and result.is_error and not error:
                error = result.error or "The answer failed"
            if result is None and not error and not cancelled and not cut:
                error = "The turn ended without an answer"
            if result is not None and result.session_id:
                chat.session_id = result.session_id
            if cut or (error and _session_lost(error)):
                chat.session_id = None  # a cut answer is no context to go on from
            if not cancelled:
                if error and not cut:
                    log.warning("Doc assistant of site %s: %s", chat.site_id, truncate(error, 300))
                    queue.put_nowait({"type": "error", "message": assistant.FAILED})
                else:
                    text = shown((result.text if result is not None and not cut else "") or step)
                    if cut:
                        text = text.rstrip() + " …"
                    sources = [{"title": title, "url": url} for url, title in chat.seen.items() if f"({url})" in text or f"({url}#" in text]
                    queue.put_nowait({"type": "done", "text": text, "sources": sources, "cut": cut})
                queue.put_nowait(None)
            try:
                await asyncio.shield(self._record(chat, ctx, started, result, None if cut else error, cancelled))
            except Exception:
                log.exception("Doc assistant turn %s not stored for the statistics", spec.run_id)

    async def _record(self, chat: Chat, ctx: RunContext, started, result, error: str | None, cancelled: bool) -> None:
        """What it cost: a run of the site's doc agent without text (statistics, the site's limits per day)."""
        cost = 0.0
        if result is not None:
            cost = result.cost_usd
            if result.session_cost_usd is not None:
                cost = result.session_cost_usd - chat.session_cost if result.session_cost_usd >= chat.session_cost else result.session_cost_usd
                chat.session_cost = result.session_cost_usd
        status = RunStatus.CANCELLED if cancelled else RunStatus.FAILED if error else RunStatus.SUCCEEDED
        async with self.runs._sm() as s:
            s.add(
                Run(
                    project_id=ctx.project_id, agent_id=ctx.agent_id, trigger=RunTrigger.DOC_ASSISTANT, status=status, input_text="",
                    error=truncate(error, 500) if error and not cancelled else None,
                    input_tokens=result.input_tokens if result else 0, output_tokens=result.output_tokens if result else 0,
                    cache_read_tokens=result.cache_read_tokens if result else 0, cache_creation_tokens=result.cache_creation_tokens if result else 0,
                    cost_usd=cost, num_turns=result.num_turns if result else 0, account_key=ctx.account_key,
                    created_at=started, started_at=started, finished_at=utcnow(),
                )
            )  # fmt: skip
            await s.commit()


def _whole_words(text: str) -> str:
    """Up to the last whitespace: a word still coming could be the start of a secret the filter sees only whole."""
    at = max(text.rfind(" "), text.rfind("\n"))
    return text[:at] if at >= 0 else ""
