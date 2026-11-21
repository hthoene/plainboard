"""External runners: a runner on another machine that connects out to PlainBoard with a token of its
own (orchestration/runners.py routes each project to its runner, api/runners.py holds the socket).

Setting one up is one command on that machine; nothing listens there. The token is shown once and
kept as a hash. A project moves to another runner only while nothing of it runs there: its
workspaces don't move (the new runner clones the repositories again), and the home is per runner."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..models import Job, JobStatus, Project, Run, RunStatus, Runner, Train, TrainStatus, utcnow
from ..schemas import ProjectRunnerOut, RunnerChoice, RunnerCreated, RunnerOut
from . import cleanup as cleanup_service
from . import version as version_service
from .errors import NotFound, ServiceError

IMAGE = "thoenehannes/plainboard-runner:latest"
TOKEN_PREFIX = "pbr_"
BUILTIN = "Built-in runner"
AWAY_TELL_SECONDS = 600  # a runner away this long: the managers of its projects hear it, once


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def command(url: str, token: str) -> str:
    return (
        "docker run -d --name plainboard-runner --restart unless-stopped \\\n"
        f"  -e PLAINBOARD_URL={url} \\\n"
        f"  -e PLAINBOARD_RUNNER_TOKEN={token} \\\n"
        "  -v plainboard-workspaces:/workspaces -v plainboard-home:/home/agent \\\n"
        f"  {IMAGE}"
    )


def compose(url: str, token: str) -> str:
    return (
        "services:\n"
        "  runner:\n"
        f"    image: {IMAGE}\n"
        "    container_name: plainboard-runner\n"
        "    restart: unless-stopped\n"
        "    environment:\n"
        f"      PLAINBOARD_URL: {url}\n"
        f"      PLAINBOARD_RUNNER_TOKEN: {token}\n"
        "    volumes:\n"
        "      - workspaces:/workspaces\n"
        "      - agent-home:/home/agent\n"
        "volumes:\n"
        "  workspaces:\n"
        "  agent-home:\n"
    )


async def get(session: AsyncSession, runner_id: int) -> Runner:
    runner = await session.get(Runner, runner_id)
    if runner is None:
        raise NotFound("Runner not found")
    return runner


async def authenticate(session: AsyncSession, token: str) -> Runner | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    return await session.scalar(select(Runner).where(Runner.token_hash == _hash(token)))


async def create(session: AsyncSession, name: str) -> tuple[Runner, str]:
    token = new_token()
    runner = Runner(name=name.strip(), token_hash=_hash(token))
    session.add(runner)
    await session.commit()
    return runner, token


async def rename(session: AsyncSession, runner: Runner, name: str) -> Runner:
    runner.name = name.strip()
    await session.commit()
    return runner


async def new_secret(session: AsyncSession, runner: Runner) -> str:
    """A new token; the old one stops working (its connection is ended by the caller)."""
    token = new_token()
    runner.token_hash = _hash(token)
    await session.commit()
    return token


async def remove(session: AsyncSession, runner: Runner) -> None:
    names = list(await session.scalars(select(Project.name).where(Project.runner_id == runner.id).order_by(Project.name)))
    if names:
        raise ServiceError(f"Projects still run on it: {', '.join(names)}. Move them to another runner first.")
    await session.delete(runner)
    await session.commit()


def short(version: str) -> str:
    return version.strip().lower()[:10]


def version_ok(version: str) -> bool:
    own = version_service.running_commit()
    return not version or not own or short(version) == short(own)


async def listing(session: AsyncSession, hub) -> list[RunnerOut]:
    """``hub``: the app's ``Runners``. Disk and memory are asked of the connected runners at once, briefly."""
    runners = list(await session.scalars(select(Runner).order_by(Runner.name, Runner.id)))
    projects: dict[int, list[str]] = {}
    for rid, name in (await session.execute(select(Project.runner_id, Project.name).where(Project.runner_id.is_not(None)).order_by(Project.name))).all():
        projects.setdefault(rid, []).append(name)
    await session.commit()  # nothing is held while the runners are asked

    async def usage(runner_id: int):
        try:
            return await asyncio.wait_for(hub.client(runner_id).usage(), 8)
        except Exception:
            return None

    found = await asyncio.gather(*(usage(r.id) for r in runners if hub.connected(r.id)))
    machines = dict(zip([r.id for r in runners if hub.connected(r.id)], found))
    out = []
    root = get_config().workspaces_root
    for r in runners:
        connected = hub.connected(r.id)
        version = (hub.hello.get(r.id) or {}).get("version") or r.version if connected else r.version
        machine = machines.get(r.id)
        volume = cleanup_service.workspace_volume(machine, root) if machine else None
        out.append(
            RunnerOut(
                id=r.id,
                name=r.name,
                connected=connected,
                version=short(version or ""),
                version_ok=version_ok(version or ""),
                last_seen_at=utcnow() if connected else r.last_seen_at,
                created_at=r.created_at,
                projects=projects.get(r.id, []),
                disk_total=volume.total if volume else 0,
                disk_used=volume.used if volume else 0,
                memory_total=machine.memory_total if machine else 0,
                memory_used=machine.memory_used if machine else 0,
            )
        )
    return out


async def created(session: AsyncSession, hub, runner: Runner, token: str, url: str) -> RunnerCreated:
    out = next(r for r in await listing(session, hub) if r.id == runner.id)
    return RunnerCreated(runner=out, token=token, command=command(url, token), compose=compose(url, token))


async def busy(session: AsyncSession, project_id: int) -> str:
    """Why the project can't move to another runner now; empty: it can."""
    turns = await session.scalar(select(func.count(Run.id)).where(Run.project_id == project_id, Run.status == RunStatus.RUNNING))
    jobs = await session.scalar(select(func.count(Job.id)).where(Job.project_id == project_id, Job.status == JobStatus.RUNNING))
    trains = await session.scalar(select(func.count(Train.id)).where(Train.project_id == project_id, Train.status == TrainStatus.CHECKING))
    parts = [f"{n} {what}" for n, what in ((turns, "running turn(s)"), (jobs, "running job(s)"), (trains, "check run(s) of its branches")) if n]
    return f"Not while it has {', '.join(parts)}: pause the project or let them end first." if parts else ""


async def choices(session: AsyncSession, hub, project: Project) -> ProjectRunnerOut:
    runners = list(await session.scalars(select(Runner).order_by(Runner.name, Runner.id)))
    return ProjectRunnerOut(
        runner_id=project.runner_id,
        runners=[RunnerChoice(id=None, name=BUILTIN, connected=True), *(RunnerChoice(id=r.id, name=r.name, connected=hub.connected(r.id)) for r in runners)],
        busy=await busy(session, project.id),
    )


async def assign(session: AsyncSession, hub, project: Project, runner_id: int | None) -> Project:
    """Move a project to another runner. Refused while something of it runs: that runs on the old one."""
    if runner_id == project.runner_id:
        return project
    if runner_id is not None:
        await get(session, runner_id)
    if reason := await busy(session, project.id):
        raise ServiceError(reason, 409)
    project.runner_id = runner_id
    await session.commit()
    hub.assign(project.id, runner_id)
    return project


async def seen(session: AsyncSession, runner_id: int, version: str | None = None, now: datetime | None = None) -> Runner | None:
    """Connected, or the connection ended: the time, and the version it said it runs."""
    runner = await session.get(Runner, runner_id)
    if runner is None:
        return None
    runner.last_seen_at = now or utcnow()
    if version is not None:
        runner.version = version[:64]
    await session.commit()
    return runner
