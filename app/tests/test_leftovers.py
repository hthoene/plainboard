"""#187: what agents leave running outside their jobs (detached processes, containers) stops with a pause or stop."""

import json

from conftest import idle, runs_of, wait_for
from plainboard_protocol import Leftover
from test_flow import flow_events, make_member, manager_of, runner, tool_results
from test_limit_pause import record, tick, two_subscriptions
from test_orchestration import chat


def stray(agent: dict, pid: int, label: str = "node server.js") -> Leftover:
    return Leftover(agent=str(agent["id"]), kind="process", id=str(pid), label=label)


def test_a_paused_project_stops_what_its_agents_left_running_outside_their_jobs(client, project):
    pid = project["id"]
    dev = make_member(client, pid, "Dev")
    fake = runner(client)
    fake.strays = [stray(dev, 4242), Leftover(agent=str(dev["id"]), kind="container", id="abc123", label="db (postgres:16)")]
    other = make_member(client, client.post("/api/projects", json={"name": "Other"}).json()["id"], "Elsewhere")
    fake.strays.append(stray(other, 777))

    assert client.post(f"/api/projects/{pid}/pause").json()["paused_at"]
    wait_for(lambda: len(fake.strays) == 1)
    assert fake.strays[0].id == "777"  # another project's stays
    assert all(o.workspace.endswith("/dev") for spec in fake.leftover_specs for o in spec.owners if o.agent == str(dev["id"]))

    assert client.delete(f"/api/projects/{pid}/pause").json()["paused_at"] is None
    chat(client, dev["id"], "SAY back")
    wait_for(lambda: idle(client, pid))
    prompt = fake.specs[-1].prompt
    assert "process 4242 (node server.js)" in prompt and "container abc123 (db (postgres:16))" in prompt
    assert fake.specs[-1].env == {"PLAINBOARD_AGENT": str(dev["id"])}  # its turns carry the mark that finds them


def test_the_limit_pause_stops_leftovers_and_the_manager_reads_how_many(client, project):
    pid = project["id"]
    two_subscriptions(client)
    dev = make_member(client, pid, "Dev")
    manager = manager_of(client, pid)
    fake = runner(client)
    fake.strays = [stray(dev, 4242), stray(dev, 4243, "sleep 1000")]
    record(client, Claude=0.99, Second=0.96)
    assert tick(client) == (1, 0)
    assert fake.strays == []
    chat(client, manager["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    assert "It stopped what members still had running in the background: Dev 2." in fake.specs[-1].prompt


def test_pausing_or_stopping_one_agent_stops_only_its_leftovers(client, project):
    pid = project["id"]
    dev = make_member(client, pid, "Dev")
    ops = make_member(client, pid, "Ops")
    manager = manager_of(client, pid)
    fake = runner(client)
    fake.strays = [stray(dev, 10), stray(ops, 20)]

    # The manager sees them with list_jobs; nothing is stopped by looking.
    chat(client, manager["id"], "TOOL list_jobs")
    wait_for(lambda: idle(client, pid))
    listed = tool_results(client, manager["id"])[-1]
    assert "Dev: process 10 (node server.js)" in listed and "Ops: process 20" in listed and len(fake.strays) == 2

    chat(client, manager["id"], "TOOL stop_agent " + json.dumps({"agent": "dev", "pause": True}))
    wait_for(lambda: idle(client, pid) and len(fake.strays) == 1)
    assert fake.strays[0].id == "20"
    assert "agent.leftovers_stopped" in [e["kind"] for e in flow_events(client, pid)]

    # A turn told to wrap up: what it left stops when it ends.
    fake.strays.append(stray(ops, 21))
    chat(client, ops["id"], "HOLD\nSAY saved")
    wait_for(lambda: any(r["status"] == "running" and r["agent_id"] == ops["id"] for r in runs_of(client, pid)))
    assert client.patch(f"/api/agents/{ops['id']}", json={"enabled": False}).status_code == 200
    assert len(fake.strays) == 2  # not while its turn runs
    fake.released = True
    wait_for(lambda: idle(client, pid) and fake.strays == [])
