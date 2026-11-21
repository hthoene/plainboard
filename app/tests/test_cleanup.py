"""Disk space under the manager's control (services/storage.py, services/cleanup.py, orchestration/cleanup.py, tools/storage.py)."""

import json
import time
from datetime import datetime, timedelta

import pytest
from plainboard_protocol import CleanedItem, CleanupCandidate, CleanupResult, FolderUsage, MachineUsage, VolumeUsage
from sqlalchemy import select

from plainboard.db import sessionmaker
from plainboard.models import Agent, FlowEvent, InboxItem, Job, Project, Repository, utcnow
from plainboard.services import cleanup, storage

from conftest import agents_by_slug, idle, wait_for

GB = 2**30
SETTINGS = {"storage_notify_percent": 80, "storage_emergency_percent": 97}


@pytest.fixture(autouse=True)
def fresh_trend():
    storage.TREND.samples = []
    yield
    storage.TREND.samples = []


def test_folders_of_busy_agents_are_marked_and_removed_ones_are_orphans():
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
    assert [(f.path, f.orphan, f.busy) for f in out] == [("/workspaces/own/idle", False, False), ("/workspaces/own/busy", False, True), ("/workspaces/own/gone", True, False)]
    assert out[0].idle_since == (turn - datetime(1970, 1, 1)).total_seconds()


def _tick(client, now=None):
    return client.portal.call(client.app_ref.state.cleanup.tick, now)


def _forecast(samples, now):
    trend = storage.Trend()
    for minutes, percent in samples:
        f = trend.add(now + timedelta(minutes=minutes), 1000 * GB, int((100 - percent) * 10 * GB))
    return f


def test_the_manager_hears_it_before_the_target_when_it_grows_fast():
    now = datetime(2026, 11, 20, 1)
    fast = _forecast([(0, 70), (30, 72), (60, 74)], now)  # 4 points an hour: 80 % in 1.5 h
    assert round(fast.rate / GB) == 40 and round(fast.hours_to(80), 1) == 1.5
    assert storage.notice_due(fast, SETTINGS, None, now) == "soon"
    text = storage.forecast_text(fast, SETTINGS)
    assert "growing 40.0 GB/h" in text and "80 % in about 1 h 30 min" in text and "full in about 6 h 30 min" in text
    assert storage.when_text("soon", fast, SETTINGS) == "Clean up within 1 h 30 min, before it crosses the target."

    slow = _forecast([(0, 73), (60, 74)], now)  # 80 % in 6 h: not yet, but it looks every few minutes
    assert storage.notice_due(slow, SETTINGS, None, now) is None
    assert storage.next_check(slow, SETTINGS, 3600) == storage.FAST_SECONDS
    calm = _forecast([(0, 60), (60, 60)], now)
    assert storage.notice_due(calm, SETTINGS, None, now) is None and storage.next_check(calm, SETTINGS, 3600) == 3600
    assert storage.notice_due(_forecast([(0, 81)], now), SETTINGS, None, now) == "over"  # at the target, growth unknown


def test_a_notice_is_repeated_only_when_it_gets_clearly_worse_or_as_a_reminder():
    now = datetime(2026, 11, 20, 1)
    soon = _forecast([(0, 70), (60, 74)], now)
    last = storage.notice_record("soon", soon, now, ["P"], SETTINGS)
    later = now + timedelta(minutes=10)
    assert storage.notice_due(_forecast([(0, 71), (60, 75)], later), SETTINGS, last, later) is None  # the same, told already
    assert storage.notice_due(_forecast([(0, 77), (60, 81)], later), SETTINGS, last, later) == "over"  # crossed it now

    last = storage.notice_record("over", _forecast([(0, 82)], now), now, ["P"], SETTINGS)
    half = now + timedelta(minutes=30)
    assert storage.notice_due(_forecast([(0, 82), (30, 82)], half), SETTINGS, last, half) is None
    assert storage.notice_due(_forecast([(0, 87)], half), SETTINGS, last, half) == "over"  # 5 points worse
    hour = now + timedelta(hours=1, minutes=5)
    assert storage.notice_due(_forecast([(0, 82)], hour), SETTINGS, last, hour) is None  # not growing: reminded only after 6 h
    assert storage.notice_due(_forecast([(0, 83)], hour), SETTINGS, last, hour) == "over"  # still growing: hourly
    assert storage.notice_due(_forecast([(0, 82)], now + timedelta(hours=6)), SETTINGS, last, now + timedelta(hours=6)) == "over"

    cleaned = _forecast([(0, 70)], hour)
    assert storage.closes(cleaned, SETTINGS, last) and not storage.closes(_forecast([(0, 78)], hour), SETTINGS, last)
    closed = {**last, "closed": True}
    assert not storage.closes(cleaned, SETTINGS, closed)
    assert storage.notice_due(_forecast([(0, 81)], half), SETTINGS, closed, half) is None
    assert storage.notice_due(_forecast([(0, 81)], hour), SETTINGS, closed, hour) == "over"  # a new time


