"""What an agent left running outside its jobs: detached processes (``&``, nohup, setsid, daemons) and
Docker containers it started. Agents may start them; when their project or they themselves are paused
or stopped, the app asks for them here and they are stopped too (#187).

A turn carries PLAINBOARD_AGENT (its agent) and PLAINBOARD_RUN (the turn) in its environment, a job
PLAINBOARD_AGENT and PLAINBOARD_JOB, and every process they start inherits them, detached or not. A
process without them (its environment was cleared, or it predates them) counts by its working directory:
inside the agent's own workspace. One whose environment cannot be read as this user (sudo) is read with
``sudo -n`` where that works. Spared: what belongs to a turn or a job that still runs, another agent's,
the runner itself and its ancestors.

Containers: only with a Docker the runner reaches (``docker`` on the PATH and its daemon answering). One
is the agent's if it was started from its workspace (the compose project's folder) or mounts a folder
inside it, and nothing above it (PlainBoard's own containers, or one that sees other workspaces).
They are stopped, not removed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from plainboard_protocol import Leftover, LeftoverOwner, LeftoverResult, LeftoverSpec

from . import jobs
from .config import WORKSPACES_ROOT

log = logging.getLogger(__name__)

AGENT, RUN, JOB = "PLAINBOARD_AGENT", "PLAINBOARD_RUN", "PLAINBOARD_JOB"
STOP_GRACE_SECONDS = 10
SUDO_SECONDS = 10
DOCKER_SECONDS = 20
MAX_LABEL = 160

_tasks: set[asyncio.Task[None]] = set()


@dataclass
class Proc:
    pid: int
    ppid: int
    session: int
    started: float  # unix time
    cmd: str
    cwd: str | None = None
    env: dict[str, str] | None = None  # only the PLAINBOARD_ ones; None: not readable
    uid: int = -1


@dataclass
class Found:
    agent: str
    proc: Proc | None = None
    container: str = ""
    label: str = ""


def _boot() -> float:
    try:
        with open("/proc/stat", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except OSError:
        pass
    return 0.0


def _marks(raw: bytes) -> dict[str, str]:
    out = {}
    for item in raw.split(b"\0"):
        name, sep, value = item.partition(b"=")
        if sep and name.startswith(b"PLAINBOARD_"):
            out[name.decode(errors="replace")] = value.decode(errors="replace")
    return out


def _read(pid: int, boot: float, ticks: int) -> Proc | None:
    base = f"/proc/{pid}"
    try:
        with open(f"{base}/stat", encoding="utf-8", errors="replace") as fh:
            stat = fh.read()
        with open(f"{base}/cmdline", "rb") as fh:
            cmd = fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
        uid = os.stat(base).st_uid
    except OSError:
        return None
    rest = stat[stat.rindex(")") + 2 :].split()
    if not cmd or rest[0] == "Z":  # a kernel thread, or already over
        return None
    proc = Proc(pid=pid, ppid=int(rest[1]), session=int(rest[3]), started=boot + int(rest[19]) / ticks, cmd=cmd, uid=uid)
    try:
        proc.cwd = os.readlink(f"{base}/cwd")
    except OSError:
        pass
    try:
        with open(f"{base}/environ", "rb") as fh:
            proc.env = _marks(fh.read())
    except OSError:
        pass
    return proc


def _with_sudo(procs: list[Proc]) -> None:
    """Fill in what only another user may read (a process started with sudo), in one call of ``sudo -n``."""
    if not procs or os.geteuid() == 0 or not shutil.which("sudo"):
        return
    script = 'for p; do printf "@@%s\\n" "$p"; readlink "/proc/$p/cwd"; tr "\\0" "\\n" < "/proc/$p/environ" | grep "^PLAINBOARD_"; done; true'
    try:
        out = subprocess.run(
            ["sudo", "-n", "sh", "-c", script, "sh", *(str(p.pid) for p in procs)], capture_output=True, text=True, timeout=SUDO_SECONDS, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return
    by_pid = {p.pid: p for p in procs}
    current: Proc | None = None
    for line in out.splitlines():
        if line.startswith("@@"):
            current = by_pid.get(int(line[2:])) if line[2:].isdigit() else None
            if current is not None:
                current.env = {}
        elif current is not None:
            name, sep, value = line.partition("=")
            if sep and name.startswith("PLAINBOARD_"):
                current.env[name] = value  # type: ignore[index]
            elif line.startswith("/") and current.cwd is None:
                current.cwd = line


def _processes() -> list[Proc]:
    boot, ticks = _boot(), os.sysconf("SC_CLK_TCK")
    procs = [p for name in os.listdir("/proc") if name.isdigit() and (p := _read(int(name), boot, ticks)) is not None]
    _with_sudo([p for p in procs if p.env is None])
    return procs


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _owner(proc: Proc, owners: list[LeftoverOwner], agents: set[str]) -> str | None:
    marked = (proc.env or {}).get(AGENT)
    if marked:
        return marked if marked in agents else None  # another agent's: never by its folder
    if proc.cwd:
        for owner in owners:
            if owner.workspace and _inside(proc.cwd, owner.workspace):
                return owner.agent
    return None


def find_processes(owners: list[LeftoverOwner], active_runs: set[str], before: float | None = None, procs: list[Proc] | None = None) -> list[Found]:
    """The owners' processes that no running turn or job accounts for."""
    procs = _processes() if procs is None else procs
    by_pid = {p.pid: p for p in procs}
    spared = {1}
    pid = os.getpid()
    while pid > 1 and pid not in spared:  # the runner and what started it
        spared.add(pid)
        pid = by_pid[pid].ppid if pid in by_pid else 1
    job_sessions = set(jobs.sessions(jobs.running_ids()))
    agents = {o.agent for o in owners}
    found = []
    for proc in procs:
        if proc.pid in spared or proc.session in job_sessions:
            continue
        env = proc.env or {}
        if env.get(RUN) in active_runs or (env.get(JOB) and jobs.is_running(env[JOB])):
            continue
        if before is not None and proc.started >= before:
            continue
        if (agent := _owner(proc, owners, agents)) is not None:
            found.append(Found(agent=agent, proc=proc, label=proc.cmd[:MAX_LABEL]))
    return found


