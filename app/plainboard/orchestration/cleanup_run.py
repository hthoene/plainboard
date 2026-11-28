"""A project's disk-space cleanup in a run of its own (#180), not in its manager's turn.

When the storage watcher (orchestration/cleanup.py) would tell a manager to clean up, this decides instead: one
completion of the project's cleanup model (``Project.cleanup_*``; default: the small model of the manager's
provider, never another one) gets the disk state and the project's candidates (only its own, as list_cleanup
shows them) and answers which to delete (only safe and busy ones: unsure ones are named to the manager, with a turn only when the disk
is above the target and the run could not free enough). The app deletes them by the same rules as delete_cleanup (only the
project's data, never what is kept) and the manager hears in its updates what went, what stayed and why,
without a turn. Nothing runs while one runs for the project, nor when nothing new can go: candidates a run
left alone are not offered again until the disk is fine again. If the model can't be used or fails, the
manager gets the notice and a turn, as before, with the reason. Stored as a ``Run`` without text (trigger
``cleanup``, on the manager's account) for the statistics.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from plainboard_protocol import CompletionResult, CompletionSpec, ProviderInfo
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Agent, Project, Run, RunStatus, RunTrigger, utcnow
from ..services import accounts as account_service
from ..services import agents as agent_service
from ..services import cleanup as cleanup_service
from ..services import settings as settings_service
from ..services import storage
from ..services.ideas import parse_json
from ..services.util import format_bytes, truncate
from . import inbox, prompts, questions

if TYPE_CHECKING:
    from .run_manager import RunManager

log = logging.getLogger(__name__)

OFFERED = ("safe", "busy")  # unsure ones are never the model's to delete: the manager hears them
MOST = 30  # candidates offered to one run, the biggest first


@dataclass
class Order:
    """What the watcher hands over: the notice the manager would have got, and the project's candidates."""

    project_id: int
    notice: str  # what the manager hears if the run can't do it
    items: list[storage.Item]
    head: str  # the disk state, one line
    need: int  # bytes to free to get a little below the target
    busy: set[int]
    unsure: list[storage.Item] = field(default_factory=list)  # named to the manager, not offered
    high: bool = False  # at or above the target: unsure ones left give the manager a turn


