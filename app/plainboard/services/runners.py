"""External runners: a runner on another machine that connects out to PlainBoard with a token of its
own (orchestration/runners.py routes each project to its runner, api/runners.py holds the socket).

Setting one up is one command on that machine; nothing listens there. The token is shown once and
kept as a hash. A project moves to another runner only while nothing of it runs there: its
workspaces don't move (the new runner clones the repositories again), and the home is per runner."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import UTC, datetime

from plainboard_protocol import UnpushedSpec
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..models import Agent, Conversation, InboxItem, Job, JobStatus, Project, Run, RunStatus, Runner, Train, TrainStatus, utcnow
from ..schemas import ProjectRunnerOut, RunnerChoice, RunnerCreated, RunnerOut, UnpushedItem, UnpushedOut
from . import cleanup as cleanup_service
from . import version as version_service
from .errors import NotFound, ServiceError

IMAGE = "thoenehannes/plainboard-runner"
TOKEN_PREFIX = "pbr_"
BUILTIN = "Built-in runner"
UNPUSHED_SECONDS = 90  # the old runner's look at the workspaces' clones
MOVED_NOTE = "Your project moved to another runner: workspaces were cloned fresh, the home is new, tools you installed must be installed again."
AWAY_TELL_SECONDS = 600  # a runner away this long: the managers of its projects hear it, once
SEEN_SECONDS = 60  # while connected, last_seen_at is written at most this often: an app restart loses no more


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def image() -> str:
    """The runner image of PlainBoard's own channel: :latest for a release, :edge for a build of a commit,
    so that ``docker compose pull`` brings the runner what PlainBoard's version needs."""
    return f"{IMAGE}:{'latest' if version_service.running_version() else 'edge'}"


def command(url: str, token: str) -> str:
    return (
        "docker run -d --name plainboard-runner --restart unless-stopped \\\n"
        f"  -e PLAINBOARD_URL={url} \\\n"
        f"  -e PLAINBOARD_RUNNER_TOKEN={token} \\\n"
        "  -v plainboard-workspaces:/workspaces -v plainboard-home:/home/agent -v plainboard-uploads:/uploads \\\n"
        f"  {image()}"
    )


def compose(url: str, token: str) -> str:
    return (
        "services:\n"
        "  runner:\n"
        f"    image: {image()}\n"
        "    container_name: plainboard-runner\n"
        "    restart: unless-stopped\n"
        "    environment:\n"
        f"      PLAINBOARD_URL: {url}\n"
        f"      PLAINBOARD_RUNNER_TOKEN: {token}\n"
        "    volumes:\n"
        "      - workspaces:/workspaces\n"
        "      - agent-home:/home/agent\n"
        "      - uploads:/uploads\n"
        "volumes:\n"
        "  workspaces:\n"
        "  agent-home:\n"
        "  uploads:\n"
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
                updating=r.id in hub.updating,
                restart_by=datetime.fromtimestamp(hub.restart_by[r.id], UTC).replace(tzinfo=None) if r.id in hub.updating and r.id in hub.restart_by else None,
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
    from . import permissions

    owner = await permissions.owner(session, project)  # only where its owner's projects may run, and where it is
    runners = [r for r in await session.scalars(select(Runner).order_by(Runner.name, Runner.id)) if r.id == project.runner_id or permissions.may_run_on(owner, r.id)]
    builtin = [RunnerChoice(id=None, name=BUILTIN, connected=True)] if project.runner_id is None or permissions.may_run_on(owner, None) else []
    return ProjectRunnerOut(
        runner_id=project.runner_id,
        runners=[*builtin, *(RunnerChoice(id=r.id, name=r.name, connected=hub.connected(r.id)) for r in runners)],
        busy=await busy(session, project.id),
    )


async def for_new_project(session: AsyncSession, hub, user) -> list[RunnerChoice]:
    """Where a new project of this person may start: the built-in runner first, if allowed."""
    from . import permissions

    builtin = [RunnerChoice(id=None, name=BUILTIN, connected=True)] if permissions.may_run_on(user, None) else []
    runners = [r for r in await session.scalars(select(Runner).order_by(Runner.name, Runner.id)) if permissions.may_run_on(user, r.id)]
    return [*builtin, *(RunnerChoice(id=r.id, name=r.name, connected=hub.connected(r.id)) for r in runners)]


async def unpushed(session: AsyncSession, hub, project: Project) -> UnpushedOut:
    """What stays behind on a move: work in the agents' workspaces on the current runner that is on no remote."""
    from ..orchestration.context import workspace_path

    name = BUILTIN if project.runner_id is None else (await get(session, project.runner_id)).name
    agents = list(await session.scalars(select(Agent).where(Agent.project_id == project.id).order_by(Agent.name)))
    folders: dict[str, list[str]] = {}
    for agent in agents:
        folders.setdefault(workspace_path(project, agent), []).append(agent.name)
    await session.commit()  # nothing is held while the runner looks
    if not hub.connected(project.runner_id):
        return UnpushedOut(runner=name, checked=False, detail="The runner is not connected.")
    try:
        found = await asyncio.wait_for(hub.client(project.runner_id).unpushed_work(UnpushedSpec(folders=list(folders))), UNPUSHED_SECONDS)
    except Exception as exc:
        return UnpushedOut(runner=name, checked=False, detail=str(exc)[:300] or type(exc).__name__)
    return UnpushedOut(runner=name, checked=True, items=[UnpushedItem(agents=folders.get(w.folder, []), repo=w.repo, problems=w.problems) for w in found])


async def assign(session: AsyncSession, hub, project: Project, runner_id: int | None) -> Project:
    """Move a project to another runner. Refused while something of it runs: that runs on the old one."""
    if runner_id == project.runner_id:
        return project
    if runner_id is not None:
        await get(session, runner_id)
    if reason := await busy(session, project.id):
        raise ServiceError(reason, 409)
    project.runner_id = runner_id
    # The sessions are in the old runner's home: the next turns start fresh (summary and recent messages).
    agents = list(await session.scalars(select(Agent.id).where(Agent.project_id == project.id)))
    await session.execute(update(Conversation).where(Conversation.agent_id.in_(agents)).values(provider_session_id=None))
    session.add_all(InboxItem(agent_id=a, text=MOVED_NOTE) for a in agents)  # read with the next turn; none is spent on it
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
