import asyncio
import os
import subprocess
import time

import pytest

from plainboard_runner import config, usage


@pytest.fixture
def machine(tmp_path, monkeypatch):
    ws, home = tmp_path / "ws", tmp_path / "home"
    (ws / "p" / "a").mkdir(parents=True)
    (ws / "p" / "b").mkdir()
    (ws / "p" / "a" / "big").write_bytes(os.urandom(200_000))
    (home / ".cargo").mkdir(parents=True)
    (home / ".cargo" / "crate").write_bytes(os.urandom(50_000))
    (home / ".plainboard" / "jobs").mkdir(parents=True)
    (home / ".plainboard" / "trains" / "p1-r2").mkdir(parents=True)
    (home / ".plainboard" / "trains" / "p1-r2" / "clone").write_bytes(os.urandom(80_000))
    monkeypatch.setattr(config, "WORKSPACES_ROOT", ws)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(usage, "_folders", [])
    monkeypatch.setattr(usage, "_measured_at", None)
    monkeypatch.setattr(usage, "_loaded", False)
    monkeypatch.setattr(usage, "_task", None)
    return ws, home


def test_measures_agents_shared_home_and_trains_apart(machine):
    ws, home = machine
    asyncio.run(usage.measure())
    found = {f.path: f for f in usage._folders}
    assert {(p, f.kind) for p, f in found.items()} == {
        (str(ws / "p" / "a"), "workspace"),
        (str(ws / "p" / "b"), "workspace"),
        (str(home / ".cargo"), "shared"),
        (str(home / ".plainboard" / "jobs"), "shared"),  # the trains are not in the shared part twice
        (str(home / ".plainboard" / "trains" / "p1-r2"), "train"),
    }
    assert all(f.bytes > 0 for f in found.values())  # not exact: a new file's blocks may show only seconds later (ZFS)

    # kept in the home: after a restart it is known without measuring again
    usage._folders, usage._measured_at, usage._loaded = [], None, False
    usage._load()
    assert {f.path for f in usage._folders} == set(found) and usage._measured_at


def test_measures_again_only_when_due_or_asked(machine, monkeypatch):
    calls = []

    async def measure():
        calls.append(1)

    monkeypatch.setattr(usage, "measure", measure)

    async def scenario():
        usage._loaded, usage._measured_at = True, time.time()
        usage.measure_soon()
        assert usage._task is None
        usage.measure_soon(force=True)
        await usage._task
        usage._measured_at = time.time() - usage.MEASURE_EVERY_SECONDS - 1
        usage.measure_soon()
        await usage._task

    asyncio.run(scenario())
    assert len(calls) == 2


def test_du_measures_each_path_that_exists(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "f").write_bytes(os.urandom(10_000))
    paths = [str(tmp_path / name) for name in ("a", "f", "gone")]
    assert asyncio.run(usage.du(paths)).keys() == set(paths[:2])
    assert asyncio.run(usage.du([])) == {}


def test_folder_of_a_working_directory(machine):
    ws, home = machine
    assert usage.folder_of(str(ws / "p" / "a" / "src" / "deep")) == str(ws / "p" / "a")
    assert usage.folder_of(str(ws / "shared")) == str(ws / "shared")  # a project's own folder (workspace_path)
    assert usage.folder_of(str(home / ".plainboard" / "trains" / "p1-r2" / "app")) == str(home / ".plainboard" / "trains" / "p1-r2")
    assert usage.folder_of(str(ws)) is None
    assert usage.folder_of("/tmp") is None


def test_memory_counts_a_process_tree_for_the_folder_it_works_in(machine):
    ws, _ = machine
    # the inner sleep works in / but is a child of a shell in the agent's folder
    proc = subprocess.Popen(["bash", "-c", "cd / && sleep 30 & sleep 30"], cwd=ws / "p" / "a")
    try:
        time.sleep(0.3)
        total, used, rows = usage.memory()
    finally:
        proc.kill()
        proc.wait()
    assert total > used > 0
    mine = next(r for r in rows if r.folder == str(ws / "p" / "a"))
    assert mine.processes >= 3 and mine.bytes > 0
    assert any(r.folder is None for r in rows)  # this test itself