def _volume(percent):
    return VolumeUsage(paths=["/workspaces", "/home/agent"], fsid=1, total=100 * GB, used=percent * GB, free=(100 - percent) * GB)


def _told(fake, text):
    return [s.prompt for s in fake.specs if text in s.prompt]


def test_at_the_target_the_manager_is_told_once_and_nothing_is_deleted(client, project):
    pid, slug = project["id"], project["slug"]
    wait_for(lambda: idle(client, pid))
    fake = client.app_ref.state.runner
    fake.machine_usage = MachineUsage(volumes=[_volume(85)], folders=[FolderUsage(path=f"/workspaces/{slug}/manager", kind="workspace", bytes=40 * GB)], measured_at=time.time())
    fake.candidates = [
        CleanupCandidate(path=f"/workspaces/{slug}/manager/repo/target", what="build", bytes=30 * GB, safety="busy"),
        CleanupCandidate(path="/home/agent/fw-jobs", what="home", bytes=26 * GB, safety="unsure", why="fw-jobs/run.log is not in a clone"),
        CleanupCandidate(path="/home/agent/.cache/sccache", what="cache", bytes=21 * GB, safety="safe", tool="sccache"),
    ]
    assert _tick(client) == storage.FAST_SECONDS
    assert not getattr(fake, "cleanups", [])  # below the emergency level nothing goes by itself
    wait_for(lambda: idle(client, pid) and _told(fake, "Disk space: 85 % used"))
    prompt = _told(fake, "Disk space")[0]
    assert "Clean up now." in prompt and "Workspaces: Manager 40.0 GB." in prompt
    assert f"- 30.0 GB /workspaces/{slug}/manager/repo/target: build output of Manager; busy\n" in prompt
    assert "Shared, not yours (47.0 GB; PlainBoard trims the shared tool caches itself" in prompt and "admin" not in prompt
    assert "- 21.0 GB /home/agent/.cache/sccache: cache, shared: sccache's shared cache, PlainBoard trims it itself; safe" in prompt
    assert "26.0 GB /home/agent/fw-jobs: home folder, shared; unsure: fw-jobs/run.log is not in a clone" in prompt
    view = client.get("/api/usage").json()["storage"]
    assert view["notify_percent"] == 80 and view["emergency_percent"] == 97 and round(view["percent"]) == 85
    assert [n["reason"] for n in view["notices"]] == ["over"]

    told = len(_told(fake, "Disk space"))
    _tick(client, utcnow() + timedelta(minutes=5))
    _tick(client, utcnow() + timedelta(minutes=10))
    assert len(client.get("/api/usage").json()["storage"]["notices"]) == 1  # no spam
    assert len(_told(fake, "Disk space")) == told and not getattr(fake, "cleanups", [])


