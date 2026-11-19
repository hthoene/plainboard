"""Automatic cleanup when disk or memory run short (services/cleanup.py, orchestration/cleanup.py)."""

from datetime import datetime, timedelta

from plainboard_protocol import CleanedItem, CleanupResult, FolderUsage, MachineUsage, VolumeUsage

from plainboard.db import sessionmaker
from plainboard.models import Agent, Job, Project, utcnow
from plainboard.services import cleanup

from conftest import agents_by_slug, idle, wait_for

GB = 2**30
SETTINGS = {"cleanup_soft_free_percent": 15, "cleanup_hard_free_percent": 5, "cleanup_memory_free_percent": 10}


def test_only_folders_of_idle_agents_and_of_removed_ones_are_offered():
    own = Project(id=1, name="Own", slug="own", workspace_path=None)
    shared = Project(id=2, name="Shared", slug="shared", workspace_path="code/mine")
    agents = [
        Agent(id=10, project_id=1, name="Idle", slug="idle", created_at=datetime(2026, 1, 1)),
        Agent(id=11, project_id=1, name="Busy", slug="busy", created_at=datetime(2026, 1, 1)),
        Agent(id=20, project_id=2, name="Solo", slug="solo", created_at=datetime(2026, 1, 1)),
    ]
    measured = [
        FolderUsage(path=p, kind="workspace", bytes=1)
        for p in ("/workspaces/own/idle", "/workspaces/own/busy", "/workspaces/own/gone", "/workspaces/code/mine", "/workspaces/stray/x")
    ]
    turn = datetime(2026, 11, 1, 12)
    out = cleanup.folders([own, shared], agents, busy={11}, turns={10: turn}, measured=measured, root="/workspaces/")
    assert [(f.path, f.orphan) for f in out] == [("/workspaces/own/idle", False), ("/workspaces/own/gone", True)]
    assert out[0].idle_since == (turn - datetime(1970, 1, 1)).total_seconds()


def test_thresholds_aim_above_the_soft_one_and_do_not_repeat_for_nothing():
    assert cleanup.level(100 * GB, 20 * GB, SETTINGS) is None
    assert cleanup.level(100 * GB, 14 * GB, SETTINGS) == "soft"
    assert cleanup.level(100 * GB, 4 * GB, SETTINGS) == "hard"
    assert cleanup.need(100 * GB, 14 * GB, SETTINGS) == 6 * GB  # up to 20 % free
    now = datetime(2026, 11, 19, 12)
    poor = {"at": (now - timedelta(hours=1)).isoformat(), "level": "soft", "freed": 1, "need": 10}
    assert not cleanup.due(poor, "soft", now)
    assert cleanup.due(poor, "hard", now)  # worse: at once
    assert cleanup.due(poor, "soft", now + timedelta(hours=6))
    assert cleanup.due({**poor, "freed": 10}, "soft", now)


def _tick(client, now=None):
    return client.portal.call(client.app_ref.state.cleanup.tick, now)


def test_a_short_volume_is_cleaned_and_the_manager_hears_only_when_still_short(client, project):
    pid, slug = project["id"], project["slug"]
    agents = agents_by_slug(client, pid)
    wait_for(lambda: idle(client, pid))
    fake = client.app_ref.state.runner
    volume = VolumeUsage(paths=["/workspaces", "/home/agent"], fsid=1, total=100 * GB, used=88 * GB, free=12 * GB)
    fake.machine_usage = MachineUsage(volumes=[volume], folders=[FolderUsage(path=f"/workspaces/{slug}/gone", kind="workspace", bytes=3 * GB)])
    fake.cleanup_result = CleanupResult(freed=9 * GB, items=[CleanedItem(path=f"/workspaces/{slug}/manager/repo/target", what="build", bytes=9 * GB)])

    _tick(client)
    (spec,) = fake.cleanups
    assert spec.level == "soft" and spec.need == 8 * GB
    assert {f.path: f.orphan for f in spec.folders} == {f"/workspaces/{slug}/{a}": False for a in agents} | {f"/workspaces/{slug}/gone": True}
    last = client.get("/api/usage").json()["cleanup"]
    assert last["level"] == "soft" and last["freed"] == 9 * GB and not last["told"]
    fake.machine_usage = MachineUsage(volumes=[volume.model_copy(update={"free": 21 * GB, "used": 79 * GB})])
    _tick(client)
    assert len(fake.cleanups) == 1  # room enough: only looked at

    # Below the hard threshold, and what goes is not enough: the manager hears it, once a day.
    fake.machine_usage = MachineUsage(volumes=[volume.model_copy(update={"free": 3 * GB, "used": 97 * GB})])
    fake.cleanup_result = CleanupResult(freed=1 * GB)
    _tick(client)
    assert fake.cleanups[-1].level == "hard" and client.get("/api/usage").json()["cleanup"]["told"]
    wait_for(lambda: idle(client, pid) and any("Disk space is still short" in s.prompt for s in fake.specs))
    _tick(client, utcnow() + timedelta(hours=7))
    assert len(fake.cleanups) == 3 and not client.get("/api/usage").json()["cleanup"]["told"]


def test_a_busy_agents_folder_is_not_offered(client, project):
    pid, slug = project["id"], project["slug"]
    wait_for(lambda: idle(client, pid))
    manager = agents_by_slug(client, pid)["manager"]

    async def running_job() -> None:
        async with sessionmaker()() as s:
            s.add(Job(project_id=pid, agent_id=manager["id"], runner_id="j-busy", command="cargo build"))
            await s.commit()

    client.portal.call(running_job)
    fake = client.app_ref.state.runner
    fake.machine_usage = MachineUsage(volumes=[VolumeUsage(paths=["/workspaces"], fsid=1, total=100 * GB, used=90 * GB, free=10 * GB)])
    _tick(client)
    assert f"/workspaces/{slug}/manager" not in {f.path for f in fake.cleanups[-1].folders}