def test_volumes_group_paths_on_one_file_system(machine):
    ws, home = machine
    vols = usage.volumes()
    assert [str(ws), str(home)] == vols[0].paths[:2]  # both in tmp_path; the uploads folder does not exist
    assert vols[0].total >= vols[0].used > 0 and vols[0].free > 0


def _fake_proc(root, ws, rows):
    """rows: pid -> (parent, cwd, own ticks, children ticks, start time)"""
    import shutil

    shutil.rmtree(root, ignore_errors=True)
    for pid, (ppid, cwd, own, kids, start) in rows.items():
        d = root / str(pid)
        d.mkdir(parents=True)
        fields = ["S", str(ppid)] + ["0"] * 9 + [str(own), "0", str(kids), "0", "0", "0", "0", "0", str(start)]
        (d / "stat").write_text(f"{pid} (my proc) " + " ".join(fields))
        if cwd:
            os.symlink(cwd, d / "cwd")
    (root / "loadavg").write_text("1.50 1.00 0.50 1/100 123\n")


def test_cpu_sampler_counts_deltas_per_folder_and_ended_processes(machine, tmp_path):
    ws, _ = machine
    a, b = str(ws / "p" / "a"), str(ws / "p" / "b")
    proc = tmp_path / "proc"
    sampler = usage.CpuSampler()
    tick = sampler.tick
    _fake_proc(proc, ws, {10: (1, a, 100, 0, 1), 11: (10, "/", 50, 0, 2), 12: (10, "/", 40, 0, 3), 20: (1, b, 10, 0, 4)})
    assert sampler.sample(proc) == {}  # the first sample only reads
    # 10 used 30 ticks; its child 11 (works in /, counts for a) 20; 12 ended after 15 more ticks, wait()ed by 10 (its 40 + 15 = 55 land in cutime);
    # a child 13 that lived only between the samples used 25 (also in 10's cutime); 20 in b used 100
    _fake_proc(proc, ws, {10: (1, a, 130, 80, 1), 11: (10, "/", 70, 0, 2), 20: (1, b, 110, 0, 4)})
    got = sampler.sample(proc)
    assert got[a] == pytest.approx((30 + 20 + 15 + 25) / tick)  # 12's 40 ticks are not counted twice
    assert got[b] == pytest.approx(100 / tick)
    # a process that is new since the last sample counts for what it has used so far
    _fake_proc(proc, ws, {10: (1, a, 130, 80, 1), 11: (10, "/", 70, 0, 2), 20: (1, b, 110, 0, 4), 30: (1, "/", 5, 0, 9)})
    assert sampler.sample(proc) == {None: pytest.approx(5 / tick)}


def test_cpu_history_slots_hold_seconds_load_and_span(machine, monkeypatch):
    monkeypatch.setattr(usage, "_buckets", {})
    usage.record({"/w/p/a": 3.0, None: 1.0}, 30, 2.0, 1_000_000.0)
    usage.record({"/w/p/a": 3.0}, 30, 4.0, 1_000_030.0)
    (slot,) = usage._buckets.values()
    assert slot.seconds == {"/w/p/a": 6.0, "": 1.0} and slot.span == 60 and slot.load == pytest.approx(3.0)
    usage.record({}, 30, 1.0, 1_000_000.0 + 25 * 3600)  # the first slot is older than 24 hours now
    assert len(usage._buckets) == 1


def test_cpu_history_is_kept_in_the_home(machine, monkeypatch):
    monkeypatch.setattr(usage, "_buckets", {})
    usage.record({"/w/p/a": 3.0}, 30, 1.0, time.time())
    usage._save_cpu(force=True)
    usage._buckets.clear()
    usage._load_cpu()
    assert list(usage._buckets.values())[0].seconds == {"/w/p/a": 3.0}


def test_cpu_sampler_sees_a_real_busy_process_in_its_folder(machine):
    ws, _ = machine
    sampler = usage.CpuSampler()
    proc = subprocess.Popen(["bash", "-c", "while :; do :; done"], cwd=ws / "p" / "a")
    try:
        sampler.sample()
        time.sleep(1)
        got = sampler.sample()
    finally:
        proc.kill()
        proc.wait()
    assert got[str(ws / "p" / "a")] > 0.3