def test_near_full_safe_things_go_by_themselves_and_the_manager_hears_what(client, project):
    pid, slug = project["id"], project["slug"]
    wait_for(lambda: idle(client, pid))
    fake = client.app_ref.state.runner
    fake.machine_usage = MachineUsage(volumes=[_volume(98)])
    fake.cleanup_result = CleanupResult(freed=10 * GB, items=[CleanedItem(path=f"/workspaces/{slug}/gone", what="folder", bytes=10 * GB)])
    _tick(client)
    (spec,) = fake.cleanups
    assert spec.level == "soft" and spec.need == 18 * GB  # down to the target
    wait_for(lambda: idle(client, pid) and _told(fake, "PlainBoard deleted by itself"))
    prompt = _told(fake, "PlainBoard deleted by itself")[0]
    assert f"/workspaces/{slug}/gone (folder) 10.0 GB" in prompt and "Now about 88 % used" in prompt and "Disk space: 88 % used" in prompt
    (done,) = client.get("/api/usage").json()["cleanups"]
    assert done["level"] == "emergency" and done["freed"] == 10 * GB
    _tick(client, utcnow() + timedelta(minutes=5))
    assert len(fake.cleanups) == 1  # not again within the hour

    assert client.put("/api/settings", json={"values": {"cleanup": False}}).status_code < 300
    _tick(client, utcnow() + timedelta(hours=2))
    assert len(fake.cleanups) == 1  # switched off


def test_a_busy_agents_folder_is_left_to_the_manager(client, project):
    pid, slug = project["id"], project["slug"]
    wait_for(lambda: idle(client, pid))
    manager = agents_by_slug(client, pid)["manager"]

    async def running_job() -> None:
        async with sessionmaker()() as s:
            s.add(Job(project_id=pid, agent_id=manager["id"], runner_id="j-busy", command="cargo build"))
            await s.commit()

    client.portal.call(running_job)
    fake = client.app_ref.state.runner
    fake.machine_usage = MachineUsage(volumes=[_volume(98)])
    _tick(client)
    assert f"/workspaces/{slug}/manager" not in {f.path for f in fake.cleanups[-1].folders}  # the emergency cleanup leaves it alone
    assert next(f for f in fake.candidate_specs[-1].folders if f.path == f"/workspaces/{slug}/manager").busy  # the manager sees it, marked busy


def _results(client, agent_id):
    messages = client.get(f"/api/agents/{agent_id}/conversation").json()["messages"]
    return [m["meta"]["result"] for m in messages if m["kind"] == "tool" and "result" in m["meta"]]


def test_the_manager_lists_and_deletes_only_his_projects_data_by_the_safety_rules(client, project):
    pid, slug = project["id"], project["slug"]
    other = client.post("/api/projects", json={"name": "Other Project", "description": "Someone else's"}).json()
    wait_for(lambda: idle(client, pid) and idle(client, other["id"]))
    manager = agents_by_slug(client, pid)["manager"]
    fake = client.app_ref.state.runner
    fake.machine_usage = MachineUsage(volumes=[_volume(85)])
    safe, busy, kept = f"/workspaces/{slug}/manager/repo/target", f"/workspaces/{slug}/manager/web/node_modules", f"/workspaces/{slug}/gone"
    theirs, shared = f"/workspaces/{other['slug']}/manager/repo/target", "/home/agent/scratch"
    fake.candidates = [
        CleanupCandidate(path=safe, what="build", bytes=5 * GB, safety="safe"),
        CleanupCandidate(path=busy, what="build", bytes=3 * GB, safety="busy"),
        CleanupCandidate(path=kept, what="folder", bytes=2 * GB, safety="keep", why="repo has unpushed commits"),
        CleanupCandidate(path=theirs, what="build", bytes=9 * GB, safety="safe"),
        CleanupCandidate(path=shared, what="home", bytes=4 * GB, safety="safe"),
    ]
    calls = [
        "TOOL list_cleanup {}",
        "TOOL delete_cleanup " + json.dumps({"paths": [safe, busy, kept, theirs, shared, "/etc"]}),
        "TOOL delete_cleanup " + json.dumps({"paths": [busy, kept, theirs, shared], "force": True}),
    ]
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "\n".join(calls)}).status_code == 202
    wait_for(lambda: idle(client, pid) and len(_results(client, manager["id"])) >= 3)
    listed, first, forced = _results(client, manager["id"])[-3:]
    assert "Safe (made again when needed):\n- 5.0 GB " + safe in listed and "Kept (unpushed work or tools; delete_cleanup won't):" in listed
    assert theirs not in listed  # another project's data is not even shown
    assert "Shared, not yours (4.0 GB;" in listed and "- 4.0 GB /home/agent/scratch: home folder, shared; safe" in listed
    assert first.startswith("Deleted 5.0 GB: " + safe) and f"{busy}: busy" in first and f"{kept}: keep" in first
    refused = f"Refused, not your project's (only its own data can be deleted): {theirs}, {shared}, /etc."
    assert refused in first
    assert forced.startswith("Deleted 3.0 GB: " + busy) and f"{kept}: keep" in forced and f"{theirs}, {shared}." in forced  # force changes nothing about whose it is
    assert [d.paths for d in fake.deletes] == [[safe, busy, kept], [busy, kept]]
    assert any(c.path == theirs for c in fake.candidates)

    async def events():
        async with sessionmaker()() as s:
            return list(await s.scalars(select(FlowEvent).where(FlowEvent.kind == "storage.cleaned")))

    assert [e.data["level"] for e in client.portal.call(events)] == ["manager", "manager"]
    assert [c["by"] for c in client.get("/api/usage").json()["cleanups"]] == ["Manager", "Manager"]