def _signal(procs: list[Proc], sig: signal.Signals) -> None:
    denied = []
    for proc in procs:
        try:
            os.kill(proc.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            denied.append(proc.pid)
    if denied and shutil.which("sudo"):
        try:
            subprocess.run(["sudo", "-n", "kill", f"-{sig.name[3:]}", *map(str, denied)], capture_output=True, timeout=SUDO_SECONDS, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


def _same(proc: Proc, boot: float, ticks: int) -> bool:
    """Still the process that was found (a pid is used again)."""
    now = _read(proc.pid, boot, ticks)
    return now is not None and abs(now.started - proc.started) < 1


async def _stop(procs: list[Proc]) -> None:
    _signal(procs, signal.SIGTERM)
    await asyncio.sleep(STOP_GRACE_SECONDS)
    boot, ticks = _boot(), os.sysconf("SC_CLK_TCK")
    _signal([p for p in procs if _same(p, boot, ticks)], signal.SIGKILL)


# --- Docker ------------------------------------------------------------------------------------------


async def _docker(*args: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec("docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), DOCKER_SECONDS)
    except TimeoutError:
        return 1, "docker did not answer in time"
    except OSError as exc:
        return 1, str(exc)
    return proc.returncode or 0, out.decode(errors="replace")


def _started(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value[:26].rstrip("Z") + "+00:00" if value.endswith("Z") else value).timestamp()
    except ValueError:
        return None


def container_owner(info: dict, owners: list[LeftoverOwner], own_host: str) -> str | None:
    """Whose container it is: started from one owner's workspace or mounting from inside it, and from nothing above."""
    if info.get("Id", "").startswith(own_host) or (info.get("Config") or {}).get("Hostname") == own_host:
        return None  # the runner's own
    labels = (info.get("Config") or {}).get("Labels") or {}
    dirs = [d for d in [labels.get("com.docker.compose.project.working_dir", "")] if d]
    dirs += [m.get("Source", "") for m in info.get("Mounts") or [] if m.get("Type") == "bind" and m.get("Source")]
    if not dirs:
        return None
    root = str(WORKSPACES_ROOT.resolve())
    if any(_inside(root, d) for d in dirs):
        return None  # sees all workspaces: PlainBoard's own
    matched = set()
    for owner in owners:
        if not owner.workspace:
            continue
        if any(_inside(owner.workspace, d) and d.rstrip("/") != owner.workspace.rstrip("/") for d in dirs):
            return None  # sees more than this agent's workspace
        if any(_inside(d, owner.workspace) for d in dirs):
            matched.add(owner.agent)
    return matched.pop() if len(matched) == 1 else None


async def find_containers(owners: list[LeftoverOwner], before: float | None = None) -> tuple[list[Found], str]:
    if not shutil.which("docker") or not any(o.workspace for o in owners):
        return [], ""
    code, out = await _docker("ps", "-q", "--no-trunc")
    if code:
        return [], f"Docker is not reachable from the runner: {out.strip().splitlines()[-1][:200] if out.strip() else 'no answer'}"
    ids = out.split()
    if not ids:
        return [], ""
    code, out = await _docker("inspect", *ids)
    if code:
        return [], f"docker inspect failed: {out.strip()[:200]}"
    found = []
    host = socket.gethostname()
    for info in json.loads(out or "[]"):
        if (agent := container_owner(info, owners, host)) is None:
            continue
        started = _started((info.get("State") or {}).get("StartedAt", ""))
        if before is not None and started is not None and started >= before:
            continue
        name = (info.get("Name") or "").lstrip("/") or info["Id"][:12]
        found.append(Found(agent=agent, container=info["Id"], label=f"{name} ({(info.get('Config') or {}).get('Image', '?')})"[:MAX_LABEL]))
    return found, ""


async def _stop_containers(ids: list[str]) -> None:
    code, out = await _docker("stop", "-t", str(STOP_GRACE_SECONDS), *ids)
    if code:
        log.warning("Stopping containers %s failed: %s", ids, out[:300])


# --- both ---------------------------------------------------------------------------------------------


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def leftovers(spec: LeftoverSpec, active_runs: set[str]) -> LeftoverResult:
    owners = [o.model_copy(update={"workspace": str(Path(o.workspace).resolve())}) if o.workspace else o for o in spec.owners]
    processes = await asyncio.to_thread(find_processes, owners, active_runs, spec.before)
    containers, docker = await find_containers(owners, spec.before)
    if spec.stop:
        if processes:
            _spawn(_stop([f.proc for f in processes if f.proc]))
            log.info("Stopping leftover processes %s", [f.proc.pid for f in processes if f.proc])
        if containers:
            _spawn(_stop_containers([f.container for f in containers]))
            log.info("Stopping leftover containers %s", [f.container[:12] for f in containers])
    found = [Leftover(agent=f.agent, kind="process", id=str(f.proc.pid), label=f.label) for f in processes if f.proc]
    found += [Leftover(agent=f.agent, kind="container", id=f.container[:12], label=f.label) for f in containers]
    return LeftoverResult(found=found, docker=docker)

