"""Keeps doc sites current (#118), without a model.

After branches land, the watcher finds the doc pages the change may concern and hands them to each site's doc agent
(services/docs.ensure_agent) in one assignment per site and burst of landings (not per commit); the manager is not
involved. A doc agent that still works on one gets the next pages as an update instead.
A burst is over when nothing landed for ``QUIET`` (or ``MAX_WAIT`` after its first landing).

A page is concerned when
- a commit of the landed change touched a code path linked to it (``DocPage.paths``: prefixes or globs), or
- with semantic search, the change (branch and ticket, the report of its assignment, the commit messages and paths)
  is close in meaning to one of the page's chunks: at least ``ModelCard.hint_similarity`` less ``NEAR_MARGIN``
  (the hint's threshold is calibrated for ticket against ticket; a change against prose reads a little lower).
- a changed literal of the change (services/docs_changes.py, #190): the diff's old value of a version, number, config
  key, flag or identifier is still written on it (any page of the working version, linked or not); the line and its
  section are named, or
- a hunk or commit message of the change is close in meaning to one of its sections.
Beyond landings, the same goes for commits pushed directly (their old values, daily), a new tag of any repository
(``TAGS_EVERY``: the previous tag -> the new one and what changed between them) and, with semantic search, decisions
made and text files attached to tickets (by meaning). Nothing found: no message, no model.
Such a page is marked (``flagged_at``, ``flag_reason``) until someone edits it or confirms it is still right.
The message also sums up what landed (tickets, report excerpts, commit subjects; bounded): the terms the doc agent
searches its site for (list_docs query) to find the places nobody marked.

Once a day per project it also reads the commits of each repository that did not come through a landing (direct
pushes): they count into the pages' ``drift``. The pages' code paths follow what git saw renamed in the commits read
(a file, or a folder whose files all moved and that is empty now; a page version says so), and once a day the paths
that match no file in any repository any more are named to the doc agent with the stale pages. The doc agent hears, at most once a week per page, of pages whose
linked code changed a lot since their last edit (``DRIFT_STALE`` commits) or that stayed marked for ``STALE_DAYS``, and at
once of pages that link to a deleted page (``docs.DEAD_LINK``, marked by services/docs.delete_page).
Each look also translates what is missing or outdated in the sites' further languages (services/docs_translate.py):
at most ``docs_translate.BATCH`` pages per site and ``TRANSLATE_SECONDS`` per look; ``translate_soon`` looks at once.
Each day the doc agent also gets the pages most likely outdated within the site's ``check_budget`` (tokens of their
text, docs_changes.risky): the longest unconfirmed, weighted by the commits on their code and the releases since.
Once a site grew, the daily look asks its doc agent at most weekly to check whether a larger restructuring would help
(services/docs_proposals.py, #156): it proposes, the user decides.
Everything is bounded: commits read, texts embedded, pages named and pages translated. Only pages with linked
code paths are read for the paths; a site's other thousands of pages cost nothing here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from plainboard_protocol import CompletionResult, CompletionSpec, GitCommit, GitLogSpec, GitPresentSpec, GitTagsSpec
from sqlalchemy import String, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import (
    Account, Agent, Assignment, AssignmentStatus, Attachment, Decision, DecisionStatus, DocPage, DocPageVersion, DocSite, DocWatch, Landing, LandingStatus, Project, RepoTag,
    Repository, Run, RunStatus, RunTrigger, Task, utcnow,
)  # fmt: skip
from ..config import get_config
from ..services import accounts as account_service
from ..services import docs as docs_service
from ..services import docs_changes, docs_proposals, docs_translate
from ..services import settings as settings_service
from ..services import flow_log
from ..services import landings as landing_service
from ..services import permissions
from ..services import search as search_service
from ..services.search import indexers
from ..services.util import truncate
from . import inbox, questions
from .run_manager import RunManager
from .runner_client import RunnerError

log = logging.getLogger(__name__)

QUIET = timedelta(minutes=10)  # a burst of landings is over when nothing landed for this long
MAX_WAIT = timedelta(hours=1)  # ... or this long after its first landing
COMMITS = 200  # read at most per repository and look
TEXTS = 24  # texts of changes searched by meaning at most per look (landings, commit messages, hunks, events)
HUNKS = 8  # of a change's hunks searched by meaning
COMMIT_DIFFS = 50  # newest commits read with their changed lines
DIFF_CHARS = 8000  # of each
PROBES = 60  # old values searched in the pages per look
PLACES = 3  # lines named per page
TAGS_EVERY = timedelta(hours=3)
EVENTS = 6  # decisions and files a look
EVENT_CHARS = 1500
NEAR = 3  # nearest pages per landed change
NEAR_MARGIN = 0.05
PAGES_NAMED = 12  # in one message
CHANGES = 12  # landings summed up in one message
PATTERNS = 2000  # code paths checked per repository and day
REPORT_CHARS = 300  # of each landing's report
COMMIT_SUBJECTS = 15  # per repository
CHANGE_CHARS = 4000  # the whole summary
DAILY = timedelta(days=1)
DRIFT_STALE = 10
STALE_DAYS = 7
REMIND_EVERY = timedelta(days=7)
TRANSLATE_SECONDS = 120.0
DEFAULT_PROVIDER = "claude_code"


def linked():
    """Pages of the working version with code paths of their own: the only ones a change of code is matched against
    (released versions are only corrected by hand, #126)."""
    return and_(cast(DocPage.paths, String) != "[]", DocPage.until_seq.is_(None))


@dataclass
class Concern:
    page: DocPage
    reasons: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)  # its lines and sections concerned