def test_a_home_folder_is_a_projects_by_its_name_commands_scripts_or_clones_and_only_one_projects():
    mine, paw, pawtools = Project(id=1, name="Mine", slug="mine"), Project(id=2, name="PaperPaw", slug="paperpaw"), Project(id=3, name="Paw Tools", slug="pawtools")
    mine.setup_script = "mkdir -p $HOME/mine-venvs\n"
    repos = [
        Repository(project_id=2, name="paperpaw", url="https://github.com/acme/paperpaw.git", quick_check_command="CARGO_TARGET_DIR=~/.cache/ck-target cargo check", check_command=""),
        Repository(project_id=1, name="mine", url="https://github.com/acme/mine", check_command="", quick_check_command=""),
        Repository(project_id=3, name="paperpaw-tools", url="https://github.com/acme/paperpaw-tools", check_command="", quick_check_command=""),
    ]
    keys = {storage._repo_key(r.url): r.project_id for r in repos}
    o = storage.Owners([mine, paw, pawtools], repos, keys, "/workspaces")

    def owner(path, what="home", **kw):
        return storage.owner(CleanupCandidate(path=path, what=what, safety="safe", **kw), o)

    assert owner("/home/agent/.cache/paperpaw-quick-check", "cache") == (2, "its name")
    assert owner("/home/agent/paperpaw_scratch") == (2, "its name")
    assert owner("/home/agent/paperpawx")[0] is None  # not as a whole word
    assert owner("/home/agent/.cache/ck-target", "cache") == (2, "used by the quick check of paperpaw")
    assert owner("/home/agent/.cache/ck-target-old", "cache")[0] is None
    assert owner("/home/agent/mine-venvs") == (1, "its name, used by the setup script")
    assert owner("/home/agent/scripts-out", mentioned=["/workspaces/mine/dev"]) == (1, "named in files in your workspaces")
    assert owner("/home/agent/fw-jobs", remotes=["git@github.com:acme/paperpaw.git"]) == (2, "its clones are of your repositories")
    # ambiguous: shared
    assert owner("/home/agent/.cache/paperpaw-tools-build", "cache") == (None, "unclear whose: it matches PaperPaw and Paw Tools")
    assert owner("/home/agent/dt-jobs", remotes=["git@github.com:acme/paperpaw.git", "https://github.com/acme/mine.git"])[0] is None
    assert owner("/home/agent/tgt", remotes=["https://github.com/acme/paperpaw", "https://github.com/rust-lang/cargo"]) == (None, "unclear whose: it holds clones of other repositories too")
    assert owner("/home/agent/.cache/sccache", "cache", tool="sccache") == (None, "sccache's shared cache, PlainBoard trims it itself")
    assert owner("/home/agent/stuff") == (None, "")


