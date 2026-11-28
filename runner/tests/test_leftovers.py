"""#187: which processes and containers an agent left running, found without touching anything real."""

import os

from plainboard_protocol import LeftoverOwner
from plainboard_runner import leftovers
from plainboard_runner.config import WORKSPACES_ROOT
from plainboard_runner.leftovers import Proc

ROOT = str(WORKSPACES_ROOT.resolve())
DEV, OPS = f"{ROOT}/p/dev", f"{ROOT}/p/ops"
OWNERS = [LeftoverOwner(agent="1", workspace=DEV)]


def proc(pid: int, env: dict | None = None, cwd: str | None = "/", started: float = 100.0, session: int = 0) -> Proc:
    return Proc(pid=pid, ppid=1, session=session or pid, started=started, cmd=f"cmd {pid}", cwd=cwd, env=env)


def test_an_agents_processes_are_found_by_its_mark_or_its_folder_and_nothing_else():
    procs = [
        proc(10, {"PLAINBOARD_AGENT": "1", "PLAINBOARD_RUN": "5"}, cwd="/tmp"),  # detached by an earlier turn: its mark
        proc(11, {"PLAINBOARD_AGENT": "1", "PLAINBOARD_RUN": "9"}),  # of the turn that runs now
        proc(12, None, cwd=f"{DEV}/app"),  # started with sudo: its folder
        proc(13, {"PLAINBOARD_AGENT": "2"}, cwd=f"{DEV}/app"),  # another agent's, even in this folder
        proc(14, {}, cwd=OPS),  # someone else's folder
        proc(15, {}, cwd=f"{DEV}-old"),  # a folder that only starts alike
        proc(16, {"PLAINBOARD_AGENT": "1"}, started=500.0),  # started after the pause began
        proc(os.getpid(), {"PLAINBOARD_AGENT": "1"}),  # the runner itself
    ]
    found = leftovers.find_processes(OWNERS, active_runs={"9"}, before=200.0, procs=procs)
    assert sorted(f.proc.pid for f in found) == [10, 12]
    assert {f.agent for f in found} == {"1"}


def test_a_shared_workspace_counts_only_by_the_mark():
    owners = [LeftoverOwner(agent="1")]
    found = leftovers.find_processes(owners, set(), procs=[proc(12, None, cwd=f"{DEV}/app"), proc(10, {"PLAINBOARD_AGENT": "1"})])
    assert [f.proc.pid for f in found] == [10]


def container(*sources: str, working_dir: str = "", cid: str = "c1") -> dict:
    labels = {"com.docker.compose.project.working_dir": working_dir} if working_dir else {}
    return {"Id": cid, "Config": {"Hostname": cid, "Labels": labels}, "Mounts": [{"Type": "bind", "Source": s} for s in sources]}


def test_a_container_is_an_agents_only_if_it_comes_clearly_from_its_workspace():
    owners = [LeftoverOwner(agent="1", workspace=DEV), LeftoverOwner(agent="2", workspace=OPS)]
    assert leftovers.container_owner(container(working_dir=f"{DEV}/app"), owners, "host") == "1"
    assert leftovers.container_owner(container(f"{OPS}/data"), owners, "host") == "2"
    assert leftovers.container_owner(container(ROOT), owners, "host") is None  # sees all workspaces: PlainBoard's own
    assert leftovers.container_owner(container(f"{ROOT}/p"), owners, "host") is None  # sees more than one agent's
    assert leftovers.container_owner(container(f"{DEV}/a", f"{OPS}/b"), owners, "host") is None  # two agents'
    assert leftovers.container_owner(container("/srv/data"), owners, "host") is None  # nothing of a workspace
    assert leftovers.container_owner(container(f"{DEV}/app", cid="host123"), owners, "host") is None  # the runner's own


def test_what_is_being_stopped_is_not_found_twice(monkeypatch):
    import asyncio

    procs = [proc(10, {"PLAINBOARD_AGENT": "1"})]
    signals = []
    monkeypatch.setattr(leftovers, "_processes", lambda: procs)
    monkeypatch.setattr(leftovers, "_signal", lambda found, sig: signals.append((sorted(p.pid for p in found), sig.name)))
    monkeypatch.setattr(leftovers, "STOP_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(leftovers, "_same", lambda p, boot, ticks: True)
    monkeypatch.setattr(leftovers.shutil, "which", lambda name: None)  # no docker here

    async def go():
        from plainboard_protocol import LeftoverSpec

        spec = LeftoverSpec(owners=OWNERS, stop=True)
        first = await leftovers.leftovers(spec, set())
        second = await leftovers.leftovers(spec, set())  # a second pause right after
        await asyncio.sleep(0.2)
        return first, second

    first, second = asyncio.run(go())
    assert [f.id for f in first.found] == ["10"] and second.found == []
    assert signals == [([10], "SIGTERM"), ([10], "SIGKILL")]
    assert leftovers._stopping == set()