class CleanupRuns:
    def __init__(self, runs: RunManager) -> None:
        self.runs = runs
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._left: dict[int, set[str]] = {}  # project -> paths a run left alone: not offered again in this episode

    def running(self, project_id: int) -> bool:
        task = self._tasks.get(project_id)
        return task is not None and not task.done()

    def forget(self) -> None:
        """The disk is fine again: the next notice is decided anew."""
        self._left.clear()

    def offer(self, listing: storage.Listing, project_id: int) -> list[storage.Item]:
        """The project's candidates a run may decide on: its own, not kept, big enough, not left alone before."""
        left = self._left.get(project_id, set())
        found = [i for i in listing.visible(project_id) if i.candidate.safety in OFFERED and i.candidate.bytes >= storage.SMALL and i.candidate.path not in left]
        return found[:MOST]

    def start(self, order: Order) -> None:
        self._tasks[order.project_id] = asyncio.create_task(self._run(order), name=f"cleanup-run-{order.project_id}")

    async def wait(self) -> None:
        """Until the runs started are over (tests)."""
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await self.wait()

    # --- the model ---------------------------------------------------------------------------------

    async def choice(self, s: AsyncSession, project: Project, manager: Agent) -> tuple[str, str]:
        """Provider and model: the project's own, else the small model of the manager's provider."""
        provider = project.cleanup_provider or manager.provider or questions.DEFAULT_PROVIDER
        model = project.cleanup_model or questions.default_model(provider, await self._catalog(s))
        return provider, model

    async def _catalog(self, s: AsyncSession) -> list[ProviderInfo]:
        try:
            return await account_service.catalog(s, await self.runs.runner.providers())
        except Exception:
            return []

    async def _run(self, order: Order) -> None:
        try:
            await self._decide(order)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Cleanup run of project %s failed", order.project_id)
            async with self.runs._sm() as s:
                await self._fallback(s, order, f"it failed ({truncate(str(exc) or type(exc).__name__, 200)})")

    async def _decide(self, order: Order) -> None:
        async with self.runs._sm() as s:
            project = await s.get(Project, order.project_id)
            if project is None:
                return
            manager = await agent_service.get_manager(s, project.id)
            provider, model = await self.choice(s, project, manager)
            where = self.runs.runners.where(project.id)
            account = await account_service.pick(s, provider, manager, None, model=model, fresh=True, where=where)
            why = ""
            if account is None:  # never another model or provider than the one set: the manager hears why
                why = await account_service.shut_out(s, provider, manager) or await account_service.not_signed_in(s, provider, manager, where)
            values = await settings_service.get_all(s)
            credentials = settings_service.credentials(values, account=account_service.fields(account))
            account_key = account.key if account else None
            manager_id = manager.id
            await s.commit()  # nothing is held while the model answers
        label = f"{model} ({account_service.LABELS.get(provider, provider)})"
        result: CompletionResult | None = None
        data: dict | None = None
        error = why
        if not error:
            spec = CompletionSpec(provider=provider, model=model, system_prompt=prompts.CLEANUP_SYSTEM, prompt=self.prompt(order), credentials=credentials)
            try:
                result = await self.runs.runners.of(order.project_id).complete(spec)
                data = parse_json(result.text)
            except ValueError:
                error = "its answer held no JSON"
            except Exception as exc:
                error = str(exc) or type(exc).__name__
        async with self.runs._sm() as s:
            await self._record(s, order.project_id, manager_id, account_key, result, error)
            if error or data is None:
                await self._fallback(s, order, f"the cleanup model {label} could not be used: {truncate(error, 300)}")
                return
            await self._apply(s, order, data, label)

    def prompt(self, order: Order) -> str:
        lines = [order.head]
        if order.need:
            lines.append(f"To get a little below the target: free {format_bytes(order.need)}.")
        lines += storage.grouped(order.items, order.project_id, order.busy, OFFERED)
        return "\n".join(lines)

    async def _apply(self, s: AsyncSession, order: Order, data: dict, label: str) -> None:
        offered = {i.candidate.path: i for i in order.items}
        chosen = [p for p in dict.fromkeys(_paths(data.get("delete"))) if p in offered]
        plain = [p for p in chosen if offered[p].candidate.safety == "safe"]
        forced = [p for p in chosen if offered[p].candidate.safety != "safe"]
        project = await s.get(Project, order.project_id)
        manager = await agent_service.get_manager(s, order.project_id)
        runner = self.runs.runners.of(order.project_id)
        by = f"Cleanup run ({label})"
        freed, went, skipped = 0, [], []
        for paths, force in ((plain, False), (forced, True)):
            if paths:
                result, refused = await storage.delete(s, runner, project, manager, paths, force, by=by)
                freed += result.freed
                went += result.items
                skipped += result.skipped + refused
        why = {}
        for k in data.get("keep") or []:
            if isinstance(k, dict) and isinstance(k.get("path"), str) and k["path"] in offered:
                why[k["path"]] = truncate(str(k.get("why") or ""), 120)
        gone = {i.path for i in went}
        left = [p for p in offered if p not in gone]
        self._left.setdefault(order.project_id, set()).update(left)
        lines = [f"{order.head} A cleanup run ({label}) decided for you; nothing to do unless you see a reason."]
        if went:
            lines.append(f"Deleted {format_bytes(freed)}: " + ", ".join(f"{i.path} {format_bytes(i.bytes)}" for i in sorted(went, key=lambda i: -i.bytes)[:10]) + ".")
        else:
            lines.append("Deleted nothing.")
        if left:
            lines.append("Left: " + "; ".join(f"{p} {format_bytes(offered[p].candidate.bytes)} ({why.get(p) or offered[p].candidate.safety})" for p in left[:10]) + ".")
        if skipped:
            lines.append("Not deleted: " + "; ".join(skipped[:10]) + ".")
        if order.unsure:
            lines.append("Unsure, not deleted (may hold someone's work: delete with delete_cleanup force=true if it is only made again, or have its member clean up):")
            lines += [storage.item_line(i, order.project_id, order.busy) for i in order.unsure[:10]]
        if order.need > freed:
            lines.append(f"Still about {format_bytes(order.need - freed)} over the target: what is left needs a member (push or drop work) or the user.")
        await inbox.push(s, manager.id, "\n".join(lines))
        if order.high and order.unsure and order.need > freed:
            await self.runs.request_wake(s, manager.id)

    async def _fallback(self, s: AsyncSession, order: Order, why: str) -> None:
        """The run could not decide: the manager gets the notice and a turn, as before the cleanup runs."""
        manager = await agent_service.get_manager(s, order.project_id)
        await inbox.push(s, manager.id, f"{order.notice}\nNo cleanup run: {why}. Clean up yourself, or change the cleanup model in the project settings.")
        await self.runs.request_wake(s, manager.id)

    async def _record(self, s: AsyncSession, project_id: int, manager_id: int, account_key: str | None, result: CompletionResult | None, error: str) -> None:
        if result is None and not error:
            return
        now = utcnow()
        s.add(
            Run(
                project_id=project_id,
                agent_id=manager_id,
                trigger=RunTrigger.CLEANUP,
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


def _paths(value) -> list[str]:
    return [p.rstrip("/") or p for p in value if isinstance(p, str)] if isinstance(value, list) else []


def need(f: storage.Forecast, settings: dict) -> int:
    return cleanup_service.need(f.total, f.total - f.used, storage.limit(settings, "storage_notify_percent") - storage.CLOSE_BELOW)