def test_the_manager_deletes_a_cache_of_his_project_and_another_manager_cannot(client, project):
    pid, slug = project["id"], project["slug"]
    other = client.post("/api/projects", json={"name": "Other Project", "description": "Someone else's"}).json()
    wait_for(lambda: idle(client, pid) and idle(client, other["id"]))
    fake = client.app_ref.state.runner
    fake.machine_usage = MachineUsage(volumes=[_volume(85)])
    cache = f"/home/agent/.cache/{slug}-quick-check"
    fake.candidates = [CleanupCandidate(path=cache, what="cache", bytes=37 * GB, safety="safe", entries={"set1": 30 * GB, "set2": 7 * GB}, modified=time.time() - 7200)]
    theirs = agents_by_slug(client, other["id"])["manager"]
    call = "TOOL list_cleanup {}\nTOOL delete_cleanup " + json.dumps({"paths": [cache]})
    assert client.post(f"/api/agents/{theirs['id']}/messages", json={"text": call}).status_code == 202
    wait_for(lambda: idle(client, other["id"]) and len(_results(client, theirs["id"])) >= 2)
    listed, refused = _results(client, theirs["id"])[-2:]
    assert f"{cache}: cache, shared; safe" not in listed and cache not in listed  # another project's: not even shown
    assert refused == f"Refused, not your project's (only its own data can be deleted): {cache}."

    manager = agents_by_slug(client, pid)["manager"]
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": call}).status_code == 202
    wait_for(lambda: idle(client, pid) and len(_results(client, manager["id"])) >= 2)
    listed, deleted = _results(client, manager["id"])[-2:]
    assert f"- 37.0 GB {cache}: cache, yours: its name; safe (last changed 2 h ago; holds set1 30.0 GB, set2 7.0 GB)" in listed
    assert deleted.startswith(f"Deleted 37.0 GB: {cache}")


def _inbox(client, agent_id):
    async def read():
        async with sessionmaker()() as s:
            return [i.text for i in await s.scalars(select(InboxItem).where(InboxItem.agent_id == agent_id))]

    return client.portal.call(read)


def test_above_the_target_shared_caches_are_trimmed_and_their_users_hear_it(client, project):
    pid, slug = project["id"], project["slug"]
    other = client.post("/api/projects", json={"name": "Other Project", "description": "Someone else's"}).json()
    wait_for(lambda: idle(client, pid) and idle(client, other["id"]))
    manager, theirs = agents_by_slug(client, pid)["manager"], agents_by_slug(client, other["id"])["manager"]
    fake = client.app_ref.state.runner
    sccache = "/home/agent/.cache/sccache"
    fake.trim_result = CleanupResult(freed=8 * GB, items=[CleanedItem(path=sccache, what="cache", bytes=8 * GB, users=[f"/workspaces/{slug}/manager"])])
    fake.machine_usage = MachineUsage(volumes=[_volume(78)])
    _tick(client)
    assert not getattr(fake, "trims", [])  # below the target nothing is trimmed

    fake.machine_usage = MachineUsage(volumes=[_volume(84)])
    _tick(client, utcnow() + timedelta(minutes=5))
    (spec,) = fake.trims
    assert spec.need == 7 * GB and spec.large == storage.TRIM_LARGE and spec.keep == storage.TRIM_KEEP  # to a little below the target
    (told,) = [t for t in _inbox(client, manager["id"]) if "trimmed shared caches" in t]
    assert f"{sccache} by 8.0 GB" in told and "84 % used" in told
    assert not [t for t in _inbox(client, theirs["id"]) if "trimmed" in t]  # its project does not use it
    assert client.get("/api/usage").json()["cleanups"][0]["level"] == "trim"
    _tick(client, utcnow() + timedelta(minutes=10))
    assert len(fake.trims) == 1  # not again soon

    assert client.put("/api/settings", json={"values": {"cleanup": False}}).status_code < 300
    _tick(client, utcnow() + timedelta(hours=4))
    assert len(fake.trims) == 1  # switched off
