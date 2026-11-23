"""The model behind a project's ideas (services/ideas.py): reading what a person types, sorting ideas into
topics, rearranging the topics, working a person's answer into an idea.

None of it is a turn of a team member: no queue, not counted against the members at work, no tool, nothing
in a chat. Each is one completion on the project's runner and subscriptions, with the project's model for
ideas (``Project.idea_*``; default: the provider's small model), stored as a ``Run`` without text (trigger
``ideas``, on the manager's account) for the statistics.

- Sorting runs in the background after a short pause, so that ideas that come quickly go together. If it
  fails or answers nonsense, the ideas stay unsorted (``sort_state`` failed).
- After a few newly sorted ideas, or when the sorting was unsure, the topics are rearranged (also in the
  background; ``reorder`` for the button). Each rearrangement is an undoable change.
- Reading an input and an answer happen while the person waits: a few seconds of a small model.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from plainboard_protocol import CompletionResult, CompletionSpec, ProviderInfo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Agent, Idea, IdeaChange, IdeaStatus, Project, Run, RunStatus, RunTrigger, utcnow
from ..services import accounts as account_service
from ..services import agents as agent_service
from ..services import ideas as idea_service
from ..services import settings as settings_service
from ..services.util import truncate
from . import questions

if TYPE_CHECKING:
    from .run_manager import RunManager

log = logging.getLogger(__name__)

DEFAULT_PROVIDER = "claude_code"
SORT_DELAY = 3.0  # seconds to wait for more ideas before sorting
SORT_BATCH = 60  # ideas sorted with one call; more wait for the next
REORDER_AFTER = 3  # newly sorted ideas after which the topics are rearranged
REORDER_MIN_IDEAS = 3  # fewer open ideas need no rearranging


class Ideas:
    def __init__(self, runs: RunManager) -> None:
        self.runs = runs
        self._sorters: dict[int, asyncio.Task[None]] = {}
        self._again: set[int] = set()  # more ideas came while sorting
        self._reorder: dict[int, str] = {}  # project -> why the topics are rearranged next: auto | button
        self._fresh: dict[int, int] = {}  # project -> ideas sorted since its topics were last rearranged

    # --- in the background ---------------------------------------------------------------------

    def sorting(self, project_id: int) -> bool:
        task = self._sorters.get(project_id)
        return task is not None and not task.done()

    def sort_soon(self, project_id: int, *, reorder: bool = False) -> None:
        """Sort what waits, after a short pause; ``reorder``: then rearrange all topics (the button)."""
        if reorder:
            self._reorder[project_id] = "button"
        if self.sorting(project_id):
            self._again.add(project_id)
            return
        self._sorters[project_id] = asyncio.create_task(self._loop(project_id), name=f"ideas-sort-{project_id}")
        bus.publish(project_id, "ideas.sorting", {"sorting": True})

    async def _loop(self, project_id: int) -> None:
        try:
            await asyncio.sleep(0 if self._reorder.get(project_id) == "button" else SORT_DELAY)
            while True:
                self._again.discard(project_id)
                more = await self._sort(project_id)
                if (source := self._reorder.pop(project_id, None)) is not None:
                    await self._rearrange(project_id, source)
                if not more and project_id not in self._again:
                    break
                if project_id in self._again:
                    await asyncio.sleep(SORT_DELAY)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Sorting the ideas of project %s failed", project_id)
        finally:
            if self._sorters.get(project_id) is asyncio.current_task():
                del self._sorters[project_id]
            bus.publish(project_id, "ideas.sorting", {"sorting": False})

    async def _sort(self, project_id: int) -> bool:
        """One call for the ideas waiting to be sorted. Returns whether more are still waiting."""
        async with self.runs._sm() as s:
            project = await s.get(Project, project_id)
            if project is None:
                return False
            ideas = await idea_service.open_ideas(s, project_id)
            batch = [i for i in ideas if i.sort_state == idea_service.PENDING][:SORT_BATCH]
            if not batch:
                return False
            ids = [i.id for i in batch]
            prompt = idea_service.sort_prompt(project, await idea_service.topics(s, project_id), ideas, batch)
            data, error = await self._ask(s, project, idea_service.SORT_SYSTEM, prompt)
        async with self.runs._sm() as s:
            if data is None:
                await idea_service.sort_failed(s, project_id, ids)
                bus.publish(project_id, "ideas.sort_failed", {"error": truncate(error, 300)})
                return False  # no retry loop over a broken model: "rearrange" retries
            count, unsure = await idea_service.apply_sort(s, project_id, ids, data)
            self._fresh[project_id] = self._fresh.get(project_id, 0) + count
            if (unsure or self._fresh[project_id] >= REORDER_AFTER) and len(ideas) >= REORDER_MIN_IDEAS:
                self._reorder.setdefault(project_id, "auto")
            left = (await s.scalars(select(Idea.id).where(Idea.project_id == project_id, Idea.status == IdeaStatus.OPEN, Idea.sort_state == idea_service.PENDING))).first()
            return left is not None

    async def _rearrange(self, project_id: int, source: str) -> IdeaChange | None:
        self._fresh[project_id] = 0
        async with self.runs._sm() as s:
            project = await s.get(Project, project_id)
            ideas = await idea_service.open_ideas(s, project_id) if project else []
            if not ideas:
                return None
            prompt = idea_service.reorder_prompt(project, await idea_service.topics(s, project_id), ideas)
            data, error = await self._ask(s, project, idea_service.REORDER_SYSTEM, prompt)
        if data is None:
            bus.publish(project_id, "ideas.sort_failed", {"error": truncate(error, 300)})
            return None
        async with self.runs._sm() as s:
            return await idea_service.apply_reorder(s, project_id, data, source)

    # --- while the person waits ----------------------------------------------------------------

    async def read_input(self, s: AsyncSession, project: Project, text: str, author: str) -> IdeaChange:
        """What the person typed, as the model reads it: a new idea, an addition, several, a merge, a question."""
        text = text.strip()
        prompt = await idea_service.intent_prompt(s, project, text)
        data, _error = await self._ask(s, project, idea_service.INTENT_SYSTEM, prompt)
        change = await idea_service.apply_intent(s, project.id, data, text, author)
        self.sort_soon(project.id)
        return change

    async def answer(self, s: AsyncSession, project: Project, idea: Idea, option_index: int | None, text: str, author: str) -> IdeaChange:
        """The person's answer to the model's question about an idea, worked into it by the model."""
        answer = await idea_service.take_answer(s, idea, option_index, text)
        prompt = await idea_service.intent_prompt(s, project, "", draft=idea, answer=answer)
        data, _error = await self._ask(s, project, idea_service.INTENT_SYSTEM, prompt)
        change = await idea_service.apply_intent(s, project.id, data, answer, author, draft=idea)
        self.sort_soon(project.id)
        return change

    # --- the call ------------------------------------------------------------------------------

    async def choice(self, s: AsyncSession, project: Project) -> tuple[str, str, str | None]:
        """Provider, model and effort for ideas: the project's own, else the provider's small model."""
        provider = project.idea_provider or DEFAULT_PROVIDER
        model = project.idea_model
        if not model:
            catalog: list[ProviderInfo] = []
            try:
                catalog = await account_service.catalog(s, await self.runs.runner.providers())
            except Exception:
                catalog = []
            model = questions.default_model(provider, catalog)
        return provider, model, project.idea_effort or None

    async def _ask(self, s: AsyncSession, project: Project, system: str, prompt: str) -> tuple[dict | None, str]:
        """The model's JSON answer, or None and why not. ``s`` is committed before the call; the cost is stored."""
        provider, model, effort = await self.choice(s, project)
        manager = await agent_service.get_manager(s, project.id)
        where = self.runs.runners.where(project.id)
        account = await account_service.pick(s, provider, manager, None, model=model, fresh=True, where=where)
        why = ""
        if account is None:
            why = await account_service.shut_out(s, provider, manager) or await account_service.not_signed_in(s, provider, manager, where)
        values = await settings_service.get_all(s)
        credentials = settings_service.credentials(values, account=account_service.fields(account))
        account_key = account.key if account else None
        await s.commit()  # nothing is held while the model answers
        result: CompletionResult | None = None
        error = why
        if not why:
            spec = CompletionSpec(provider=provider, model=model, system_prompt=system, prompt=prompt, credentials=credentials, effort=effort)
            try:
                result = await self.runs.runners.of(project.id).complete(spec)
            except Exception as exc:
                log.warning("Ideas of project %s: the model failed: %s", project.id, exc)
                error = str(exc) or type(exc).__name__
        data = None
        if result is not None:
            try:
                data = idea_service.parse_json(result.text)
            except ValueError:
                error = "The answer held no JSON"
                log.warning("Ideas of project %s: no JSON in %r", project.id, truncate(result.text, 300))
        await self._record(s, project.id, manager, account_key, result, error)
        return data, error

    async def _record(self, s: AsyncSession, project_id: int, manager: Agent, account_key: str | None, result: CompletionResult | None, error: str) -> None:
        """What it cost, for the statistics: a run of the manager's that holds no text."""
        now = utcnow()
        s.add(
            Run(
                project_id=project_id,
                agent_id=manager.id,
                trigger=RunTrigger.IDEAS,
                status=RunStatus.FAILED if error else RunStatus.SUCCEEDED,
                input_text="",
                error=truncate(error, 500) if error else None,
                input_tokens=result.input_tokens if result else 0,
                output_tokens=result.output_tokens if result else 0,
                cost_usd=result.cost_usd if result else 0.0,
                num_turns=1 if result else 0,
                account_key=account_key,
                created_at=now,
                started_at=now,
                finished_at=now,
            )
        )
        await s.commit()