matches = docs_changes.matches


@dataclass
class TranslationChoice:
    """What the site's "llm" translations run on right now: shown in its settings (#178), used by the watcher."""

    provider: str
    model: str
    provider_from: str  # "site": its own setting, "agent": its doc agent's provider
    model_from: str  # "site": its own setting, "small": the provider's small model
    account: Account | None = None  # the subscription it runs on; None: it can't run, ``problem`` says why
    problem: str = ""


async def translation_choice(s: AsyncSession, runs: RunManager, project: Project, site: DocSite, agent: Agent) -> TranslationChoice:
    """The provider and model of the site's translations: its own, else its doc agent's provider and that provider's
    small model. Never another provider: one the project can't use is a problem, not a reason to switch."""
    provider = site.translate_provider or agent.provider or DEFAULT_PROVIDER
    model = site.translate_model
    if not model:
        try:
            catalog = await account_service.catalog(s, await runs.runner.providers())
        except Exception:
            catalog = []
        model = questions.default_model(provider, catalog)
    choice = TranslationChoice(provider=provider, model=model, provider_from="site" if site.translate_provider else "agent", model_from="site" if site.translate_model else "small")
    where = runs.runners.where(project.id)
    choice.account = await account_service.pick(s, provider, agent, None, model=model, fresh=True, where=where)
    if choice.account is None:
        choice.problem = (
            await account_service.shut_out(s, provider, agent)
            or await account_service.not_signed_in(s, provider, agent, where)
            or f"There is no subscription for {provider}. Add one, or choose a provider the project has a subscription for."
        )
    return choice


