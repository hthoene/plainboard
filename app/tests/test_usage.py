"""Disk and memory, per volume, project and agent (services/usage.py)."""

from plainboard_protocol import CpuBucket, FolderUsage, MachineUsage, ProcessMemory, VolumeUsage

from plainboard.models import Agent, Project
from plainboard.services import usage

from conftest import agents_by_slug

GB = 2**30


def folder(path: str, size: int, kind: str = "workspace") -> FolderUsage:
    return FolderUsage(path=path, kind=kind, bytes=size)


def test_sizes_go_to_their_agents_projects_and_the_shared_part():
    own = Project(id=1, name="Own", slug="own", workspace_path=None)
    shared = Project(id=2, name="Shared", slug="shared", workspace_path="code/mine")
    agents = [
        Agent(id=10, project_id=1, name="Manager", slug="manager"),
        Agent(id=11, project_id=1, name="Dev", slug="dev"),
        Agent(id=20, project_id=2, name="Solo", slug="solo"),
    ]
    machine = MachineUsage(
        volumes=[
            VolumeUsage(paths=["/workspaces", "/home/agent"], fsid=7, total=400 * GB, used=395 * GB, free=5 * GB),
            VolumeUsage(paths=["/uploads"], fsid=8, total=100 * GB, used=10 * GB, free=90 * GB),
        ],
        folders=[
            folder("/workspaces/own/dev", 3 * GB),
            folder("/workspaces/own/gone", 1 * GB),  # an agent that was deleted
            folder("/workspaces/code/mine", 2 * GB),
            folder("/workspaces/stray/x", 5),
            folder("/home/agent/.cargo", 4 * GB, "shared"),
            folder("/home/agent/.npm", 1 * GB, "shared"),
            folder("/home/agent/.plainboard/trains/p1-r3", 6, "train"),
        ],
        measured_at=1_800_000_000,
        memory_total=125 * GB,
        memory_used=93 * GB,
        memory=[
            ProcessMemory(folder="/workspaces/own/dev", bytes=700, processes=3),
            ProcessMemory(folder="/workspaces/code/mine", bytes=50, processes=1),
            ProcessMemory(folder="/home/agent/.plainboard/trains/p1-r3", bytes=40, processes=2),
            ProcessMemory(folder=None, bytes=300, processes=9),
        ],
    )
    own_machine = usage.Machine(volumes=[usage.Volume("data", 9, 50 * GB, 1 * GB, 49 * GB), usage.Volume("uploads", 8, 100 * GB, 10 * GB, 90 * GB)], data=1234)
    out = usage.summarize([own, shared], agents, {1: 99}, machine, own_machine)

    assert out.runner_ok and out.measured_at is not None
    assert [(v.kinds, v.low) for v in out.volumes] == [(["data"], False), (["uploads"], False), (["workspaces", "home"], True)]
    first, second = out.projects
    assert [(a.name, a.disk, a.memory, a.processes) for a in first.agents] == [("Manager", None, 0, 0), ("Dev", 3 * GB, 700, 3)]
    assert (first.uploads, first.other_disk, first.other_memory) == (99, 1 * GB + 6, 40)
    # a folder all agents of a project share is the project's, not one agent's
    assert [(a.disk, a.memory) for a in second.agents] == [(None, 0)]
    assert (second.uploads, second.other_disk, second.other_memory) == (0, 2 * GB, 50)
    assert [(f.name, f.bytes) for f in out.shared] == [(".cargo", 4 * GB), (".npm", 1 * GB)]
    assert out.other_disk == 5 and out.data == 1234
    assert (out.memory_total, out.memory_used, out.memory_other) == (125 * GB, 93 * GB, 300)


def test_without_the_runner_the_apps_own_part_still_shows():
    out = usage.summarize([], [], {}, None, usage.Machine(volumes=[usage.Volume("data", 1, 100 * GB, 95 * GB, 5 * GB)]))
    assert not out.runner_ok and [(v.kinds, v.low) for v in out.volumes] == [(["data"], True)]
    assert usage.low(1000 * GB, 50 * GB) and not usage.low(1000 * GB, 200 * GB)


def test_the_overview_through_the_api(client, project):
    runner = client.app_ref.state.runner
    dev = next(iter(agents_by_slug(client, project["id"]).values()))
    runner.machine_usage = MachineUsage(folders=[folder(f"/workspaces/{project['slug']}/{dev['slug']}", 4096)], measured_at=1_800_000_000)

    out = client.get("/api/usage").json()
    assert out["runner_ok"] and out["measured_at"].endswith("Z")
    (mine,) = [p for p in out["projects"] if p["id"] == project["id"]]
    assert {a["id"]: a["disk"] for a in mine["agents"]}[dev["id"]] == 4096
    assert out["volumes"] and out["data"] > 0

    assert client.post("/api/usage/measure").status_code == 200 and runner.usage_measured == 1
    runner.reachable = False
    assert client.get("/api/usage").json()["runner_ok"] is False


def test_cpu_per_agent_project_and_the_rest_with_history_and_windows():
    own = Project(id=1, name="Own", slug="own", workspace_path=None)
    agents = [Agent(id=11, project_id=1, name="Dev", slug="dev")]
    start = 1_800_000_000 // 300 * 300
    buckets = [
        CpuBucket(start=start - 2 * 3600, span=300, seconds={"/workspaces/own/dev": 150.0}, load=1.0),
        CpuBucket(start=start - 300, span=300, seconds={"/workspaces/own/dev": 30.0, "/home/agent/.plainboard/trains/p1-r3": 60.0, "": 15.0, "/workspaces/stray/x": 15.0}, load=2.0),
        CpuBucket(start=start, span=150, seconds={"/workspaces/own/dev": 75.0}, load=3.0),
    ]
    machine = MachineUsage(cpu_cores=8, load_average=[1.5, 1.0, 0.5], cpu_now={"/workspaces/own/dev": 0.5, "/home/agent/.plainboard/trains/p1-r3": 1.0, "": 0.25}, cpu_history=buckets)
    out = usage.summarize([own], agents, {}, machine, usage.Machine())
    (project,) = out.projects
    (dev,) = project.agents
    assert out.cpu_cores == 8 and out.cpu_load == [1.5, 1.0, 0.5] and len(dev.cpu_history) == 288
    assert dev.cpu == 0.5 and project.other_cpu == 1.0 and out.cpu_other == 0.25  # the landing check counts for its project
    assert dev.cpu_history[-1] == 0.5 and dev.cpu_history[-2] == 0.1 and dev.cpu_history[-3] == 0
    assert project.other_cpu_history[-2] == 0.2 and out.cpu_other_history[-2] == 0.1
    assert out.cpu_load_history[-1] == 3.0
    # 1 h: (30 + 75) s over 450 s sampled; 6 h adds the slot two hours back
    assert dev.cpu_avg == [round(105 / 450, 3), round(255 / 750, 3), round(255 / 750, 3)]
    assert out.cpu_start is not None
