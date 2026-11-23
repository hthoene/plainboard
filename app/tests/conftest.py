from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import shutil
import tempfile
import time
import weakref
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from plainboard_protocol import (
    GitLogResult,
    EndpointInfo,
    LimitsEvent,
    LimitWindow,
    CleanedItem,
    CleanupResult,
    MachineUsage,
    AssistantTextEvent,
    CheckResult,
    CompactionEvent,
    CompletionResult,
    CompletionSpec,
    JobSpec,
    JobState,
    ModelInfo,
    ProviderInfo,
    PullRequestCheck,
    PullRequestState,
    ResultEvent,
    RunEvent,
    RunSpec,
    SessionEvent,
    ToolResultEvent,
    ToolUseEvent,
    TidyBranch,
    TidyResult,
    TrainResult,
    UsageEvent,
)

from plainboard.orchestration.runner_client import RunnerError, RunnerStreamBroken, RunnerUnavailable


# Transactions with a write that is not committed yet, and the task that wrote. Looked at when the
# runner is called (FakeRunner.database_free): no waiting, so looking changes nothing. Kept by the
# Connection object, which lives as long as its transaction, and never asked for anything: asking
# one that was cut off for its database connection makes it connect again, in the middle of the
# app's shutdown.
_writes: dict[int, tuple[weakref.ref, weakref.ref]] = {}


def _current_task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:  # the migrations: no event loop
        return None