class DocsWatcher:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runs: RunManager, interval: float):
        self._sm = sessionmaker
        self._runs = runs
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._daily: dict[int, datetime] = {}  # project -> its last daily look
        self._rechecked: set[int] = set()  # sites whose LLM translations were checked for their Markdown skeleton
        self._bases: dict[int, str] = {}  # repository -> its base branch, as its commits were last read
        self._tags_at: dict[int, datetime] = {}  # project -> when its repositories' tags were last read
        self._soon = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="docs-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def translate_soon(self) -> None:
        """Look now (a language or a translator was added, a translation was asked for)."""
        self._soon.set()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Docs watcher tick failed")
            try:
                await asyncio.wait_for(self._soon.wait(), self._interval)
            except TimeoutError:
                pass
            self._soon.clear()

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        if self._runs.paused:
            return
        async with self._sm() as s:
            ids = list(await s.scalars(select(DocSite.project_id).distinct()))
            # A paused project (by a person, or before its limits): no translations on its subscriptions, no git reads.
            resting = Project.paused_at.is_not(None) | Project.limit_paused_at.is_not(None)
            for project in list(await s.scalars(select(Project).where(Project.id.in_(ids), ~resting).order_by(Project.id))):
                try:
                    await self._project(s, project, now)
                except Exception:
                    log.exception("Docs watcher failed for project %s", project.id)
                    await s.rollback()

    async def _project(self, s: AsyncSession, project: Project, now: datetime) -> None:
        concerns: dict[int, Concern] = {}
        landed: list[str] = []
        changes: list[str] = []  # what landed, in short: the terms the doc agent searches for
        daily = now - self._daily.get(project.id, datetime.min) >= DAILY
        tags = now - self._tags_at.get(project.id, datetime.min) >= TAGS_EVERY
        texts: list[tuple[str, str]] = []  # what else changed, searched by meaning
        repos = list(await s.scalars(select(Repository).where(Repository.project_id == project.id).order_by(Repository.id)))
        for repo in repos:
            watch = await s.get(DocWatch, (project.id, repo.id))
            if watch is None:  # from now on: what landed before the docs existed concerns none of them
                watch = DocWatch(project_id=project.id, repository_id=repo.id, head="", landed_at=now, checked_at=now)
                s.add(watch)
                await s.commit()
            if not watch.head:  # where its history stands now (again, while the runner is away)
                await self._commits(s, project, repo, watch, now)
                continue
            landings = list(
                await s.scalars(
                    select(Landing)
                    .where(Landing.repository_id == repo.id, Landing.status == LandingStatus.MERGED, Landing.merged_at > (watch.landed_at or datetime.min))
                    .order_by(Landing.merged_at)
                )
            )
            if landings and (landings[-1].merged_at > now - QUIET and landings[0].merged_at > now - MAX_WAIT):
                continue  # still landing: one message for the whole burst, its commits read with it
            if not landings and not daily:
                continue
            commits = await self._commits(s, project, repo, watch, now)
            await self._follow_renames(s, project, repo, commits)
            await self._drift(s, project, commits)
            if not landings:
                before = len(concerns)
                await self._literals(s, project, [(_where(repo, c), c.diff) for c in commits if c.diff], concerns)  # old values only: no landing, no search by meaning
                if len(concerns) > before:
                    landed.append(f"{repo.name}: {len(commits)} commit{'s' if len(commits) != 1 else ''} pushed directly")
                continue
            watch = await s.get(DocWatch, (project.id, repo.id))
            watch.landed_at = landings[-1].merged_at
            await s.commit()
            labels = await self._concerned(s, project, repo, landings, commits, concerns, changes, texts)
            landed.append(f"{repo.name}: {'; '.join(labels)}")
        if tags:
            self._tags_at[project.id] = now
            for repo in repos:
                if label := await self._tags(s, project, repo, now, concerns, changes, texts):
                    landed.append(label)
            landed += await self._events(s, project, now, texts)
        await self._near(s, project, texts[:TEXTS], concerns)
        if concerns:
            await self._tell(s, project, landed, list(concerns.values()), now, changes)
        if daily:
            self._daily[project.id] = now
            await self._remind(s, project, now, await self._dead_paths(s, project, repos))
            await self._rolling(s, project, now)
            await self._structure(s, project, now)
        await self.translate(s, project, now)

    # --- translations ------------------------------------------------------------------------------------------

    async def translate(self, s: AsyncSession, project: Project, now: datetime) -> int:
        """Missing and outdated translations of the project's sites, a bounded batch. Returns how many were made."""
        started, made = time.monotonic(), 0
        for site in list(await s.scalars(select(DocSite).where(DocSite.project_id == project.id, DocSite.translator != "").order_by(DocSite.id))):
            if not site.languages:
                continue
            if site.id not in self._rechecked:
                self._rechecked.add(site.id)
                await docs_translate.recheck(s, site)
            llm = self._llm(s, project, site) if site.translator == "llm" else None
            for page, lang in await docs_translate.pending(s, site, now):
                if time.monotonic() - started > TRANSLATE_SECONDS:
                    return made
                page_id = page.id
                await s.commit()  # nothing of the database is held while the translator works
                try:
                    title, body, by = await docs_translate.translate(site, page, lang, llm)
                except Exception as exc:  # noqa: BLE001 - kept with the translation, tried again later
                    log.info("Translating doc page %s into %s failed: %s", page_id, lang, exc)
                    page = await s.get(DocPage, page_id)
                    if page is not None:
                        await docs_translate.save(s, site, page, lang, "", "", "", error=str(exc) or type(exc).__name__)
                    continue
                page = await s.get(DocPage, page_id)
                if page is not None:
                    await docs_translate.save(s, site, page, lang, title, body, by)
                    made += 1
        if made:
            await flow_log.record(s, project.id, "docs.translated", pages=made, seconds=round(time.monotonic() - started, 1))
        return made

    def _llm(self, s: AsyncSession, project: Project, site: DocSite):  # noqa: ANN202
        """One completion without tools on the project's subscriptions, its cost a run of the site's doc agent."""

        async def ask(system: str, prompt: str) -> str:
            agent = await docs_service.ensure_agent(s, site)
            choice = await translation_choice(s, self._runs, project, site, agent)
            account, provider, model = choice.account, choice.provider, choice.model
            if account is None:
                raise RuntimeError(choice.problem)
            credentials = settings_service.credentials(await settings_service.get_all(s), account=account_service.fields(account))
            account_key, agent_id = account.key, agent.id
            await s.commit()
            result: CompletionResult | None = None
            error = ""
            try:
                result = await self._runs.runners.of(project.id).complete(CompletionSpec(provider=provider, model=model, system_prompt=system, prompt=prompt, credentials=credentials))
            except Exception as exc:
                error = str(exc) or type(exc).__name__
            at = utcnow()
            s.add(Run(
                project_id=project.id, agent_id=agent_id, trigger=RunTrigger.DOCS, status=RunStatus.FAILED if error else RunStatus.SUCCEEDED, input_text="",
                error=truncate(error, 500) if error else None, input_tokens=result.input_tokens if result else 0, output_tokens=result.output_tokens if result else 0,
                cost_usd=result.cost_usd if result else 0.0, num_turns=1 if result else 0, account_key=account_key, created_at=at, started_at=at, finished_at=at,
            ))  # fmt: skip
            await s.commit()
            if result is None:
                raise RuntimeError(error)
            return result.text

        return ask

    async def _commits(self, s: AsyncSession, project: Project, repo: Repository, watch: DocWatch, now: datetime) -> list[GitCommit]:
        """The base branch's commits since the last one read (the first time: only where to start from)."""
        if not self._runs.runners.ready(project.id):
            return []
        spec = GitLogSpec(
            credentials=await permissions.credentials(s, project), key=landing_service.train_key(repo), remote=landing_service.remote(repo),
            base=repo.default_branch or None, since=watch.head or None, limit=COMMITS if watch.head else 1,
            diffs=COMMIT_DIFFS if watch.head else 0, diff_chars=DIFF_CHARS,
        )  # fmt: skip
        first = not watch.head
        await s.commit()  # nothing of the database is held while the runner reads
        try:
            result = await self._runs.runners.of(project.id).git_log(spec)
        except (RunnerError, AttributeError) as exc:
            log.info("Docs watcher: the commits of repository %s could not be read: %s", repo.id, exc)
            return []
        watch = await s.get(DocWatch, (project.id, repo.id))
        if result.branch:
            self._bases[repo.id] = result.branch
        if watch is not None and result.head:
            watch.head, watch.checked_at = result.head, now
            await s.commit()
        return [] if first else result.commits

    async def _unmatched(self, s: AsyncSession, project: Project, repo: Repository, patterns: list[str]) -> set[str] | None:
        """Which of these patterns match no file at the tip of the repository's base branch (None: unknown)."""
        base = self._bases.get(repo.id)
        if not patterns or not base or not self._runs.runners.ready(project.id):
            return None
        await s.commit()
        try:
            result = await self._runs.runners.of(project.id).git_present(GitPresentSpec(key=landing_service.train_key(repo), base=base, patterns=patterns[:PATTERNS]))
        except (RunnerError, AttributeError) as exc:
            log.info("Docs watcher: the paths of repository %s could not be checked: %s", repo.id, exc)
            return None
        return None if result.unmatched is None else set(result.unmatched)

    async def _follow_renames(self, s: AsyncSession, project: Project, repo: Repository, commits: list[GitCommit]) -> None:
        """Pages' code paths follow what these commits renamed, without a model: a file to its new name, a folder
        whose files all moved to one new folder (once it holds nothing any more). Each change is a page version."""
        renames: dict[str, str] = {}
        for c in reversed(commits):  # oldest first: what was renamed twice ends at its last name
            for old, new in c.renames.items():
                for k, v in list(renames.items()):
                    if v == old:
                        renames[k] = new
                renames.setdefault(old, new)
        if not renames:
            return
        plans: dict[int, list[tuple[str, str]]] = {}  # page -> (pattern, new pattern)
        folders: set[str] = set()
        pages = {p.id: p for p in await s.scalars(select(DocPage).where(DocPage.project_id == project.id, linked()))}
        for page in pages.values():
            for pattern in page.paths or []:
                clean = pattern.strip().lstrip("./").rstrip("/")
                if not clean or any(ch in clean for ch in "*?["):
                    continue
                if clean in renames:
                    plans.setdefault(page.id, []).append((pattern, renames[clean]))
                    continue
                moved = [(old[len(clean) :], new) for old, new in renames.items() if old.startswith(clean + "/")]
                to = {new[: -len(rest)] for rest, new in moved if new.endswith(rest)}
                if moved and len(to) == 1 and all(new.endswith(rest) for rest, new in moved) and (target := to.pop()) and target != clean:
                    plans.setdefault(page.id, []).append((pattern, target))
                    folders.add(clean)
        if folders:  # a folder moves only when nothing is left in it
            empty = await self._unmatched(s, project, repo, sorted(folders)) or set()
            plans = {pid: [(p, n) for p, n in plan if p.strip().lstrip("./").rstrip("/") not in folders or p.strip().lstrip("./").rstrip("/") in empty] for pid, plan in plans.items()}
        done = []
        for page_id, plan in plans.items():
            if not plan:
                continue
            page = await s.get(DocPage, page_id)
            if page is None:
                continue
            swap = dict(plan)
            page.paths = list(dict.fromkeys(swap.get(p, p) for p in page.paths or []))
            reason = "Linked code paths follow a rename: " + ", ".join(f"{p} -> {n}" for p, n in plan)
            s.add(DocPageVersion(project_id=page.project_id, page_id=page.id, title=page.title, body=page.body, author="PlainBoard", reason=truncate(reason, 300), created_at=utcnow()))
            done.append({"page": page.id, "renamed": plan})
        if done:
            await s.commit()
            await flow_log.record(s, project.id, "docs.paths_renamed", pages=done[:50])

    async def _dead_paths(self, s: AsyncSession, project: Project, repos: list[Repository]) -> dict[int, list[str]]:
        """Pages whose code paths match no file in any of the project's repositories (as last read), by page."""
        rows = (await s.execute(select(DocPage.id, DocPage.paths).where(DocPage.project_id == project.id, linked()))).all()
        patterns = sorted({p for _, paths in rows for p in paths or [] if p.strip()})
        if not patterns or not repos:
            return {}
        dead = set(patterns[:PATTERNS])
        for repo in repos:
            unmatched = await self._unmatched(s, project, repo, patterns)
            if unmatched is None:
                return {}  # a repository that cannot tell: nothing is called dead
            dead &= unmatched
        return {pid: [p for p in paths if p in dead] for pid, paths in rows if any(p in dead for p in paths or [])}

    async def _drift(self, s: AsyncSession, project: Project, commits: list[GitCommit]) -> None:
        if not commits:
            return
        for page in await s.scalars(select(DocPage).where(DocPage.project_id == project.id, linked())):
            if page.paths and (n := sum(1 for c in commits if any(matches(f, page.paths) for f in c.files))):
                page.drift += n
        await s.commit()

    async def _concerned(
        self,
        s: AsyncSession,
        project: Project,
        repo: Repository,
        landings: list[Landing],
        commits: list[GitCommit],
        concerns: dict[int, Concern],
        changes: list[str],
        all_texts: list[tuple[str, str]],
    ) -> list[str]:
        """Adds the pages these landings concern, to ``changes`` what landed, in short, and to ``all_texts`` what to search
        by meaning; returns how the landings are named."""
        labels, texts = [], []
        for landing in landings:
            task = await s.get(Task, landing.task_id) if landing.task_id else None
            report = await s.get(Assignment, landing.assignment_id) if landing.assignment_id else None
            label = landing.branch + (f" (#{task.number})" if task else "")
            labels.append(label)
            parts = [landing.title or landing.branch, f"#{task.number} {task.title}\n{truncate(task.description, 800)}" if task else "", truncate(report.result, 1200) if report and report.result else ""]
            texts.append((label, "\n\n".join(p for p in parts if p)))
            if len(changes) < CHANGES:
                said = f"#{task.number} {task.title}" if task else landing.title or landing.branch
                excerpt = " ".join(report.result.split()) if report and report.result else ""
                changes.append(f"- {repo.name} {said}" + (f": {truncate(excerpt, REPORT_CHARS)}" if excerpt else ""))
        if commits:
            changed = sorted({f for c in commits for f in c.files})
            messages = "\n".join(c.message.split("\n", 1)[0] for c in commits[:40])
            texts.append((f"the commits in {repo.name}", f"{messages}\n\nChanged: {', '.join(changed[:80])}"))
            subjects = list(dict.fromkeys(c.message.split("\n", 1)[0].strip() for c in commits if c.message.strip()))
            if subjects:
                more = f" (and {len(subjects) - COMMIT_SUBJECTS} more)" if len(subjects) > COMMIT_SUBJECTS else ""
                changes.append(f"- commits in {repo.name}: " + "; ".join(truncate(m, 120) for m in subjects[:COMMIT_SUBJECTS]) + more)
        for page in await s.scalars(select(DocPage).where(DocPage.project_id == project.id, linked())):
            if not page.paths or page.human_only:  # written by people only (#166): no work for the doc agent
                continue
            touched = [c for c in commits if any(matches(f, page.paths) for f in c.files)]
            if touched:
                files = sorted({f for c in touched for f in c.files if matches(f, page.paths)})
                more = f" and {len(files) - 4} more" if len(files) > 4 else ""
                concerns.setdefault(page.id, Concern(page)).reasons.append(
                    f"its linked code changed: {', '.join(files[:4])}{more} ({len(touched)} commit{'s' if len(touched) != 1 else ''})"
                )
        if len(commits) > 1:  # each commit message on its own too: a small change is not drowned by the others
            texts += [(f"commit {_where(repo, c)}", c.message) for c in commits[:HUNKS] if c.message.strip()]
        texts += await self._literals(s, project, [(_where(repo, c), c.diff) for c in commits if c.diff], concerns)
        all_texts.extend(texts)
        return labels

    async def _literals(self, s: AsyncSession, project: Project, diffs: list[tuple[str, str]], concerns: dict[int, Concern], extra: list | None = None) -> list[tuple[str, str]]:
        """Pages that still name an old value these diffs changed (and ``extra`` probes) are concerned, with their
        lines. Returns the diffs' hunks, one per file, for the search by meaning."""
        found = list(extra or [])
        texts: list[tuple[str, str]] = []
        files: set[str] = set()
        for where, diff in diffs:
            parts = docs_changes.hunks(diff)
            found += docs_changes.probes(docs_changes.changed_tokens(parts), where)
            for h in parts:
                if h.path not in files and len(texts) < HUNKS:
                    files.add(h.path)
                    texts.append((f"the change of {h.path} in {where}", h.text()))
        named = await docs_changes.mentions(s, project.id, found[:PROBES], PLACES)
        for page_id, lines in named.items():
            concern = concerns.setdefault(page_id, Concern(lines[0].page))
            for said in dict.fromkeys(m.probe.says() for m in lines if m.probe):
                concern.reasons.append(said)
            concern.places += [m.place() for m in lines]
        if found:
            await flow_log.record(s, project.id, "docs.literals", searched=[p.old for p in found[:PROBES]][:40], pages=list(named)[:50])
        return texts if diffs else []

    async def _tags(self, s: AsyncSession, project: Project, repo: Repository, now: datetime, concerns: dict[int, Concern], changes: list[str], texts: list[tuple[str, str]]) -> str:
        """The repository's tags: new ones are kept, and (once its tags were read before) what each changed is searched
        for like a landing's commits. Returns how they are named, or ""."""
        if not self._runs.runners.ready(project.id):
            return ""
        known = list(await s.scalars(select(RepoTag.name).where(RepoTag.repository_id == repo.id).order_by(RepoTag.id.desc()).limit(1000)))
        spec = GitTagsSpec(credentials=await permissions.credentials(s, project), key=landing_service.train_key(repo), remote=landing_service.remote(repo), known=known, diff_chars=DIFF_CHARS * 3)
        await s.commit()
        try:
            result = await self._runs.runners.of(project.id).git_tags(spec)
        except (RunnerError, AttributeError) as exc:
            log.info("Docs watcher: the tags of repository %s could not be read: %s", repo.id, exc)
            return ""
        have = set(known)
        new = [t for t in result.tags if t.name not in have]
        for t in reversed(new):  # oldest first: ids follow their order
            s.add(RepoTag(project_id=project.id, repository_id=repo.id, name=t.name[:250], sha=t.sha[:64], tagged_at=indexers._utc(t.date) if t.date else None, subject=truncate(t.subject, 300), seen_at=now))
        await s.commit()
        if not known or not new:
            return ""  # the first read: where its releases stand, nothing to tell
        named = []
        for t in new[:5]:
            where = f"release {t.name} of {repo.name}"
            extra = docs_changes.probes([(t.previous, t.name)], where) if t.previous else []
            texts += await self._literals(s, project, [(where, t.diff)] if t.diff else [], concerns, extra)
            if t.subjects:
                texts.append((where, f"{t.name}: {t.subject}\n" + "\n".join(t.subjects[:40])))
            if len(changes) < CHANGES:
                since = f" (after {t.previous})" if t.previous else ""
                changes.append(f"- {where}{since}: " + truncate("; ".join(t.subjects[:COMMIT_SUBJECTS]) or t.subject or "no commits named", REPORT_CHARS))
            named.append(t.name)
        return f"{repo.name}: new tag{'s' if len(named) != 1 else ''} {', '.join(named)}"

    async def _events(self, s: AsyncSession, project: Project, now: datetime, texts: list[tuple[str, str]]) -> list[str]:
        """Decisions made and text files attached to tickets since the last look (every ``TAGS_EVERY``): searched by
        meaning (with semantic search only). The project's sites keep how far they were read (``DocSite.changes_at``)."""
        sites = list(await s.scalars(select(DocSite).where(DocSite.project_id == project.id)))
        since = min((site.changes_at for site in sites if site.changes_at), default=None)
        for site in sites:
            site.changes_at = now
        await s.commit()
        if since is None or not await search_service.enabled(s, project):
            return []
        named: list[str] = []
        decided = (
            await s.execute(
                select(Decision, Task.number)
                .join(Task, Task.id == Decision.task_id)
                .where(Decision.project_id == project.id, Decision.status == DecisionStatus.ANSWERED, Decision.answered_at > since, Decision.answered_at <= now)
                .order_by(Decision.answered_at)
                .limit(EVENTS)
            )
        ).all()
        for d, number in decided:
            answer = docs_changes.decision_answer(d.options, d.answer_option, d.answer_text)
            texts.append((f"the decision on #{number}", truncate(f"{d.question}\n{answer}\n{d.details}", EVENT_CHARS)))
            named.append(f"decision on #{number}")
        files = (
            await s.execute(
                select(Attachment, Task.number)
                .join(Task, Task.id == Attachment.task_id)
                .where(Attachment.project_id == project.id, Attachment.created_at > since, Attachment.created_at <= now, Attachment.purged_at.is_(None), indexers._texty())
                .order_by(Attachment.created_at)
                .limit(EVENTS)
            )
        ).all()
        for a, number in files:
            body = await asyncio.to_thread(indexers.read_text, get_config().uploads_dir / a.storage_name)
            if body:
                texts.append((f'the file "{a.filename}" on #{number}', truncate(f"{a.filename}\n{body}", EVENT_CHARS)))
                named.append(f'file "{truncate(a.filename, 60)}" on #{number}')
        return [f"board: {', '.join(named)}"] if named else []

    async def _near(self, s: AsyncSession, project: Project, texts: list[tuple[str, str]], concerns: dict[int, Concern]) -> None:
        """Pages with a section close in meaning to one of these texts are concerned, with that section."""
        if not texts or not await search_service.enabled(s, project):
            return
        model = await search_service.embedder(s)
        if model.state != "ready":
            return
        threshold = model.card.hint_similarity - NEAR_MARGIN
        found = []
        for label, text in texts:
            seen: set[int] = set()
            for hit in await docs_changes.near(s, project.id, text, NEAR):
                found.append({"page": hit.page.id, "sim": round(hit.similarity, 4), "to": label})
                if hit.similarity < threshold or hit.page.human_only:
                    continue
                concern = concerns.setdefault(hit.page.id, Concern(hit.page))
                if hit.page.id not in seen:
                    seen.add(hit.page.id)
                    concern.reasons.append(f"close in meaning to {label} ({hit.similarity:.2f})")
                concern.places.append(f'section "{hit.section}" is close in meaning to {label}')
        await flow_log.record(s, project.id, "docs.near", model=model.name, threshold=threshold, nearest=found)

    async def _tell(self, s: AsyncSession, project: Project, landed: list[str], concerns: list[Concern], now: datetime, changes: list[str] | None = None) -> None:
        sites = {site.id: site for site in await s.scalars(select(DocSite).where(DocSite.project_id == project.id))}
        for c in concerns:
            if c.page.flagged_at is None:
                c.page.flagged_at = now
            dead = c.page.flag_reason.split("; change or remove the link")[0] + "; change or remove the link" if docs_service.DEAD_LINK in c.page.flag_reason else ""
            c.page.flag_reason = truncate("; ".join([*([dead] if dead else []), *c.reasons]), 1000)
        await s.commit()
        await flow_log.record(s, project.id, "docs.outdated", pages=[c.page.id for c in concerns], landed=landed)
        for site_id, mine in _by_site([c.page for c in concerns]).items():
            reasons = {c.page.id: "; ".join(c.reasons) for c in concerns}
            places = {c.page.id: c.places for c in concerns}
            lines = [_line(p, reasons[p.id], places[p.id]) for p in mine[:PAGES_NAMED]]
            more = f"\n(and {len(mine) - PAGES_NAMED} more marked pages: list_docs marked=true)" if len(mine) > PAGES_NAMED else ""
            text = (
                f"Pages of your doc site may be outdated after what changed ({' | '.join(landed)}). Pages to check (with the lines and sections concerned):\n"
                + "\n".join(lines) + more + "\n"
                "Check whether each is really wrong or missing something; only then change it, and only the sentences or sections concerned "
                "(write_doc_page with a reason, no rewording or restructuring), else confirm it (write_doc_page still_current).\n"
                "Then search your site yourself (doc_grep for the old values, doc_search or list_docs query for the change's meaning: settings, commands, "
                "endpoints, UI labels) for other pages that now describe what changed wrongly and fix just that, add a page for a new feature that has none, "
                "check the code paths of the pages you touch, and keep your site overview in your notes current."
            )
            if changes:
                text += "\n\n" + truncate("What landed:\n" + "\n".join(changes), CHANGE_CHARS)
            await self._send(s, sites[site_id], "Update docs after changes", text)

    async def _remind(self, s: AsyncSession, project: Project, now: datetime, dead: dict[int, list[str]] | None = None) -> None:
        dead = dead or {}
        due = (DocPage.reminded_at.is_(None)) | (DocPage.reminded_at < now - REMIND_EVERY)
        dead_link = DocPage.flag_reason.contains(docs_service.DEAD_LINK)  # at once: its link goes nowhere (#154)
        stale = (DocPage.drift >= DRIFT_STALE) | (DocPage.flagged_at < now - timedelta(days=STALE_DAYS)) | dead_link
        if dead:
            stale = stale | DocPage.id.in_(list(dead)[:PATTERNS])
        stale = and_(stale, DocPage.until_seq.is_(None), DocPage.human_only.is_(False))  # the working version's, not those written by people only (#166)
        sites = {site.id: site for site in await s.scalars(select(DocSite).where(DocSite.project_id == project.id))}
        for site in sites.values():
            pages = list(await s.scalars(select(DocPage).where(DocPage.site_id == site.id, due, stale).order_by(DocPage.drift.desc(), DocPage.id).limit(PAGES_NAMED + 1)))
            if not pages:
                continue
            lines = []
            for page in pages[:PAGES_NAMED]:
                page.reminded_at = now
                why = []
                if page.drift >= DRIFT_STALE:
                    why.append(f"{page.drift} commits changed its linked code since its last edit {page.edited_at:%Y-%m-%d}")
                if docs_service.DEAD_LINK in page.flag_reason:
                    why.append(truncate(page.flag_reason, 300))
                elif page.flagged_at and page.flagged_at < now - timedelta(days=STALE_DAYS):
                    why.append(f"marked as possibly outdated since {page.flagged_at:%Y-%m-%d}: {truncate(page.flag_reason, 200)}")
                if page.id in dead:
                    more_dead = f" and {len(dead[page.id]) - 5} more" if len(dead[page.id]) > 5 else ""
                    why.append(f"its code paths match no file any more: {', '.join(dead[page.id][:5])}{more_dead}")
                lines.append(_line(page, "; ".join(why)))
            await s.commit()
            total = int(await s.scalar(select(func.count()).select_from(DocPage).where(DocPage.site_id == site.id, stale)) or 0)
            more = f"\n(of {total} such pages: list_docs marked=true)" if total > len(lines) else ""
            text = (
                "Pages of your doc site that are probably stale:\n" + "\n".join(lines) + more + "\n"
                "Bring them up to date per the doc spec, or confirm them with write_doc_page still_current. A page whose code paths "
                "match nothing: link it to where that code is now (write_doc_page paths), or change the page if the feature is gone. A page that "
                "links to a deleted page: link the page that covers it now (doc-page:ID) or remove the link."
            )
            await self._send(s, site, "Bring stale doc pages up to date", text)

    async def _rolling(self, s: AsyncSession, project: Project, now: datetime) -> None:
        """The daily check of each site: its riskiest pages within ``check_budget`` (docs_changes.risky), none when
        nothing is due. Each is named at most once per ``REMIND_EVERY``."""
        for site in list(await s.scalars(select(DocSite).where(DocSite.project_id == project.id, DocSite.check_budget > 0).order_by(DocSite.id))):
            picked = await docs_changes.risky(s, site, now, now - REMIND_EVERY)
            if not picked:
                continue
            lines = []
            for r in picked:
                page = await s.get(DocPage, r.page_id)
                if page is not None:
                    page.reminded_at = now
                    lines.append(_line(page, r.why))
            await s.commit()
            await flow_log.record(s, project.id, "docs.rolling_check", site=site.id, pages=[r.page_id for r in picked], tokens=sum(r.tokens for r in picked))
            text = (
                f"Today's check of your doc site (a small daily budget of {site.check_budget} tokens): these pages went longest without a check, "
                "weighted by the code changes and releases since.\n" + "\n".join(lines) + "\n"
                "For each: changes_since (page) shows the commits, releases and decisions since it was last confirmed; read only what concerns it. "
                "Fix just what is wrong now (write_doc_page with a reason), else confirm it (write_doc_page still_current). Only these pages."
            )
            await self._send(s, site, "Check a few doc pages", text)

    async def _structure(self, s: AsyncSession, project: Project, now: datetime) -> None:
        """Asks a doc agent to check its site's structure once the site grew (services/docs_proposals.due, #156)."""
        for site in list(await s.scalars(select(DocSite).where(DocSite.project_id == project.id, DocSite.restructure_check.is_(True)).order_by(DocSite.id))):
            if not site.spec.strip() or (due := await docs_proposals.due(s, site, now)) is None:
                continue
            pages, why = due
            agent = await docs_service.ensure_agent(s, site)
            if not agent.enabled:
                continue  # paused: checked when it is back
            site.structure_checked_at, site.structure_pages = now, pages
            await s.commit()
            await flow_log.record(s, project.id, "docs.structure_check", site=site.id, why=why)
            await self._send(s, site, "Check the structure of your doc site", docs_proposals.check_text(why, await docs_proposals.declined(s, site.id)))

    async def _send(self, s: AsyncSession, site: DocSite, title: str, text: str) -> None:
        """To the site's doc agent: one assignment, or an update for the one it still works on."""
        agent = await docs_service.ensure_agent(s, site)
        if not agent.enabled:
            return  # paused by the user: the pages stay marked, list_docs shows them
        busy = await s.scalar(
            select(Assignment.id).where(Assignment.agent_id == agent.id, Assignment.created_by_agent_id.is_(None), Assignment.status.in_((AssignmentStatus.QUEUED, AssignmentStatus.RUNNING))).limit(1)
        )
        if busy is not None:
            await inbox.push(s, agent.id, f"More for assignment #{busy}: {text}")
            await self._runs.request_wake(s, agent.id)
            return
        await self._runs.delegate(s, None, agent, title, text, None, source="PlainBoard")


def _by_site(pages: list[DocPage]) -> dict[int, list[DocPage]]:
    out: dict[int, list[DocPage]] = {}
    for page in pages:
        out.setdefault(page.site_id, []).append(page)
    return out


def _line(page: DocPage, why: str, places: list[str] | None = None) -> str:
    lines = [f'- doc page {page.page_key} "{page.title}" (/{page.path}): {why}']
    lines += [f"  - {p}" for p in list(dict.fromkeys(places or []))[:PLACES]]
    return "\n".join(lines)


def _where(repo: Repository, commit: GitCommit) -> str:
    return f'{commit.sha[:10]} "{truncate(commit.message.split(chr(10), 1)[0].strip(), 60)}" in {repo.name}'