@event.listens_for(Engine, "before_cursor_execute")
def _wrote(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
    task = _current_task()
    if task is not None and statement.lstrip()[:7].split(" ")[0].upper() in ("INSERT", "UPDATE", "DELETE"):
        _writes[id(conn)] = (weakref.ref(conn), weakref.ref(task))


@event.listens_for(Engine, "commit")
@event.listens_for(Engine, "rollback")
def _ended(conn):  # noqa: ANN001
    _writes.pop(id(conn), None)


def writing() -> bool:
    """Has the task that asks written to the database without committing?"""
    task = _current_task()
    return task is not None and any(conn() is not None and writer() is task for conn, writer in list(_writes.values()))


class FakeRunner:
    """Stands in for the runner service. Understands the same directives as the mock provider:
    ``TOOL <name> <json>`` and ``SAY <text>``; ``CONTEXT <n>`` sets the reported context size,
    ``COMPACT <n>`` reports a compaction by the provider."""

    app = None  # set by fixture; used to execute tools through the real RunManager
    held: list[str] = []  # calls that were made while a write to the database was waiting for its commit

    def database_free(self, call: str) -> None:
        """What the runner does takes seconds (git, a model): nobody may call it between a write and
        its commit. SQLite has one writer, so every write of everyone would wait that long."""
        if writing():
            self.held.append(call)

    def browser_url(self, key: str) -> str:
        return f"ws://runner/browser/{key}"

    def __init__(self, base_url: str):
        self.specs: list[RunSpec] = []
        self.completions: list[CompletionSpec] = []
        self.notices: list[tuple[int, str]] = []
        self.limit_windows: list[LimitWindow] = []
        self.limit_calls: list = []
        self.limit_plans: dict[str, tuple[str, float | None]] = {}  # by token: the plan the provider names
        self.unavailable = 0  # how many starts of a turn fail because the runner is not up yet
        self.active: set[int] = set()  # turns streaming right now
        self.forgotten: set[int] = set()  # turns the runner lost although the app still waits for them
        self.jobs: dict[str, JobState] = {}
        self.job_specs: list[JobSpec] = []
        self.ports: dict[str, list] = {}  # job id -> the web servers it has started (JobPort)
        self.listening: dict[str, list[int]] = {}  # job id -> further ports it listens on (no web server)
        self.reachable = True
        self.released = False
        # Landing: what the git host would say.
        self.branches: dict[str, str] = {}  # branch -> head commit on the remote
        self.conflicting: set[str] = set()  # branches that cannot be merged onto the base
        self.behind: set[str] = set()  # branches that merge onto the base, but not with a branch before them on the train
        self.touches: dict[str, set[str]] = {}  # branch -> the files it changes
        self.commit_checks_of: dict[str, list] = {}  # commit -> the checks GitHub ran for it on the base branch
        self.reruns: list[list[int]] = []  # workflow runs whose failed jobs were run once more
        self.job_timings_of: dict[str, list] = {}  # commit -> how long its jobs of Actions took
        self.on_base: set[str] = set()  # branches the base branch already contains
        self.train_specs: list = []
        self.breaks = 0  # how many streams broke (BREAK, BREAK_ONCE)
        self.emptied: set[str] = set()  # runs whose turn ended without a step once
        self.train_results: list[TrainResult] = []
        self.check_specs: list = []
        self.land_specs: list = []
        self.land_error = ""
        self.pulls: dict[tuple[str, int], PullRequestState] = {}  # GitHub: the trains' pull requests
        self.merged: list[tuple[str, int, dict]] = []
        self.closed: list[int] = []
        self.merge_error = ""
        # Newer versions of PlainBoard itself: what GitHub would say.
        self.upstream_state = None
        self.upstream_error = ""
        self.upstream_calls: list[tuple[str, str, str]] = []

    async def stream_run(self, spec: RunSpec) -> AsyncIterator[RunEvent]:
        self.active.add(int(spec.run_id))
        try:
            async for event in self._events(spec):
                yield event
        finally:
            self.active.discard(int(spec.run_id))

    async def active_runs(self) -> set[int]:
        if not self.reachable:
            raise RunnerError("Runner unreachable")
        return self.active - self.forgotten

    async def _events(self, spec: RunSpec) -> AsyncIterator[RunEvent]:
        self.database_free("stream_run")
        if self.unavailable > 0:  # not up yet
            self.unavailable -= 1
            raise RunnerUnavailable("Runner unreachable: [Errno -2] Name or service not known")
        self.specs.append(spec)
        runs = self.app.state.runs
        ctx = runs.context_for_token(spec.bridge.run_token, int(spec.run_id))
        session_id = spec.resume_session_id or f"session-{spec.run_id}"
        yield SessionEvent(session_id=session_id)
        said, context = [], 1000
        for i, line in enumerate(spec.prompt.splitlines()):
            line = line.strip()
            if line.startswith("TOOL "):
                _, name, *rest = line.split(" ", 2)
                args = json.loads(rest[0]) if rest else {}
                yield ToolUseEvent(id=f"t{i}", name=name, input=args)
                result = await runs.execute_tool(ctx, name, args)
                yield ToolResultEvent(tool_use_id=f"t{i}", content=result.text, is_error=result.is_error)
                said.append(result.text)
            elif line.startswith("SAY "):
                said.append(line[4:])
            elif line.startswith("CONTEXT "):
                context = int(line.split()[1])
            elif line == "HOLD":
                while not self.released:
                    await asyncio.sleep(0.01)
            elif line.startswith("LIMITS "):  # "LIMITS name used resets_in [ALL]": ALL = the report is complete
                _, name, used, resets_in, *rest = line.split()
                yield LimitsEvent(windows=[LimitWindow(name=name, utilization=float(used), resets_at=int(time.time()) + int(resets_in))], complete=rest == ["ALL"])
            elif line.startswith("REJECT "):  # "REJECT token window resets_in": the account with that token is used up
                _, token, name, resets_in = line.split()
                if token in (spec.credentials.claude_oauth_token, spec.credentials.anthropic_api_key):
                    yield LimitsEvent(windows=[LimitWindow(name=name, utilization=1.0, resets_at=int(time.time()) + int(resets_in), status="rejected")])
                    yield ResultEvent(session_id=session_id, is_error=True, error="You've hit your limit")
                    return
            elif line.startswith("COMPACT "):
                yield CompactionEvent(tokens_before=int(line.split()[1]))
            elif line == "BREAK" or (line == "BREAK_ONCE" and spec.run_id not in self.emptied):
                # The connection broke before the first step, without a word from the runner
                self.emptied.add(spec.run_id)
                self.breaks += 1
                raise RunnerStreamBroken("Runner unreachable: ReadError", 1)
            elif line == "EMPTY" or (line == "EMPTY_ONCE" and spec.run_id not in self.emptied):
                # Claude Code ended the turn at once: no step, no text
                self.emptied.add(spec.run_id)
                yield ResultEvent(session_id=session_id)
                return
        text = " | ".join(said) or "ok"
        yield AssistantTextEvent(text=text)
        yield UsageEvent(context_tokens=context)
        yield ResultEvent(session_id=session_id, text=text, num_turns=1, input_tokens=10, output_tokens=5, cost_usd=0.001, context_tokens=context)

    skills_in_git: dict[str, bytes] = {}  # "url path@ref" -> the zip the runner would send (fetch_skill)

    async def fetch_skill(self, spec) -> bytes:
        self.database_free("fetch_skill")
        key = f"{spec.url} {spec.path}@{spec.ref}"
        if key not in self.skills_in_git:
            raise RunnerError(f"git clone failed: {key}")
        return self.skills_in_git[key]

    async def complete(self, spec: CompletionSpec) -> CompletionResult:
        self.completions.append(spec)
        return CompletionResult(text=f"SUMMARY #{len(self.completions)}", input_tokens=100, output_tokens=10, cost_usd=0.0001)

    async def cancel(self, run_id: int) -> None:
        pass

    async def limits(self, provider, credentials) -> LimitsEvent:
        self.limit_calls.append((provider, credentials))
        plan, size = self.limit_plans.get(credentials.claude_oauth_token or "", (None, None))
        if provider == "codex":
            windows = [w for w in self.limit_windows if w.name.startswith("codex_")]
        elif provider == "antigravity":
            windows = [w for w in self.limit_windows if w.name.startswith("antigravity_")]
        else:
            windows = [w for w in self.limit_windows if not w.name.startswith(("codex_", "antigravity_"))]
        return LimitsEvent(windows=windows, complete=provider in ("codex", "antigravity"), plan=plan, plan_size=size)

    async def notify_run(self, run_id: int, text: str, urgent: bool = False) -> bool:
        self.notices.append((run_id, text))
        return True

    async def start_job(self, spec: JobSpec) -> JobState:
        self.job_specs.append(spec)
        self.jobs[spec.id] = JobState(running=True, log_path=f"/jobs/{spec.id}.log")
        return self.jobs[spec.id]

    async def job_status(self, ids: list[str]) -> dict[str, JobState]:
        if not self.reachable:
            raise RunnerError("Runner unreachable")
        return {i: self.jobs[i] for i in ids if i in self.jobs}

    async def job_ports(self, ids: list[str]) -> dict[str, list]:
        if not self.reachable:
            raise RunnerError("Runner unreachable")
        return {i: self.ports[i] for i in ids if i in self.ports}

    async def job_listening(self, ids: list[str]) -> dict[str, list[int]]:
        if not self.reachable:
            raise RunnerError("Runner unreachable")
        found = {i: sorted({p.port for p in self.ports.get(i, [])} | set(self.listening.get(i, []))) for i in ids}
        return {i: ports for i, ports in found.items() if ports}

    async def stop_job(self, runner_id: str) -> bool:
        job = self.jobs.get(runner_id)
        return bool(job and job.running)

    async def branch_head(self, credentials, remote, branch: str) -> str | None:
        return self.branches.get(branch)

    async def assemble_train(self, spec) -> TrainResult:
        self.train_specs.append(spec)
        result = TrainResult(base=spec.base or "main", head_sha=f"train-{len(self.train_specs)}")
        for branch in spec.members:
            if branch not in self.branches:
                result.missing.append(branch)
            elif spec.skip_unchanged.get(branch) == self.branches[branch]:
                result.unchanged.append(branch)
            elif branch in self.on_base:
                result.landed.append(branch)
            elif set(spec.busy_files) & self.touches.get(branch, set()):
                result.held.append(branch)
            elif branch in self.conflicting:
                result.conflicts.append(branch)
                result.conflict_files[branch] = sorted(self.touches.get(branch, set()))
            elif branch in self.behind and result.merged:
                result.waits.append(branch)
                result.conflict_files[branch] = sorted(self.touches.get(branch, set()))
            else:
                result.merged[branch] = self.branches[branch]
        if spec.github and result.merged:
            result.number, result.url = 100 + len(self.train_specs), f"https://github.com/{spec.github}/pull/{100 + len(self.train_specs)}"
            self.pulls[(spec.github, result.number)] = PullRequestState(
                state="open", mergeable="yes", head_sha=result.head_sha, url=result.url, checks=[PullRequestCheck(name="ci / test", state="pending", link="https://ci/run/1")]
            )
        self.train_results.append(result)
        return result

    async def commit_checks(self, credentials, repo: str, sha: str) -> list:
        if getattr(self, "checks_error", ""):
            raise RunnerError(self.checks_error)
        return list(self.commit_checks_of.get(sha, []))

    async def job_timings(self, credentials, repo: str, sha: str) -> list:
        return list(self.job_timings_of.get(sha, []))

    async def rerun_failed(self, credentials, repo: str, run_ids: list[int]) -> int:
        self.reruns.append(list(run_ids))
        return len(run_ids)

    async def check_train(self, spec) -> JobState:
        self.check_specs.append(spec)
        self.jobs[spec.id] = JobState(running=True, log_path=f"/jobs/{spec.id}.log")
        return self.jobs[spec.id]

    async def land_train(self, spec) -> CheckResult:
        self.land_specs.append(spec)
        return CheckResult(ok=not self.land_error, detail=self.land_error or "pushed")

    async def pull_request(self, credentials, repo: str, number: int) -> PullRequestState:
        if (repo, number) not in self.pulls:
            raise RunnerError("no such pull request")
        return self.pulls[(repo, number)]

    async def merge_pull_request(self, credentials, repo: str, number: int, **options) -> CheckResult:
        if self.merge_error:
            return CheckResult(ok=False, detail=self.merge_error)
        self.merged.append((repo, number, options))
        self.pulls[(repo, number)] = self.pulls[(repo, number)].model_copy(update={"state": "merged"})
        return CheckResult(ok=True, detail="merge")

    async def close_pull_request(self, credentials, repo: str, number: int) -> None:
        self.closed.append(number)

    async def upstream(self, credentials, repository: str, branch: str, commit: str):
        self.upstream_calls.append((repository, branch, commit))
        if self.upstream_error:
            raise RunnerError(self.upstream_error)
        return self.upstream_state

    async def providers(self) -> list[ProviderInfo]:
        return [
            ProviderInfo(
                name="claude_code",
                label="Claude Code",
                models=[ModelInfo(id="claude-sonnet-5-5", label="Sonnet", tier="medium"), ModelInfo(id="claude-haiku-5-5", label="Haiku", tier="small")],
            ),
            ProviderInfo(
                name="codex",
                label="Codex",
                models=[ModelInfo(id="gpt-6.1-sol", label="Sol", tier="large"), ModelInfo(id="gpt-6.1-terra", label="Terra", tier="medium"), ModelInfo(id="gpt-6-luna", label="Luna", tier="small")],
            ),
            ProviderInfo(
                name="antigravity",
                label="Antigravity",
                models=[
                    ModelInfo(id="gemini-4-neon", label="Gemini 4 Neon", tier="xlarge"),
                    ModelInfo(id="gemini-4-carbon", label="Gemini 4 Carbon", tier="large"),
                    ModelInfo(id="gemini-4-argon", label="Gemini 4 Argon", tier="medium"),
                    ModelInfo(id="gemini-3.8-flash", label="Gemini 3.8 Flash", tier="small"),
                ],
                efforts=["minimal", "low", "medium", "high", "xhigh", "max"],
            ),
            # Providers that are an address. Agents can be given them once an account names a model.
            ProviderInfo(name="ollama", label="Ollama", models=[], endpoint=EndpointInfo(url="http://host.docker.internal:11434", key=False, hint="A model server of your own.")),
            ProviderInfo(name="gemini", label="Gemini (Google)", models=[], endpoint=EndpointInfo(url="https://generativelanguage.googleapis.com/v1beta/openai")),
        ]

    def forget_providers(self) -> None:
        self.providers_forgotten = getattr(self, "providers_forgotten", 0) + 1

    async def provider_account(self, provider: str, key: str, action: str | None = None, *, code: str = ""):
        from plainboard_protocol import ProviderAccount

        self.account_actions = getattr(self, "account_actions", []) + [(provider, key, action, code)]
        signed = getattr(self, "signed_in_accounts", set())
        self.signed_in_accounts = signed
        if action == "logout":
            signed.discard((provider, key))
            return ProviderAccount()
        if action == "login":
            if code:
                signed.add((provider, key))
                return ProviderAccount(signed_in=True, detail="user@example.com")
            if provider == "antigravity":
                return ProviderAccount(signed_in=False, login_url="https://accounts.google.com/o/oauth2/auth?state=test", login_input=True)
            return ProviderAccount(signed_in=False, login_url="https://auth.openai.com/device", login_code="ABCD-1234")
        return ProviderAccount(signed_in=(provider, key) in signed, detail="user@example.com" if (provider, key) in signed else "")

    async def provider_models(self, provider, credentials):
        self.models_asked = (provider, credentials)
        return ["a-model", "b-model"]

    async def check_provider(self, spec):
        return CheckResult(ok=True, detail="fake")

    async def github_create_repo(self, credentials, name, owner="", private=True):
        self.github_token_used = credentials.github_token
        full = f"{owner or 'me'}/{name}"
        return {"ok": True, "detail": "", "repos": [{"full_name": full, "name": name, "clone_url": f"https://github.com/{full}.git", "private": private}]}

    async def git_log(self, spec):
        """The commits of a repository's base branch: ``self.commits`` (newest first) after ``spec.since``, or
        those of ``spec.upto`` after its ``spec.skip`` newest."""
        self.git_log_specs = getattr(self, "git_log_specs", []) + [spec]
        commits = list(getattr(self, "commits", []))
        shas = [c.sha for c in commits]
        head, total = (shas or [""])[0], len(commits)
        if spec.upto and spec.upto in shas:
            head, commits = spec.upto, commits[shas.index(spec.upto) :][spec.skip :]
        elif spec.since and spec.since in shas:
            commits = commits[: shas.index(spec.since)]
        return GitLogResult(branch=spec.base or "main", head=head, total=total, commits=commits[: spec.limit])

    async def git_present(self, spec):
        """Every commit asked is on the base branch and every path there, except ``self.gone`` (shas whose files went too)."""
        from plainboard_protocol import GitPresentResult

        gone = getattr(self, "gone", set())
        return GitPresentResult(on_base=[sha for sha in spec.shas if sha not in gone], missing=list(spec.paths) if gone else [])

    async def tidy_branches(self, spec):
        self.tidy_specs = getattr(self, "tidy_specs", []) + [spec]
        removed = [TidyBranch(name=b, sha=f"{i + 1:040x}", how="squashed") for i, b in enumerate(getattr(self, "merged_branches", [])) if b not in spec.keep]
        return TidyResult(deleted=[b.name for b in removed], removed=removed, unmerged=getattr(self, "stale_branches", []))

    async def remove_files(self, spec):
        self.removed = getattr(self, "removed", []) + [spec]
        while getattr(self, "remove_holds", False):  # large folders: it takes a while
            await asyncio.sleep(0.01)
        return CheckResult(ok=not getattr(self, "remove_fails", False), detail="2 folders removed" if not getattr(self, "remove_fails", False) else "Permission denied")

    async def check_github(self, credentials, repos=None):
        return CheckResult(ok=bool(credentials.github_token), detail="fake")

    async def github_repos(self, credentials):
        self.github_token_used = credentials.github_token
        if not credentials.github_token:
            return {"ok": False, "detail": "No GitHub token configured", "repos": []}
        return {"ok": True, "detail": "", "repos": [{"full_name": "acme/api", "name": "api", "clone_url": "https://github.com/acme/api.git"}]}

    async def clean_up(self, spec):
        self.cleanups = [*getattr(self, "cleanups", []), spec]
        return getattr(self, "cleanup_result", None) or CleanupResult()

    async def cleanup_candidates(self, spec):
        self.candidate_specs = [*getattr(self, "candidate_specs", []), spec]
        return [c.model_copy() for c in getattr(self, "candidates", [])]

    async def unpushed_work(self, spec):
        self.unpushed_specs = [*getattr(self, "unpushed_specs", []), spec]
        return list(getattr(self, "unpushed", []))

    async def trim_caches(self, spec):
        self.trims = [*getattr(self, "trims", []), spec]
        return getattr(self, "trim_result", None) or CleanupResult()

    async def cleanup_delete(self, spec):
        """Like the runner: busy and unsure only with force, kept never."""
        self.deletes = [*getattr(self, "deletes", []), spec]
        found = {c.path: c for c in getattr(self, "candidates", [])}
        result = CleanupResult()
        for path in spec.paths:
            c = found[path]
            if c.safety == "keep" or (c.safety != "safe" and not spec.force):
                result.skipped.append(f"{path}: {c.safety}")
            else:
                result.items.append(CleanedItem(path=path, what=c.what, bytes=c.bytes))
                result.freed += c.bytes
                self.candidates = [x for x in self.candidates if x.path != path]
        return result

    async def usage(self, measure: bool = False):
        if not self.reachable:
            raise RunnerError("Runner unreachable")
        self.usage_measured = getattr(self, "usage_measured", 0) + measure
        return getattr(self, "machine_usage", None) or MachineUsage()

    async def aclose(self) -> None:
        pass


def _with_free_database(method):
    @functools.wraps(method)
    async def call(self, *args, **kwargs):
        self.database_free(method.__name__)
        return await method(self, *args, **kwargs)

    return call


for _name, _method in list(vars(FakeRunner).items()):
    if inspect.iscoroutinefunction(_method) and _name not in ("database_free", "aclose"):
        setattr(FakeRunner, _name, _with_free_database(_method))


ADMIN_PASSWORD = "admin-password"


POSTGRES = bool(os.environ.get("PLAINBOARD_DATABASE"))  # the whole suite runs against PostgreSQL


def pytest_collection_modifyitems(config, items) -> None:  # noqa: ANN001
    """PLAINBOARD_TEST_SHARD=i/n (CI): only every n-th test, starting with the i-th, so that n jobs
    together run each test once. Every n-th rather than a block: the slow tests stand together."""
    shard = os.environ.get("PLAINBOARD_TEST_SHARD", "")
    if not shard:
        return
    index, count = (int(part) for part in shard.split("/"))
    config.hook.pytest_deselected(items=[item for i, item in enumerate(items) if i % count != index - 1])
    items[:] = [item for i, item in enumerate(items) if i % count == index - 1]


def pytest_configure(config) -> None:  # noqa: ANN001
    """Run in parallel (pytest -n, pytest-xdist): every worker is a process of its own and gets a
    PostgreSQL database of its own, next to the one named, as the tests empty theirs. With SQLite
    each test has a file of its own anyway."""
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not POSTGRES or not worker:
        return
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from plainboard import config as app_config

    url = make_url(app_config.Config().sync_database_url)
    name = f"{url.database}_{worker}"
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    os.environ["PLAINBOARD_DATABASE"] = url.set(drivername="postgresql", database=name).render_as_string(hide_password=False)
    app_config.get_config.cache_clear()


# The database a new installation starts with, made once per test run (see new_installation):
# SQLite: the file to copy. PostgreSQL: the tables are there and only have to be emptied.
_made: dict = {"sqlite": None, "postgres": False}


def empty_database() -> None:
    """PostgreSQL without any tables, for a test that starts an app of its own; with SQLite each test has a file of its own anyway."""
    if not POSTGRES:
        return
    from sqlalchemy import create_engine, text

    from plainboard import config

    _made["postgres"] = False
    engine = create_engine(config.get_config().sync_database_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def open_transactions() -> list:
    """PostgreSQL, after the app stopped: the connections to the test's database that are still in a
    transaction. A task the app did not wait for kept one past the engine's end, with its locks: the next
    test's TRUNCATE would wait for them forever. Ended, as is every other connection still open (one that
    is just closing is idle)."""
    if not POSTGRES:
        return []
    from sqlalchemy import create_engine, text

    from plainboard import config

    engine = create_engine(config.get_config().sync_database_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            others = "FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() AND backend_type = 'client backend'"
            rows = conn.execute(text(f"SELECT state, left(query, 300), pg_terminate_backend(pid) {others}")).all()
    finally:
        engine.dispose()
    return [(state, query) for state, query, _ in rows if state != "idle"]


def new_installation(monkeypatch) -> None:
    """The database of a new installation, for the app the test is about to start. The migrations run
    for the first test only: run for each, they were most of a test's time (1.6 of about 2 seconds).
    After that SQLite gets a copy of the first test's file, and PostgreSQL has its tables emptied and
    the rows of a new installation put back. The app's own migration at its start is left out."""
    from sqlalchemy import create_engine, inspect, text

    from plainboard import config, main, migrate

    cfg = config.get_config()
    if POSTGRES and _made["postgres"]:
        engine = create_engine(cfg.sync_database_url)
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '30s'"))  # a lock left by someone fails the test, not the run
                tables = [name for name in inspect(conn).get_table_names() if name != "alembic_version"]
                conn.execute(text("TRUNCATE " + ", ".join(f'"{name}"' for name in tables) + " RESTART IDENTITY CASCADE"))
            migrate._first_rows(engine)
        finally:
            engine.dispose()
    elif POSTGRES:
        empty_database()
        migrate.upgrade()
        _made["postgres"] = True
    elif _made["sqlite"] is None:
        migrate.upgrade()
        _made["sqlite"] = Path(tempfile.mkdtemp(prefix="plainboard-tests-")) / "plainboard.db"
        shutil.copyfile(cfg.data_dir / "plainboard.db", _made["sqlite"])
    else:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_made["sqlite"], cfg.data_dir / "plainboard.db")
    monkeypatch.setattr(main, "upgrade", lambda: None)


@pytest.fixture
def lands_on_red(monkeypatch):
    """The time after the hold: finished branches land although the base branch is red. What happens then
    (which repair is ordered, and when the manager decides) is what these tests are about."""
    from plainboard.services import landings

    monkeypatch.setattr(landings, "RED_HOLD_SECONDS", 0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAINBOARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PLAINBOARD_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("PLAINBOARD_STATIC_DIR", str(tmp_path / "static"))
    monkeypatch.setenv("PLAINBOARD_SCHEDULER_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("PLAINBOARD_JOB_WATCH_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("PLAINBOARD_LANDING_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("PLAINBOARD_LANDING_LINGER_SECONDS", "0")
    monkeypatch.setenv("PLAINBOARD_SEARCH_INTERVAL_SECONDS", "3600")  # tests index with app.state.search.tick
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "PLAINBOARD_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    from plainboard import config, main

    config.get_config.cache_clear()
    new_installation(monkeypatch)
    monkeypatch.setattr(main, "RunnerClient", FakeRunner)
    app = main.create_app()
    FakeRunner.app = app
    FakeRunner.held = []
    _writes.clear()
    with TestClient(app) as test_client:
        test_client.app_ref = app
        # A fresh installation has no users: its first admin is who the tests work as.
        assert test_client.post("/api/auth/setup", json={"username": "admin", "name": "Admin", "password": ADMIN_PASSWORD}).status_code == 201
        # Direct messages to members are off by default; most tests write to members, so they start with it on.
        assert test_client.put("/api/settings", json={"values": {"member_chat": True}}).status_code == 200
        yield test_client
    left = open_transactions()
    config.get_config.cache_clear()
    assert not FakeRunner.held, f"The runner was called between a write to the database and its commit: {sorted(set(FakeRunner.held))}"
    assert not left, f"The app left a transaction open when it stopped (a task it did not wait for): {left}"


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


@pytest.fixture
def project(client):
    resp = client.post("/api/projects", json={"name": "Demo Project", "description": "A test project"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def serve_preview(client, project_id: int, port: int) -> int:
    """A server job of the project that runs and listens on the port, as an agent would leave one. Returns its id."""
    from plainboard.db import sessionmaker
    from plainboard.models import Job

    agent_id = agents_by_slug(client, project_id)["manager"]["id"]
    runner_id = f"srv{project_id}x{port}x{time.monotonic_ns()}"

    async def add() -> int:
        async with sessionmaker()() as session:
            job = Job(project_id=project_id, agent_id=agent_id, runner_id=runner_id, command="npm run dev")
            session.add(job)
            await session.commit()
            return job.id

    job_id = client.portal.call(add)
    client.app_ref.state.runner.listening[runner_id] = [port]
    client.app_ref.state.preview_owners._at = -60.0  # asked again at once
    return job_id


def end_job(client, job_id: int) -> None:
    from plainboard.db import sessionmaker
    from plainboard.models import Job, JobStatus

    async def end() -> None:
        async with sessionmaker()() as session:
            job = await session.get(Job, job_id)
            job.status = JobStatus.STOPPED
            await session.commit()

    client.portal.call(end)


def agents_by_slug(client, project_id: int) -> dict:
    return {a["slug"]: a for a in client.get(f"/api/projects/{project_id}/agents").json()}


def runs_of(client, project_id: int, **params) -> list[dict]:
    return client.get(f"/api/projects/{project_id}/runs", params=params).json()


def idle(client, project_id: int) -> bool:
    return not runs_of(client, project_id, active=True)
