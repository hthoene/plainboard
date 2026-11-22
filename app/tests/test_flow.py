"""How work flows: one piece of work per assignment, with its own context, one report and its time
measured; pull requests watched and merged without any agent's turn."""

import asyncio
import gzip
import json
from datetime import timedelta

from conftest import FakeRunner, agents_by_slug, idle, runs_of, wait_for
from plainboard_protocol import JobState, PullRequestCheck, PullRequestState


def chat(client, agent_id: int, text: str) -> None:
    assert client.post(f"/api/agents/{agent_id}/messages", json={"text": text}).status_code == 202


def make_member(client, pid: int, name: str = "Dev") -> dict:
    resp = client.post(f"/api/projects/{pid}/agents", json={"name": name, "role": "Developer", "description": "Backend API"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def runner(client) -> FakeRunner:
    return client.app_ref.state.runner


def manager_of(client, pid: int) -> dict:
    return agents_by_slug(client, pid)["manager"]


def delegate(agent: str, title: str, instructions: str, **extra) -> str:
    return "TOOL delegate " + json.dumps({"agent": agent, "title": title, "instructions": instructions, **extra})


def assignments(client, pid: int) -> list[dict]:
    return sorted(client.get(f"/api/projects/{pid}/assignments").json(), key=lambda a: a["id"])


def tool_results(client, agent_id: int) -> list[str]:
    messages = client.get(f"/api/agents/{agent_id}/conversation").json()["messages"]
    return [m["meta"]["result"] for m in messages if m["kind"] == "tool" and "result" in m["meta"]]


def job_tick(client) -> None:
    client.portal.call(client.app_ref.state.job_watcher.tick)


def flow_events(client, pid: int) -> list[dict]:
    """What PlainBoard wrote down about its own decisions, as the full log gives it."""
    lines = gzip.decompress(client.get(f"/api/projects/{pid}/work-log?days=1").content).splitlines()
    return [row for row in map(json.loads, lines) if row["type"] == "flow_event"]


# --- one assignment, one report -------------------------------------------------------------


def test_reports_stay_with_their_assignment_and_comments_are_only_what_someone_meant_to_say(client, project):
    pid = project["id"]
    make_member(client, pid)
    manager = manager_of(client, pid)
    for title in ("Build it", "Another"):
        client.post(f"/api/projects/{pid}/tasks", json={"title": title, "status": "in_progress"})
    # The member says it twice, as a comment on its own ticket and as its report, and notes something on another ticket.
    work = 'TOOL add_comment {"number": 1, "body": "Done, see feat-a"}\nTOOL add_comment {"number": 2, "body": "feat-a changes the API this needs"}\nSAY Done on feat-a'
    chat(client, manager["id"], delegate("dev", "Do it", work, ticket_number=1))
    wait_for(lambda: idle(client, pid) and assignments(client, pid)[0]["status"] == "done")
    first, second = (client.get(f"/api/projects/{pid}/tasks").json()[i]["id"] for i in (0, 1))
    assert client.get(f"/api/tasks/{first}").json()["comments"] == []
    assert [c["body"] for c in client.get(f"/api/tasks/{second}").json()["comments"]] == ["feat-a changes the API this needs"]
    refused = tool_results(client, agents_by_slug(client, pid)["dev"]["id"])[0]
    assert refused.startswith("Not added: ticket #1 shows the report of your assignment")

    # Whoever reads the ticket gets the report from the assignment, and only the latest ones of a long ticket.
    chat(client, manager["id"], 'TOOL add_comment {"number": 1, "body": "The user must restart the device once"}\nTOOL get_task {"number": 1}')
    wait_for(lambda: idle(client, pid))
    text = tool_results(client, manager["id"])[-1]
    assert "Recent comments:\n- Manager: The user must restart the device once" in text
    assert "Assignments:\n- #1 [done] Dev: Do it → " in text and "Done on feat-a" in text
    for i in range(2, 9):
        chat(client, manager["id"], delegate("dev", f"Part {i}", f"SAY part {i} done", ticket_number=1))
        wait_for(lambda: idle(client, pid) and all(a["status"] == "done" for a in assignments(client, pid)))
    chat(client, manager["id"], 'TOOL get_task {"number": 1}')
    wait_for(lambda: idle(client, pid))
    text = tool_results(client, manager["id"])[-1]
    assert "Assignments (last 5 of 8; older: list_assignments):" in text and "Part 4" in text and "Part 3" not in text
    assert client.get(f"/api/tasks/{first}").json()["comment_count"] == 1


def test_the_manager_is_asked_to_split_a_ticket_that_keeps_getting_assignments(client, project):
    pid = project["id"]
    make_member(client, pid)
    manager = manager_of(client, pid)
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Everything", "status": "in_progress"})
    for i in range(1, 11):
        chat(client, manager["id"], delegate("dev", f"Part {i}", f"SAY part {i} done", ticket_number=1))
        wait_for(lambda: idle(client, pid) and all(a["status"] == "done" for a in assignments(client, pid)))
    results = [r for r in tool_results(client, manager["id"]) if r.startswith("Assignment #")]
    assert len(results) == 10 and not any("split" in r for r in results[:9])  # a ticket of a normal size hears nothing
    assert "Ticket #1 has 10 assignments now" in results[9] and "split it into smaller tickets (create_tasks)" in results[9]


def test_an_assignment_stays_open_while_its_job_runs_and_reports_once(client, project):
    pid = project["id"]
    make_member(client, pid)
    manager = manager_of(client, pid)
    chat(client, manager["id"], delegate("dev", "Fix it", "TOOL start_job " + json.dumps({"command": "make test"})))
    wait_for(lambda: idle(client, pid) and runner(client).job_specs)
    (assignment,) = assignments(client, pid)
    assert assignment["status"] == "running" and assignment["result"] is None
    assert [r["trigger"] for r in runs_of(client, pid)] == ["assignment", "chat"]  # no report yet: the manager is not woken
    assert "Your assignment stays open until it ends" in runs_of(client, pid)[0]["result_text"]

    spec = runner(client).job_specs[-1]
    runner(client).jobs[spec.id] = JobState(running=False, exit_code=0, log_path="/jobs/x.log", tail="SAY All green")
    job_tick(client)
    wait_for(lambda: idle(client, pid) and assignments(client, pid)[0]["status"] == "done")
    assert assignments(client, pid)[0]["result"] == "All green"
    wait_for(lambda: any('Assignment #1 "Fix it"' in s.prompt and s.toolset == "board" for s in runner(client).specs))
    wait_for(lambda: idle(client, pid))
    report, turn, _first, _chat = runs_of(client, pid)
    assert (turn["trigger"], turn["assignment_id"]) == ("wake", assignment["id"])  # the job's result continues the assignment
    going_on = next(s.prompt for s in runner(client).specs if "Go on with assignment #1" in s.prompt)
    assert "exit code 0 after" in going_on and "So far it took" in going_on and "waiting for 1 job" in going_on
    assert report["agent_id"] == manager["id"] and report["trigger"] == "wake"
    update = runner(client).specs[-1].prompt
    assert 'Assignment #1 "Fix it" (Dev) done after' in update and "of work in 2 turns" in update and "All green" in update


def test_a_job_whose_waiting_tool_call_outlived_its_turn_still_continues_the_assignment(client, project):
    pid = project["id"]
    make_member(client, pid)
    chat(client, manager_of(client, pid)["id"], delegate("dev", "Fix it", "TOOL start_job " + json.dumps({"command": "make test"})))
    wait_for(lambda: idle(client, pid) and runner(client).job_specs)
    assert assignments(client, pid)[0]["status"] == "running"
    turn = runs_of(client, pid)[0]["id"]  # the assignment's turn, over
    # A tool call of that turn still waits for the job: the agent's side gave up on it (a proxy's timeout, 524).
    late = client.portal.start_task_soon(client.app_ref.state.runs.wait_job, 1, 600, turn)
    wait_for(lambda: bool(client.app_ref.state.runs._job_waiters))
    spec = runner(client).job_specs[-1]
    runner(client).jobs[spec.id] = JobState(running=False, exit_code=0, log_path="/jobs/x.log", tail="SAY All green")
    job_tick(client)
    wait_for(lambda: idle(client, pid) and assignments(client, pid)[0]["status"] == "done")
    assert "exit code 0" in late.result(5) and assignments(client, pid)[0]["result"] == "All green"


def test_a_server_does_not_keep_an_assignment_open(client, project):
    pid = project["id"]
    make_member(client, pid)
    chat(client, manager_of(client, pid)["id"], delegate("dev", "Preview", "TOOL start_job " + json.dumps({"command": "npm run dev", "server": True})))
    wait_for(lambda: idle(client, pid) and assignments(client, pid) and assignments(client, pid)[0]["status"] == "done")
    assert "keeps running until someone stops it" in assignments(client, pid)[0]["result"]


def test_an_assignment_waiting_for_its_job_can_be_cancelled(client, project):
    pid = project["id"]
    make_member(client, pid)
    manager = manager_of(client, pid)
    chat(client, manager["id"], delegate("dev", "Fix it", "TOOL start_job " + json.dumps({"command": "make test"})))
    wait_for(lambda: idle(client, pid) and runner(client).job_specs)
    chat(client, manager["id"], 'TOOL cancel_assignment {"assignment_id": 1}')
    wait_for(lambda: idle(client, pid) and assignments(client, pid)[0]["status"] == "cancelled")
    assert agents_by_slug(client, pid)["dev"]["running_jobs"] == 0  # its job is stopped with it


# --- context per piece of work --------------------------------------------------------------


def test_a_member_starts_another_ticket_with_a_fresh_context(client, project):
    pid = project["id"]
    dev = make_member(client, pid)
    manager = manager_of(client, pid)
    for title in ("One", "Two"):
        client.post(f"/api/projects/{pid}/tasks", json={"title": title, "status": "todo"})
    for number, text in ((1, "first"), (1, "again"), (2, "other")):
        chat(client, manager["id"], delegate("dev", f"Work {text}", f"SAY {text}", ticket_number=number))
        wait_for(lambda: idle(client, pid) and len([a for a in assignments(client, pid) if a["status"] == "done"]) == len(assignments(client, pid)))
        wait_for(lambda: idle(client, pid))
    first, again, other = [s for s in runner(client).specs if s.toolset == "full"]
    assert first.resume_session_id is None and "This is a new context" in first.prompt
    assert again.resume_session_id == f"session-{first.run_id}" and "new context" not in again.prompt  # same ticket: same context
    assert other.resume_session_id is None and "This is a new context" in other.prompt
    assert "#2 Work again; #1 Work first" in other.prompt  # where it left off, without the conversation
    assert "Work first" not in other.prompt.split("#2 Work again; #1 Work first")[1]
    marks = [m for m in client.get(f"/api/agents/{dev['id']}/conversation").json()["messages"] if m["kind"] == "summary"]
    assert len(marks) == 1 and marks[0]["meta"]["fresh"] is True and not runner(client).completions  # nothing is summarized for it


# --- the database remembers what is ordered, not the manager ---------------------------------


def test_delegate_refuses_a_second_assignment_for_a_ticket_with_open_work(client, project):
    pid = project["id"]
    make_member(client, pid)
    make_member(client, pid, "QA")
    manager = manager_of(client, pid)
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Build it", "status": "todo"})
    chat(client, manager["id"], delegate("dev", "Work", "HOLD", ticket_number=1))
    wait_for(lambda: any(r["trigger"] == "assignment" and r["status"] == "running" for r in runs_of(client, pid)))
    chat(client, manager["id"], delegate("qa", "Work", "HOLD", ticket_number=1) + chr(10) + delegate("qa", "Work", "HOLD", ticket_number=1, parallel=True))
    wait_for(lambda: len(tool_results(client, manager["id"])) == 3)
    results = tool_results(client, manager["id"])
    assert "already has open work" in results[1] and '#1 "Work" with Dev (running)' in results[1]
    assert "Assignment #2 queued for qa" in results[2]
    # Every manager turn lists what is in flight.
    prompt = [s.prompt for s in runner(client).specs if s.toolset == "board"][-1]
    assert "In flight (already ordered" in prompt and 'Assignment #1 for #1, Dev: "Work" · working' in prompt
    runner(client).released = True
    wait_for(lambda: idle(client, pid))


def test_started_work_goes_before_new_work_and_the_manager_needs_no_slot(client, project):
    pid = project["id"]
    client.put("/api/settings", json={"values": {"max_parallel_runs": 1}})
    dev, qa, ops = make_member(client, pid), make_member(client, pid, "QA"), make_member(client, pid, "Ops")
    manager = manager_of(client, pid)
    chat(client, ops["id"], "HOLD")
    wait_for(lambda: any(r["status"] == "running" for r in runs_of(client, pid)))
    # The only slot is taken, and the manager still gets its turn.
    chat(client, manager["id"], delegate("dev", "New work", "SAY new") + chr(10) + 'TOOL message_agent {"agent": "qa", "message": "SAY note"}')
    wait_for(lambda: any(r["agent_id"] == manager["id"] and r["status"] == "succeeded" for r in runs_of(client, pid)))
    assert {r["agent_id"] for r in runs_of(client, pid) if r["status"] == "queued"} == {dev["id"], qa["id"]}
    runner(client).released = True
    wait_for(lambda: idle(client, pid))
    run_of = {r["agent_id"]: str(r["id"]) for r in runs_of(client, pid) if r["agent_id"] in (dev["id"], qa["id"])}
    order = [s.run_id for s in runner(client).specs]
    assert order.index(run_of[qa["id"]]) < order.index(run_of[dev["id"]])  # queued later, but it belongs to work that has begun


# --- landing: finished branches go to the base branch together, with one check run -----------


def land_tick(client) -> None:
    client.portal.call(client.app_ref.state.landing.tick)


def team_with_branches(client, pid: int, branches: list[str], *, url: str = "https://gitlab.com/acme/api.git", check_command: str = "./ci.sh", land_first: bool = False) -> FakeRunner:
    """One member per branch; each has pushed its branch and hands it over."""
    assert client.post(f"/api/projects/{pid}/repositories", json={"url": url, "check_command": check_command, "land_first": land_first}).status_code == 201
    fake = runner(client)
    for index, branch in enumerate(branches):
        member = make_member(client, pid, f"Dev{index + 1}")
        fake.branches[branch] = f"sha-{branch}"
        chat(client, member["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": branch}))
    wait_for(lambda: idle(client, pid) and len(runs_of(client, pid)) == len(branches))
    return fake


def finish_check(client, fake: FakeRunner, culprit: str | None = None, every: bool = False) -> None:
    """End the running check of the last train: it fails if the culprit is on board (or always)."""
    failed = every or culprit in fake.train_results[-1].merged
    fake.jobs[fake.check_specs[-1].id] = JobState(running=False, exit_code=1 if failed else 0, log_path="/jobs/check.log", tail="FAILED test_login" if failed else "all passed")
    land_tick(client)
    wait_for(lambda: idle(client, pid_of(client)))


def pid_of(client) -> int:
    return client.get("/api/projects").json()[0]["id"]


def last_update(fake: FakeRunner) -> str:
    """The manager's latest turn ("" before its first one)."""
    return next((s.prompt for s in reversed(fake.specs) if s.toolset == "board"), "")


def test_finished_branches_are_checked_together_and_one_check_run_lands_them_all(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b", "feat-c"])
    assert "is with PlainBoard now" in runs_of(client, pid)[0]["result_text"]  # the member is done without waiting for any check
    land_tick(client)
    (train,) = fake.train_specs
    assert train.members == ["feat-a", "feat-b", "feat-c"] and train.github is None and train.remote.url == "https://gitlab.com/acme/api.git"
    assert [c.command for c in fake.check_specs] == ["./ci.sh"]  # one check run for three branches, on any git host

    # What gets ready while the check runs waits for the next train: check runs follow the clock, not the changes.
    late = make_member(client, pid, "Late")
    fake.branches["feat-d"] = "sha-feat-d"
    chat(client, late["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-d"}))
    wait_for(lambda: idle(client, pid))
    before = len(fake.specs)
    land_tick(client)
    assert len(fake.train_specs) == 1 and len(fake.specs) == before  # nobody gets a turn while it runs
    chat(client, manager_of(client, pid)["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    assert "Branch feat-a, Dev1: being checked with 2 other branches" in last_update(fake) and "Branch feat-d, Late: goes on the next train" in last_update(fake)

    finish_check(client, fake)
    (landed,) = fake.land_specs
    assert landed.base == "main" and landed.head_sha == "train-1" and landed.delete_branches == {"feat-a": "sha-feat-a", "feat-b": "sha-feat-b", "feat-c": "sha-feat-c"}
    update = last_update(fake)
    assert "Landed on main in api: feat-a (Dev1); feat-b (Dev2); feat-c (Dev3)." in update and "for 3 branches" in update
    assert [t.members for t in fake.train_specs] == [["feat-a", "feat-b", "feat-c"], ["feat-d"]]  # the next train left at once
    finish_check(client, fake)
    assert len(fake.check_specs) == 2 and len(fake.land_specs) == 2  # four branches, two check runs


def test_a_train_waits_a_moment_for_further_branches(client, project):
    from plainboard.models import utcnow

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"])
    watcher = client.app_ref.state.landing
    watcher.linger = 90
    land_tick(client)
    assert not fake.train_specs  # a second member may be about to finish: one train for both is one check run
    late = make_member(client, pid, "Late")
    fake.branches["feat-b"] = "sha-feat-b"
    chat(client, late["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-b"}))
    wait_for(lambda: idle(client, pid))
    client.portal.call(watcher.tick, utcnow() + timedelta(seconds=91))
    assert [t.members for t in fake.train_specs] == [["feat-a", "feat-b"]]


def hand_over(client, pid: int, fake: FakeRunner, name: str, branch: str) -> None:
    member = make_member(client, pid, name)
    fake.branches[branch] = f"sha-{branch}"
    chat(client, member["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": branch}))
    wait_for(lambda: idle(client, pid))


def test_a_failed_train_goes_to_one_member_who_makes_its_branches_pass_together(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b", "feat-c", "feat-d"])
    land_tick(client)
    finish_check(client, fake, every=True)  # several of them fail, each for its own reason: no single branch to find
    # No further check run to find out why: one assignment, with all the branches and what failed.
    assert len(fake.check_specs) == 1
    (repair,) = assignments(client, pid)
    assert repair["title"] == "Make 4 branches of api pass together" and repair["agent_id"] == agents_by_slug(client, pid)["dev1"]["id"]
    prompt = next(s.prompt for s in fake.specs if "pass together" in s.prompt)
    assert "Assignment #1 from PlainBoard" in prompt and "FAILED test_login" in prompt and "failed round 1 of 3" in prompt
    assert all(f"- feat-{x} (Dev{n})" in prompt for n, x in enumerate("abcd", start=1)) and "return_branch" in prompt
    assert runs_of(client, pid)[0]["trigger"] == "assignment"  # the repair's report wakes nobody

    # Repaired: the same branches are checked again as a group, also those that needed no change.
    # What became ready meanwhile does not join them: it goes on the train after.
    hand_over(client, pid, fake, "Late", "feat-e")
    fake.branches["feat-a"] = "sha-feat-a-2"
    land_tick(client)
    assert fake.train_specs[-1].members == ["feat-a", "feat-b", "feat-c", "feat-d"] and fake.train_specs[-1].skip_unchanged == {}
    finish_check(client, fake)
    assert sorted(fake.land_specs[-1].delete_branches) == ["feat-a", "feat-b", "feat-c", "feat-d"]
    assert fake.land_specs[-1].delete_branches["feat-a"] == "sha-feat-a-2"
    assert fake.train_specs[-1].members == ["feat-e"]
    finish_check(client, fake)
    assert len(fake.check_specs) == 3 and len(fake.land_specs) == 2  # five branches, one failure: three check runs

    # The course of it is written down, not only where it ended: that is what the flow is improved with.
    events = flow_events(client, pid)
    assert [e["kind"] for e in events if e["kind"].startswith("train.")] == [
        "train.departed", "train.failed", "train.repair_ordered", "train.departed", "train.landed", "train.departed", "train.landed",
    ]  # fmt: skip
    failed = next(e for e in events if e["kind"] == "train.failed")
    assert failed["data"]["branches"] == ["feat-a", "feat-b", "feat-c", "feat-d"] and "FAILED test_login" in failed["data"]["evidence"]
    ordered = next(e for e in events if e["kind"] == "train.repair_ordered")
    assert ordered["assignment_id"] == repair["id"] and ordered["agent_id"] == repair["agent_id"] and ordered["train_id"] == failed["train_id"]
    assert sum(e["kind"] == "landing.handed_over" for e in events) == 5
    report = client.get(f"/api/projects/{pid}/work-report?days=1").json()
    assert report["flow_events"]["train.failed"] == 1 and [t["status"] for t in report["trains"]] == ["landed", "landed", "failed"]
    assert [(t["branches"], t["repaired_group"]) for t in report["trains"]] == [(1, False), (4, True), (4, False)]


def test_a_branch_that_needs_more_is_given_back_and_the_others_land_without_it(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b", "feat-c"])
    land_tick(client)
    # The member that repairs the train finds that feat-c cannot be fixed quickly (its turn is scripted through the check's output).
    taken_off = "TOOL return_branch " + json.dumps({"repository": "api", "branch": "feat-c", "reason": "the parser has to be redesigned"})
    fake.jobs[fake.check_specs[-1].id] = JobState(running=False, exit_code=1, log_path="/jobs/check.log", tail=taken_off)
    land_tick(client)
    wait_for(lambda: idle(client, pid) and len(assignments(client, pid)) == 2 and all(a["status"] == "done" for a in assignments(client, pid)))
    together, alone = assignments(client, pid)
    assert alone["title"] == "Repair branch feat-c" and alone["agent_id"] == agents_by_slug(client, pid)["dev3"]["id"]
    prompt = next(s.prompt for s in fake.specs if "Repair branch feat-c" in s.prompt)
    assert "Dev1, who repaired them, took yours off" in prompt and "the parser has to be redesigned" in prompt
    assert "is back with its author" in tool_results(client, agents_by_slug(client, pid)["dev1"]["id"])[-1]

    fake.branches["feat-c"] = "sha-feat-c-2"
    land_tick(client)
    assert fake.train_specs[-1].members == ["feat-a", "feat-b"]  # unchanged, but without the branch that broke them
    finish_check(client, fake)
    assert sorted(fake.land_specs[-1].delete_branches) == ["feat-a", "feat-b"] and fake.train_specs[-1].members == ["feat-c"]
    finish_check(client, fake)
    assert sorted(fake.land_specs[-1].delete_branches) == ["feat-c"]

    # Outside such a repair nobody takes branches off.
    chat(client, agents_by_slug(client, pid)["dev2"]["id"], "TOOL return_branch " + json.dumps({"repository": "api", "branch": "feat-a", "reason": "I do not like it"}))
    wait_for(lambda: idle(client, pid))
    assert "is not one of the branches you are repairing" in tool_results(client, agents_by_slug(client, pid)["dev2"]["id"])[-1]


def test_a_repair_that_changes_nothing_or_branches_that_keep_failing_go_to_the_manager(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b"])
    land_tick(client)
    for round_no in (1, 2):
        finish_check(client, fake, every=True)
        wait_for(lambda: idle(client, pid) and len(assignments(client, pid)) == round_no and assignments(client, pid)[-1]["status"] == "done")
        fake.branches["feat-b"] = f"try-{round_no}"
        land_tick(client)
    finish_check(client, fake, every=True)
    wait_for(lambda: idle(client, pid) and "3 times now" in last_update(fake))
    update = last_update(fake)
    assert "feat-a (Dev1); feat-b (Dev2) did not pass together" in update and "PlainBoard has stopped for these branches" in update
    assert len(fake.check_specs) == 3 and len(assignments(client, pid)) == 2  # the third failure is not handed out again
    land_tick(client)
    assert len(fake.train_specs) == 3


def test_a_group_whose_repair_changed_nothing_is_not_checked_again(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b"])
    land_tick(client)
    finish_check(client, fake, every=True)  # e.g. the base branch is broken, or a check fails at random
    wait_for(lambda: idle(client, pid) and assignments(client, pid) and assignments(client, pid)[0]["status"] == "done")
    land_tick(client)  # the repair pushed nothing: the same commits would fail the same way
    wait_for(lambda: idle(client, pid) and "their repair changed none of them" in last_update(fake))
    assert len(fake.check_specs) == 1 and len(fake.train_specs) == 1


def test_a_repair_without_a_push_and_a_branch_that_keeps_failing_go_to_the_manager(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"])
    land_tick(client)
    finish_check(client, fake, culprit="feat-a")
    wait_for(lambda: idle(client, pid) and assignments(client, pid) and assignments(client, pid)[0]["status"] == "done")
    land_tick(client)  # repaired, says its author, but nothing new was pushed
    wait_for(lambda: idle(client, pid) and "still fails, and nothing new was pushed" in last_update(fake))
    assert len(fake.check_specs) == 1  # no check run for the same commit again

    # The manager hands it over again; it fails with new commits each time.
    for round_no in (1, 2, 3):
        fake.branches["feat-a"] = f"try-{round_no}"
        if round_no == 1:
            chat(client, manager_of(client, pid)["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-a"}))
            wait_for(lambda: idle(client, pid))
        land_tick(client)
        finish_check(client, fake, culprit="feat-a")
        wait_for(lambda: idle(client, pid))
    assert "3 times now" in last_update(fake) and "PlainBoard has stopped for this branch" in last_update(fake)
    assert len(assignments(client, pid)) == 3  # two more repairs; the third failure is not handed back


def test_a_branch_that_conflicts_stays_behind_and_the_rest_goes(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b", "feat-c"])
    fake.conflicting.add("feat-b")
    fake.on_base.add("feat-c")
    land_tick(client)
    wait_for(lambda: idle(client, pid) and assignments(client, pid))
    assert fake.train_results[-1].merged == {"feat-a": "sha-feat-a"} and len(fake.check_specs) == 1
    (repair,) = assignments(client, pid)
    assert repair["title"] == "Resolve the conflicts of branch feat-b"
    assert "feat-c (Dev3) is on main in api already" in last_update(fake)


def test_a_check_lost_with_the_runner_or_a_base_branch_that_moved_means_another_train(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"])
    land_tick(client)
    fake.jobs.clear()  # the runner restarted
    land_tick(client)
    assert len(fake.train_specs) == 2 and len(fake.check_specs) == 2
    fake.land_error = "! [rejected] main -> main (fetch first)"
    finish_check(client, fake)
    assert len(fake.train_specs) == 3  # assembled and checked again on the new base
    fake.land_error = "remote: GitLab: You are not allowed to push code to protected branches"
    finish_check(client, fake)
    assert "could not be pushed to main" in last_update(fake) and len(fake.train_specs) == 3


def test_on_github_a_pull_request_for_the_whole_train_is_checked_and_merged(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b"], url="https://github.com/acme/api.git", check_command="")
    land_tick(client)
    (train,) = fake.train_specs
    assert train.github == "acme/api" and train.branch.startswith("plainboard/train-") and "feat-a" in train.body and not fake.check_specs
    number = fake.train_results[-1].number
    before = len(fake.specs)
    land_tick(client)  # its checks are still running
    assert len(fake.specs) == before and not fake.merged

    fake.pulls[("acme/api", number)] = fake.pulls[("acme/api", number)].model_copy(update={"checks": [PullRequestCheck(name="ci / test", state="pass")]})
    land_tick(client)
    wait_for(lambda: idle(client, pid) and "Landed on main in api: feat-a (Dev1); feat-b (Dev2)" in last_update(fake))
    ((repo, merged, options),) = fake.merged
    assert (repo, merged) == ("acme/api", number) and options["methods"][0] == "merge" and options["delete_branches"] == {"feat-a": "sha-feat-a", "feat-b": "sha-feat-b"}


def test_on_github_failed_checks_of_a_single_branch_go_back_with_their_names(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"], url="https://github.com/acme/api.git", check_command="")
    land_tick(client)
    number = fake.train_results[-1].number
    checks = [PullRequestCheck(name="ci / lint", state="fail", link="https://ci/lint"), PullRequestCheck(name="ci / test", state="pending")]
    fake.pulls[("acme/api", number)] = fake.pulls[("acme/api", number)].model_copy(update={"checks": checks})
    land_tick(client)  # one check still runs: a failure is reported once, with all of them
    assert not assignments(client, pid)
    checks[1] = PullRequestCheck(name="ci / test", state="fail", link="https://ci/test")
    fake.pulls[("acme/api", number)] = fake.pulls[("acme/api", number)].model_copy(update={"checks": checks})
    land_tick(client)
    wait_for(lambda: idle(client, pid) and assignments(client, pid))
    prompt = next(s.prompt for s in fake.specs if "Repair branch feat-a" in s.prompt)
    assert "ci / lint: https://ci/lint" in prompt and "ci / test: https://ci/test" in prompt and fake.closed == [number]


def test_the_quick_check_runs_at_hand_over_and_a_failure_never_reaches_a_train(client, project):
    pid = project["id"]
    body = {"url": "https://gitlab.com/acme/api.git", "check_command": "./ci.sh", "quick_check_command": "cargo fmt --check"}
    assert client.post(f"/api/projects/{pid}/repositories", json=body).status_code == 201
    fake = runner(client)
    dev = make_member(client, pid)
    fake.branches["feat-a"] = "abc1234def"
    waiting = lambda: bool(client.app_ref.state.runs._job_waiters)  # noqa: E731

    def hand_over_and_end_with(exit_code: int, tail: str) -> str:
        jobs = len(fake.job_specs)
        chat(client, dev["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-a"}))
        wait_for(lambda: len(fake.job_specs) > jobs and waiting())
        fake.jobs[fake.job_specs[-1].id] = JobState(running=False, exit_code=exit_code, log_path="/jobs/quick.log", tail=tail)
        job_tick(client)
        wait_for(lambda: idle(client, pid))
        return tool_results(client, dev["id"])[-1]

    # PlainBoard runs it in the member's clone, on exactly the commit that is handed over.
    result = hand_over_and_end_with(1, "Diff in src/home.rs at line 12")
    # On exactly the commit that is handed over: where the member has it checked out (its clone or a worktree
    # of its own, with a warm build cache), else in a worktree that is thrown away afterwards.
    command = fake.job_specs[-1].command
    assert command.startswith("# PlainBoard quick check: api" + chr(10) + "cd api || exit 97" + chr(10) + "where=$(git worktree list --porcelain") and '= "abc1234def" ] && git -C "$tree" diff --quiet HEAD' in command
    assert '(' + chr(10) + 'cd "$where" || exit 97' + chr(10) + 'cargo fmt --check' + chr(10) + ')' in command
    assert 'git worktree add -q --detach "$tmp/tree" abc1234def || exit 97' in command and '(' + chr(10) + 'cd "$tmp/tree" || exit 97' + chr(10) + 'cargo fmt --check' + chr(10) + ')' in command
    assert "git fetch -q origin feat-a" in command and command.endswith('git worktree remove --force "$tmp/tree" >/dev/null 2>&1; rm -rf "$tmp"' + chr(10) + 'exit $rc')
    assert "Not handed over: the quick check of api failed" in result and "Diff in src/home.rs at line 12" in result
    land_tick(client)
    assert not fake.train_specs  # the member fixes it in the same turn; no check run was spent on it

    assert "could not check out the commit you pushed (abc1234def)" in hand_over_and_end_with(97, "")
    land_tick(client)
    assert not fake.train_specs

    result = hand_over_and_end_with(0, "ok")
    assert "is with PlainBoard now. The quick check passed." in result
    land_tick(client)
    assert [t.members for t in fake.train_specs] == [["feat-a"]]
    events = [(e["kind"], e["data"].get("why") or e["data"].get("quick_check")) for e in flow_events(client, pid) if e["kind"].startswith("landing.")]
    assert events == [("landing.quick_check_failed", "failed"), ("landing.quick_check_failed", "not_as_pushed"), ("landing.handed_over", "The quick check passed.")]

    # The manager hands over what others wrote: a decision, not a check.
    jobs = len(fake.job_specs)
    fake.branches["feat-b"] = "0123abcd"
    chat(client, manager_of(client, pid)["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-b"}))
    wait_for(lambda: idle(client, pid))
    assert len(fake.job_specs) == jobs and "is with PlainBoard now" in tool_results(client, manager_of(client, pid)["id"])[-1]


def test_branches_can_only_be_handed_over_where_plainboard_can_check_them(client, project):
    pid = project["id"]
    dev = make_member(client, pid)
    assert client.post(f"/api/projects/{pid}/repositories", json={"url": "https://gitlab.com/acme/api.git"}).status_code == 201
    hand = "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-a"})
    chat(client, dev["id"], hand)
    wait_for(lambda: idle(client, pid))
    assert "has no check command and is not on GitHub" in runs_of(client, pid)[0]["result_text"]

    # The manager gives the repository a check command: now it works on any host.
    chat(client, manager_of(client, pid)["id"], "TOOL update_repository " + json.dumps({"repository": "api", "check_command": "make check"}))
    wait_for(lambda: idle(client, pid))
    chat(client, dev["id"], hand)
    wait_for(lambda: idle(client, pid))
    assert "is not on the remote of api: push it first" in runs_of(client, pid)[0]["result_text"]
    runner(client).branches["feat-a"] = "a1"
    chat(client, dev["id"], hand)
    wait_for(lambda: idle(client, pid))
    assert "is with PlainBoard now" in runs_of(client, pid)[0]["result_text"]


def test_a_ticket_stays_in_flight_until_its_branch_has_landed(client, project):
    pid = project["id"]
    make_member(client, pid)
    assert client.post(f"/api/projects/{pid}/repositories", json={"url": "https://gitlab.com/acme/api.git", "check_command": "./ci.sh"}).status_code == 201
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Feature", "status": "todo"}).json()
    fake = runner(client)
    fake.branches["feat-a"] = "a1"
    order = delegate("dev", "Add it", "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-a"}), ticket_number=1)
    chat(client, manager_of(client, pid)["id"], order)
    # The manager's turn for the report lists the branch as in flight, so the ticket is not one nothing happens on.
    wait_for(lambda: "Branch feat-a for #1, Dev: goes on the next train" in last_update(fake))
    wait_for(lambda: idle(client, pid))
    assert "Board check" not in last_update(fake)
    land_tick(client)
    finish_check(client, fake)
    assert "Landed on main in api: feat-a (Dev, ticket #1)" in last_update(fake)
    comments = client.get(f"/api/tasks/{ticket['id']}").json()["comments"]
    assert any("Branch feat-a is on main (one check run" in c["body"] for c in comments)


def test_checks_count_only_when_all_have_finished():
    from plainboard.orchestration.landing import NO_CHECKS_GRACE_SECONDS, base_moved, checks_result

    def pull(checks: dict[str, str]) -> PullRequestState:
        return PullRequestState(state="open", checks=[PullRequestCheck(name=name, state=result) for name, result in checks.items()])

    assert checks_result(pull({"a": "fail", "b": "pending"}), 600) == "pending"
    assert checks_result(pull({"a": "fail", "b": "pass"}), 600) == "failed"
    assert checks_result(pull({"a": "pass"}), 5) == "passed"
    assert checks_result(pull({}), NO_CHECKS_GRACE_SECONDS - 1) == "pending"  # they may not have appeared yet
    assert checks_result(pull({}), NO_CHECKS_GRACE_SECONDS) == "passed"  # a repository without checks
    assert base_moved("! [rejected] main -> main (fetch first)") and not base_moved("protected branch hook declined")


# --- time ------------------------------------------------------------------------------------


def test_durations_are_said_in_plain_words():
    from plainboard.services.timing import Ledger, command_key, duration

    assert [duration(s) for s in (45, 600, 5400, 6720, 200000)] == ["45 s", "10 min", "1 h 30 min", "1 h 52 min", "2 d 7 h"]
    ledger = Ledger(total=6720, work=540, waiting=5760, queued=420, turns=4, jobs=3)
    assert ledger.text() == "1 h 52 min: 9 min of work in 4 turns, 1 h 36 min waiting for 3 jobs, 7 min queued"
    assert Ledger(total=300, work=300, turns=1).text() == "5 min: 5 min of work in 1 turn"
    assert command_key("cd /workspaces/p/dev/api  &&  make   test") == "make test"


def test_the_same_slow_command_is_not_run_in_circles(client, project):
    from plainboard.models import Assignment, Job, JobStatus, utcnow
    from plainboard.orchestration.run_manager import RunContext

    pid = project["id"]
    dev = make_member(client, pid)
    runs = client.app_ref.state.runs

    async def scenario():
        async with runs._sm() as s:
            assignment = Assignment(project_id=pid, agent_id=dev["id"], title="Fix", instructions="x")
            s.add(assignment)
            await s.flush()
            for n in range(3):
                start = utcnow() - timedelta(minutes=90 - 25 * n)
                s.add(Job(project_id=pid, agent_id=dev["id"], assignment_id=assignment.id, runner_id=f"cd{n}", command="make test", status=JobStatus.FAILED, created_at=start, finished_at=start + timedelta(minutes=20)))
            await s.commit()
            ctx = RunContext(run_id=0, project_id=pid, agent_id=dev["id"], token="", assignment_id=assignment.id)
        refused = await runs.execute_tool(ctx, "start_job", {"command": "make test"})
        other = await runs.execute_tool(ctx, "start_job", {"command": "make lint"})
        insisted = await runs.execute_tool(ctx, "start_job", {"command": "make test", "repeat": True})
        return refused, other, insisted

    refused, other, insisted = client.portal.call(scenario)
    assert refused.is_error and "3 times already (60 min together)" in refused.text and "is a loop" in refused.text
    assert not other.is_error  # another command is another matter
    assert not insisted.is_error and "This is run 4 of it in this assignment" in insisted.text


def test_a_slow_command_says_what_it_cost_before_it_is_started_again(client, project):
    from plainboard.models import Assignment, Job, JobStatus, utcnow
    from plainboard.services import timing

    pid = project["id"]
    dev = make_member(client, pid)

    async def scenario():
        async with client.app_ref.state.runs._sm() as s:
            assignment = Assignment(project_id=pid, agent_id=dev["id"], title="Fix", instructions="x")
            s.add(assignment)
            await s.flush()
            now = utcnow()
            for n in range(2):
                start = now - timedelta(minutes=60 - 25 * n)
                s.add(Job(project_id=pid, agent_id=dev["id"], assignment_id=assignment.id, runner_id=f"ab{n}", command="make test", status=JobStatus.FAILED, created_at=start, finished_at=start + timedelta(minutes=20)))
            await s.commit()
            in_assignment = await timing.job_note(s, dev["id"], assignment.id, "make test")
            outside = await timing.job_note(s, dev["id"], None, "make test")
            unknown = await timing.job_note(s, dev["id"], assignment.id, "make lint")
            overview = await timing.overview(s, pid, 24)
            return in_assignment, outside, unknown, overview

    in_assignment, outside, unknown, overview = client.portal.call(scenario)
    assert outside == "It took about 20 min the last 2 times."
    assert in_assignment.startswith("It took about 20 min the last 2 times. This is run 3 of it in this assignment; the earlier ones took 40 min together.")
    assert "fix everything the last one showed" in in_assignment
    assert unknown == ""
    (slow,) = overview.commands
    assert (slow.command, slow.runs, slow.median, slow.total) == ("make test", 2, 1200, 2400)
    report = timing.report_text(overview, [])
    assert "40 min = 2 x 20 min: make test" in report and "Most of the time is waiting" in report
    assert "40 min waiting for jobs and checks" in timing.waiting_line(overview) or timing.waiting_line(overview) == ""
    assert "`make test` 20 min per run" in timing.facts_text(overview)


# --- what the production data showed (2026-11-16) --------------------------------------------


def set_failures(client, branch: str, failures: int) -> None:
    from plainboard.db import sessionmaker
    from plainboard.models import Landing
    from sqlalchemy import update

    async def write() -> None:
        async with sessionmaker()() as s:
            await s.execute(update(Landing).where(Landing.branch == branch).values(failures=failures))
            await s.commit()

    client.portal.call(write)


def repairs(client, pid: int) -> list[dict]:
    return [a for a in assignments(client, pid) if a["title"].startswith(("Make ", "Repair "))]


def test_a_branch_that_used_up_its_rounds_leaves_alone_and_the_others_are_repaired(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b", "feat-c"])
    set_failures(client, "feat-c", 2)  # it failed twice before, with other trains
    land_tick(client)
    finish_check(client, fake, every=True)
    wait_for(lambda: idle(client, pid) and "feat-c (Dev3) did not pass" in last_update(fake))
    # One branch that keeps failing does not send the whole train to the manager.
    update = last_update(fake)
    assert "3 times now" in update and "PlainBoard has stopped for this branch." in update
    assert "The other 2 branches of that train are being repaired without it." in update and "feat-a (Dev1)" not in update.split("did not pass")[0]
    (repair,) = repairs(client, pid)
    assert repair["title"] == "Make 2 branches of api pass together"


def test_the_repair_goes_to_an_author_who_is_free(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b"])
    dev1, dev2 = (agents_by_slug(client, pid)[slug] for slug in ("dev1", "dev2"))
    # Dev1 wrote most of the train, but is in the middle of other work.
    fake.branches["feat-x"] = "sha-feat-x"
    chat(client, dev1["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-x"}))
    wait_for(lambda: idle(client, pid))
    chat(client, manager_of(client, pid)["id"], delegate("dev1", "Long work", "TOOL start_job " + json.dumps({"command": "make all"})))
    wait_for(lambda: idle(client, pid) and fake.job_specs)
    land_tick(client)
    assert fake.train_specs[-1].members == ["feat-a", "feat-b", "feat-x"]
    finish_check(client, fake, every=True)
    # It would wait behind that work, and nothing of the train lands until it is repaired: Dev2 starts at once.
    (repair,) = repairs(client, pid)
    assert repair["agent_id"] == dev2["id"] and repair["status"] == "done"


def test_trains_get_shorter_while_they_fail_and_longer_again_when_they_pass(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, [f"feat-{c}" for c in "abcdef"])
    land_tick(client)
    assert len(fake.train_specs[-1].members) == 6  # nothing has failed: as many as are ready
    finish_check(client, fake, every=True)
    for n in range(1, 8):
        hand_over(client, pid, fake, f"New{n}", f"new-{n}")
    fake.branches["feat-a"] = "sha-feat-a-2"
    land_tick(client)
    assert fake.train_specs[-1].members == [f"feat-{c}" for c in "abcdef"]  # the repaired branches, whole and first
    finish_check(client, fake)
    # One of six branches failed its train: at that rate five per train land most per check run.
    assert fake.train_specs[-1].members == [f"new-{n}" for n in range(1, 6)]
    finish_check(client, fake)
    assert fake.train_specs[-1].members == ["new-6", "new-7"]  # and with every train that passes, more fit again
    events = [e["data"]["size_limit"] for e in flow_events(client, pid) if e["kind"] == "train.departed"]
    assert events == [12, 12, 5, 10]


def test_a_turn_that_waits_for_its_job_takes_no_slot(client, project):
    pid = project["id"]
    assert client.put("/api/settings", json={"values": {"max_parallel_runs": 1}}).status_code == 200
    builder, other = make_member(client, pid, "Builder"), make_member(client, pid, "Other")
    chat(client, builder["id"], "TOOL start_job " + json.dumps({"command": "cargo build", "wait_minutes": 5}))
    wait_for(lambda: bool(client.app_ref.state.runs._job_waiters))
    # One agent may work at a time, and the builder only waits: the other one works meanwhile.
    chat(client, other["id"], "SAY done meanwhile")
    wait_for(lambda: any(r["agent_id"] == other["id"] and r["status"] == "succeeded" for r in runs_of(client, pid)))
    assert next(r for r in runs_of(client, pid) if r["agent_id"] == builder["id"])["status"] == "running"
    runner(client).jobs[runner(client).job_specs[-1].id] = JobState(running=False, exit_code=0, log_path="/jobs/x.log", tail="ok")
    job_tick(client)
    wait_for(lambda: idle(client, pid))


def test_the_manager_pauses_and_resumes_the_whole_team_in_one_call_each(client, project):
    pid = project["id"]
    manager = manager_of(client, pid)
    make_member(client, pid, "Dev1"), make_member(client, pid, "Dev2")
    off = make_member(client, pid, "Off")
    assert client.patch(f"/api/agents/{off['id']}", json={"enabled": False}).status_code == 200  # switched off by the user before
    chat(client, manager["id"], delegate("dev1", "Build", "TOOL start_job " + json.dumps({"command": "make all"})))
    wait_for(lambda: idle(client, pid) and runner(client).job_specs)

    chat(client, manager["id"], 'TOOL pause_team {"reason": "the user wants quiet"}')
    wait_for(lambda: idle(client, pid) and not agents_by_slug(client, pid)["dev2"]["enabled"])
    assert tool_results(client, manager["id"])[-1].startswith("Paused 2 members.")
    assert assignments(client, pid)[0]["status"] == "running"  # nothing is lost

    chat(client, manager["id"], "TOOL resume_team")
    wait_for(lambda: idle(client, pid) and agents_by_slug(client, pid)["dev2"]["enabled"])
    team = agents_by_slug(client, pid)
    assert tool_results(client, manager["id"])[-1].startswith("Resumed dev1, dev2.")
    assert team["dev1"]["enabled"] and not team["off"]["enabled"]  # whoever was off before stays off


def test_everything_is_paused_at_once_and_goes_on_where_it_stopped(client, project):
    pid = project["id"]
    manager = manager_of(client, pid)
    dev = make_member(client, pid, "Dev1")
    off = make_member(client, pid, "Off")
    assert client.patch(f"/api/agents/{off['id']}", json={"enabled": False}).status_code == 200  # switched off by the user before
    other = client.post("/api/projects", json={"name": "Other"}).json()
    other_manager = manager_of(client, other["id"])
    fake = runner(client)
    assert client.get("/api/pause").json() == {"paused": False, "by": "", "since": None, "standing": False, "moving": [], "servers": 0}

    # A member works on its assignment, and in another project the manager is in the middle of a turn.
    chat(client, manager["id"], delegate("dev1", "Build", "HOLD\nSAY built half"))
    chat(client, other_manager["id"], "HOLD\nSAY hello")
    working = lambda p, agent: any(r["status"] == "running" and r["agent_id"] == agent["id"] for r in runs_of(client, p))  # noqa: E731
    wait_for(lambda: working(pid, dev) and working(other["id"], other_manager) and not working(pid, manager))

    paused = client.post("/api/pause").json()
    assert paused["paused"] and paused["by"] == "Admin" and paused["since"].endswith("Z") and not paused["standing"]
    assert sorted((m["what"], m["project"], m["agent"]) for m in paused["moving"]) == [("turn", "Demo Project", "Dev1"), ("turn", "Other", "Manager")]
    # Both turns were told to wrap up; no manager spent a turn on the pause.
    assert len([text for _, text in fake.notices if "You are being paused" in text and "maintenance" in text]) == 2
    team = agents_by_slug(client, pid)
    assert not team["dev1"]["enabled"] and team["manager"]["enabled"]
    assert client.get("/api/settings").json()["values"]["pause"]["by"] == "Admin"  # a setting: it holds across the restart

    chat(client, manager["id"], "SAY later")  # what the user writes meanwhile waits
    fake.released = True
    wait_for(lambda: client.get("/api/pause").json()["standing"])
    assert assignments(client, pid)[0]["status"] == "running"  # nothing is lost
    assert [r["status"] for r in runs_of(client, pid) if r["agent_id"] == manager["id"]][0] == "queued"

    assert client.delete("/api/pause").json()["paused"] is False
    wait_for(lambda: idle(client, pid) and assignments(client, pid)[0]["status"] == "done")
    team = agents_by_slug(client, pid)
    assert team["dev1"]["enabled"] and not team["off"]["enabled"]  # whoever was off before stays off
    # What the user wrote meanwhile was answered, and the member's report reaches the manager as usual.
    wait_for(lambda: all(r["status"] == "succeeded" for r in runs_of(client, pid) if r["agent_id"] == manager["id"]))


def test_pausing_everything_stops_the_jobs_itself_and_their_agents_read_which(client, project):
    pid = project["id"]
    manager = manager_of(client, pid)
    dev = make_member(client, pid, "Dev1")
    fake = runner(client)
    jobs = lambda: {slug: a["running_jobs"] for slug, a in agents_by_slug(client, pid).items()}  # noqa: E731

    # A preview's server that nobody waits for, and a job of the manager, which is in the middle of a turn.
    chat(client, dev["id"], 'TOOL start_job {"command": "npm run dev"}')
    chat(client, manager["id"], 'TOOL start_job {"command": "cargo build"}')
    wait_for(lambda: idle(client, pid) and jobs() == {"manager": 1, "dev1": 1})
    turns = len(fake.specs) + 1
    chat(client, manager["id"], "HOLD\nSAY wrapped up")
    # Its turn is under way at the runner too: a run counts as running a moment before its spec arrives there.
    wait_for(lambda: any(r["status"] == "running" for r in runs_of(client, pid)) and len(fake.specs) == turns)

    # The idle member's job stops at once, without a turn for it; the manager's when its turn has wrapped up.
    assert client.post("/api/pause").json()["paused"]
    assert jobs() == {"manager": 1, "dev1": 0}
    assert any("leave your background jobs alone" in text for _, text in fake.notices)
    fake.released = True
    wait_for(lambda: client.get("/api/pause").json()["standing"] and jobs() == {"manager": 0, "dev1": 0})
    assert len(fake.specs) == turns and client.get("/api/pause").json()["servers"] == 0

    # Afterwards each reads which jobs were stopped, with the turn it has anyway.
    assert client.delete("/api/pause").json()["paused"] is False
    chat(client, dev["id"], "SAY back")
    wait_for(lambda: idle(client, pid))
    told = [note for m in client.get(f"/api/agents/{dev['id']}/conversation").json()["messages"] for note in m["meta"].get("inbox", [])]
    assert len(told) == 1 and "npm run dev" in told[0] and "Start again what you still need" in told[0]


def stand_tick(client, minutes: int = 0) -> int:
    """Look at the boards as the standstill watcher does, ``minutes`` from now. Returns how many managers it woke."""
    from plainboard.models import utcnow

    async def tick() -> int:
        return await client.app_ref.state.standstill.tick(utcnow() + timedelta(minutes=minutes))

    return client.portal.call(tick)


def test_a_manager_is_told_once_when_ready_work_stands_next_to_free_slots(client, project):
    pid = project["id"]
    make_member(client, pid, "Dev1")
    fake = runner(client)
    # A ticket is to do, and the manager's turn (if it had one) ended without ordering anything.
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Write the docs", "status": "todo"}).json()
    wait_for(lambda: idle(client, pid))
    assert stand_tick(client) == 0  # the manager has only just looked at this board
    turns = len(fake.specs)

    assert stand_tick(client, 20) == 1
    wait_for(lambda: idle(client, pid) and len(fake.specs) == turns + 1)
    told = fake.specs[-1].prompt
    assert "3 of 3 member slots are free (idle: Dev1)" in told and "- #1 Write the docs (todo)" in told and "nothing was ordered" in told
    assert [e["kind"] for e in flow_events(client, pid)].count("manager.told_of_standstill") == 1
    assert stand_tick(client, 40) == 0  # the same situation is no news

    # A new ticket is news, but not within the hour: the manager is not asked every few minutes.
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Fix the login", "status": "todo"})
    wait_for(lambda: idle(client, pid))
    assert stand_tick(client, 50) == 0
    assert stand_tick(client, 100) == 1
    wait_for(lambda: idle(client, pid) and "- #2 Fix the login (todo)" in fake.specs[-1].prompt)
    # And then never again, however long nothing changes: a project with nothing to start costs nothing.
    assert stand_tick(client, 200) == 0 and stand_tick(client, 2000) == 0
    assert "set its user_action to what the user has to do" in fake.specs[-1].prompt  # how a waiting ticket leaves this list

    # No ready work, no question: a ticket that waits for the user, members switched off by the limit, everything paused.
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Third", "status": "todo"})
    wait_for(lambda: idle(client, pid))
    for number in (1, 2, 3):
        task = next(t for t in client.get(f"/api/projects/{pid}/tasks").json() if t["number"] == number)
        assert client.patch(f"/api/tasks/{task['id']}", json={"user_action": "Try it on your phone"}).status_code == 200
    wait_for(lambda: idle(client, pid))
    assert stand_tick(client, 4000) == 0
    assert client.patch(f"/api/tasks/{ticket['id']}", json={"user_action": ""}).status_code == 200
    wait_for(lambda: idle(client, pid))
    assert client.patch(f"/api/projects/{pid}", json={"parallel_agents": 0}).status_code == 200
    assert stand_tick(client, 4000) == 0
    assert client.patch(f"/api/projects/{pid}", json={"parallel_agents": 2}).status_code == 200
    assert client.post("/api/pause").json()["paused"]
    assert stand_tick(client, 4000) == 0
    assert client.delete("/api/pause").json()["paused"] is False
    assert stand_tick(client, 4000) == 1  # and with all that gone, it is asked about the ticket that is left


def test_the_same_ready_tickets_are_told_once_while_work_runs_and_once_more_when_all_rests():
    from types import SimpleNamespace

    from plainboard.services.standstill import Standstill, is_news

    def found(tickets: list[int], resting: bool) -> Standstill:
        return Standstill(1, [SimpleNamespace(id=t) for t in tickets], free=3, limit=4, idle=["Dev"], quiet_seconds=900, resting=resting)

    assert is_news(found([1, 2], resting=False), None)
    # While other work comes and goes, the same tickets are no news: that cost a turn an hour for tickets that waited for the user.
    assert not is_news(found([1, 2], resting=False), (frozenset({1, 2}), False))
    # Once nothing runs at all, they are told once more, and then never again, also not when work starts and ends later.
    assert is_news(found([1, 2], resting=True), (frozenset({1, 2}), False))
    assert not is_news(found([1, 2], resting=True), (frozenset({1, 2}), True))
    assert not is_news(found([1, 2], resting=False), (frozenset({1, 2}), True))
    assert is_news(found([1, 2, 3], resting=False), (frozenset({1, 2}), True))  # a ticket it has not heard of is news
    assert not is_news(found([1], resting=True), (frozenset({1, 2}), True))  # fewer of the same are not


def test_a_project_goes_on_with_its_backlog_unless_it_is_switched_off(client, project):
    pid = project["id"]
    make_member(client, pid, "Dev1")
    fake = runner(client)
    assert client.get(f"/api/projects/{pid}").json()["continue_with_backlog"] is True  # the default
    # Nothing is to do, but two tickets are in the backlog, one of them urgent.
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Tidy the imports", "priority": "low"})
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Fix the crash", "priority": "urgent"})
    wait_for(lambda: idle(client, pid))
    turns = len(fake.specs)

    assert stand_tick(client, 20) == 1
    wait_for(lambda: idle(client, pid) and len(fake.specs) == turns + 1)
    told = fake.specs[-1].prompt
    # The tickets are named, the most urgent first: the manager answers to them, not to its memory of the backlog.
    assert told.index("- #2 Fix the crash (backlog)") < told.index("- #1 Tidy the imports (backlog)") and "goes on with its Backlog" in told
    assert "## Backlog\nWhen nothing in To do can start, go on with the Backlog" in fake.specs[-1].system_prompt

    # Switched off, the backlog is the user's again: nobody is woken for it, and the manager is told to leave it.
    assert client.patch(f"/api/projects/{pid}", json={"continue_with_backlog": False}).json()["continue_with_backlog"] is False
    client.post(f"/api/projects/{pid}/tasks", json={"title": "A third"})
    wait_for(lambda: idle(client, pid))
    assert stand_tick(client, 200) == 0
    chat(client, manager_of(client, pid)["id"], "SAY ok")
    wait_for(lambda: idle(client, pid) and fake.specs[-1].prompt.endswith("SAY ok"))
    # Its running session gets the few lines of that rule, not the whole guide again.
    update = fake.specs[-1].prompt
    assert "[Briefing update]" in update and "Backlog is not ordered yet" in update and "How work flows" not in update


def test_a_ticket_with_several_reports_keeps_its_state_and_work_starts_from_it(client, project):
    pid = project["id"]
    make_member(client, pid)
    manager = manager_of(client, pid)
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Make Home fast", "description": "Back to Home takes 4 s", "status": "in_progress"}).json()
    fake = runner(client)

    def order(title: str, report: str) -> None:
        chat(client, manager["id"], delegate("dev", title, f"SAY {report}", ticket_number=1))
        wait_for(lambda: idle(client, pid) and all(a["status"] == "done" for a in assignments(client, pid)))

    order("Measure", "3.4 s from the text page, measured on the X4")
    order("Cache the layout", "tried caching the layout: no gain, dropped")
    assert client.get(f"/api/tasks/{ticket['id']}").json()["state"] == "" and not fake.completions  # two reports say it themselves
    order("Cut the service calls", "26 calls cost 1.31 s; cut to 19 on branch fw/home-calls")
    wait_for(lambda: client.get(f"/api/tasks/{ticket['id']}").json()["state"])
    # PlainBoard wrote it from the reports, with the small model; no agent spent a step on it.
    (spec,) = fake.completions
    assert "nothing is done twice" in spec.system_prompt and "Tried and dropped" in spec.system_prompt
    assert "Ticket #1: Make Home fast" in spec.prompt and "State so far:\n(none yet)" in spec.prompt
    assert "Assignment #2 (Dev, done): Cache the layout\ntried caching the layout: no gain, dropped" in spec.prompt and "26 calls cost 1.31 s" in spec.prompt
    detail = client.get(f"/api/tasks/{ticket['id']}").json()
    assert detail["state"] == "SUMMARY #1" and detail["state_updated_at"] and detail["comments"] == []

    # The next piece of work on the ticket starts from it, and so does whoever reads the ticket.
    order("Next cut", "down to 2.4 s")
    prompt = next(s.prompt for s in reversed(fake.specs) if "Assignment #4 from Manager" in s.prompt)
    assert "Where ticket #1 stands, from the reports so far." in prompt and "SUMMARY #1" in prompt and "don't redo what is done" in prompt
    chat(client, manager["id"], 'TOOL get_task {"number": 1}')
    wait_for(lambda: idle(client, pid))
    text = tool_results(client, manager["id"])[-1]
    assert "Where it stands (kept by PlainBoard from the reports up to assignment #3; newer ones are below):\nSUMMARY #1" in text and "#4 [done] Dev: Next cut" in text
    assert len(fake.completions) == 1  # rewritten only when three more reports have come in
    order("Fifth", "x")
    order("Sixth", "y")
    wait_for(lambda: len(fake.completions) == 2 and client.get(f"/api/tasks/{ticket['id']}").json()["state"] == "SUMMARY #2")
    assert fake.completions[-1].effort == "high"  # no effort of the manager's own: the installation's for summaries
    assert "State so far:\nSUMMARY #1" in fake.completions[-1].prompt and "Assignment #6" in fake.completions[-1].prompt and "Assignment #3 " not in fake.completions[-1].prompt


# --- land first, check after -----------------------------------------------------------------


def land_tick_at(client, when) -> None:
    client.portal.call(client.app_ref.state.landing.tick, when)


def test_land_first_merges_at_once_and_a_red_base_is_repaired_by_one_assignment(client, project, lands_on_red):
    from plainboard.models import utcnow

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b"], url="https://github.com/acme/api.git", check_command="", land_first=True)
    assert "within minutes; the full checks run on the base branch afterwards" in runs_of(client, pid)[0]["result_text"]
    start = utcnow() - timedelta(hours=1)
    land_tick_at(client, start)
    wait_for(lambda: idle(client, pid) and "Landed on main in api: feat-a (Dev1); feat-b (Dev2)." in last_update(fake))
    # On the base branch at once: no pull request, and no check run anybody waits for.
    (landed,) = fake.land_specs
    assert landed.head_sha == "train-1" and sorted(landed.delete_branches) == ["feat-a", "feat-b"] and fake.train_specs[-1].github is None and not fake.pulls
    assert "Its checks run on main now, once for all of this" in last_update(fake)

    # While those checks run, what gets ready waits: a newer push would replace the run and no result would ever come.
    hand_over(client, pid, fake, "Dev3", "feat-c")
    fake.commit_checks_of["train-1"] = [PullRequestCheck(name="Tests", state="pending"), PullRequestCheck(name="Lint", state="pass")]
    land_tick_at(client, start + timedelta(minutes=10))
    assert len(fake.land_specs) == 1

    # They fail. Nothing is taken back and nothing stops: one member repairs the base branch, and the next batch lands.
    fake.commit_checks_of["train-1"] = [PullRequestCheck(name="Tests", state="fail", link="https://ci/1"), PullRequestCheck(name="Lint", state="pass")]
    now = utcnow()
    land_tick_at(client, now)
    wait_for(lambda: idle(client, pid) and repairs(client, pid) and repairs(client, pid)[0]["status"] == "done")
    (repair,) = repairs(client, pid)
    assert repair["title"] == "Make main of api pass its checks again" and repair["agent_id"] == agents_by_slug(client, pid)["dev1"]["id"]
    prompt = next(s.prompt for s in fake.specs if "Nothing is taken back" in s.prompt)
    assert "- feat-a (Dev1)\n- feat-b (Dev2)" in prompt and "- Tests: https://ci/1" in prompt and "Nothing is taken back: repair it forward" in prompt and "repair 1 of 3" in prompt
    assert [sorted(spec.delete_branches) for spec in fake.land_specs] == [["feat-a", "feat-b"], ["feat-c"]]
    assert "does not pass" not in last_update(fake)  # the manager is not needed for this

    # The fix is a branch like any other. The batch that landed meanwhile is red for the same reason: no second repair.
    fake.branches["fix-tests"] = "sha-fix"
    chat(client, agents_by_slug(client, pid)["dev1"]["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "fix-tests"}))
    wait_for(lambda: idle(client, pid))
    fake.commit_checks_of["train-2"] = [PullRequestCheck(name="Tests", state="fail", link="https://ci/2")]
    land_tick_at(client, now + timedelta(minutes=30))
    wait_for(lambda: idle(client, pid))
    assert len(repairs(client, pid)) == 1 and sorted(fake.land_specs[-1].delete_branches) == ["fix-tests"]
    # What broke and what fixed it reaches the manager with its next turn (no turn of its own), and the ticket it came from.
    assert "main of api was red after a landing (Tests); Dev1 repaired it in" in last_update(fake) and "(assignment #1)" in last_update(fake)
    assert "write one line on that ticket (add_comment): what it broke and how you fixed it" in prompt

    # Still red with the fix on it: the next repair. Green after that one: nothing more.
    fake.commit_checks_of["train-3"] = [PullRequestCheck(name="Tests", state="fail", link="https://ci/3")]
    land_tick_at(client, now + timedelta(minutes=60))
    wait_for(lambda: idle(client, pid) and len(repairs(client, pid)) == 2)
    assert "repair 2 of 3" in next(s.prompt for s in reversed(fake.specs) if "Nothing is taken back" in s.prompt)
    events = [e["kind"] for e in flow_events(client, pid) if e["kind"].startswith(("base.", "train."))]
    assert events == ["train.landed", "base.red", "base.repair_ordered", "train.landed", "base.red", "base.still_red", "train.landed", "base.red", "base.repair_ordered"]
    report = client.get(f"/api/projects/{pid}/work-report?days=1").json()
    assert report["time"]["branches_landed"] == 4 and report["time"]["landing_check_runs_failed"] == 3
    # The statistics show the speed of it: per day, and this period against the one before.
    stats = client.get(f"/api/projects/{pid}/stats?days=7").json()
    days = stats["days"][-2:]  # the first landing is an hour back: shortly after midnight that is yesterday

    def total(key: str) -> int:
        return sum(day[key] for day in days)

    # ... and the last one an hour ahead: shortly before midnight that is tomorrow, which no day shows yet.
    if (now + timedelta(minutes=60)).date() == now.date():
        assert (total("branches_landed"), total("check_runs"), total("check_runs_failed"), total("repairs")) == (4, 3, 3, 2)
        assert any(day["landing_seconds"] is not None for day in days)
    assert stats["current"]["branches_landed"] == 4 and stats["current"]["repairs"] == 2 and stats["current"]["assignments_done"] == 2
    assert stats["previous"] == {"tickets_done": 0, "branches_landed": 0, "landing_seconds": None, "check_runs": 0, "check_runs_failed": 0, "assignments_done": 0, "repairs": 0, "cost_usd": 0.0}


def test_while_the_base_is_red_only_its_repair_lands_and_not_for_longer_than_the_hold(client, project):
    """Seen in production: every batch broke something, and the next ones landed on top before the repair did.
    The base branch was red for hours, each check run for another mix of reasons."""
    from plainboard.models import utcnow

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"], url="https://github.com/acme/api.git", check_command="", land_first=True)
    manager = manager_of(client, pid)
    red = [PullRequestCheck(name="Tests", state="fail", link="https://ci/t")]
    green = [PullRequestCheck(name="Tests", state="pass")]
    landed = lambda: [sorted(spec.delete_branches) for spec in fake.land_specs]  # noqa: E731
    now = utcnow()  # the manager's turn looks at the clock, so the first red check run ends now
    land_tick_at(client, now - timedelta(minutes=15))

    # Red: PlainBoard has it repaired, and what was finished meanwhile waits instead of landing on top.
    hand_over(client, pid, fake, "Dev2", "feat-b")
    fake.commit_checks_of["train-1"] = red
    land_tick_at(client, now)
    wait_for(lambda: idle(client, pid) and repairs(client, pid) and repairs(client, pid)[0]["status"] == "done")
    assert landed() == [["feat-a"]]

    # The manager reads it with its turn, and that nobody repairs it now (PlainBoard's repair handed over no fix).
    # Its own repair is marked as one: that branch lands, alone, while feat-b still waits.
    fake.branches["fix-1"] = "sha-fix-1"
    order = {"agent": "dev2", "title": "Fix the tests on main", "instructions": "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "fix-1"}), "base_repair": True}
    chat(client, manager["id"], "TOOL delegate " + json.dumps(order))
    wait_for(lambda: idle(client, pid) and any(a["title"] == "Fix the tests on main" and a["status"] == "done" for a in assignments(client, pid)))
    told = next(s.prompt for s in fake.specs if "TOOL delegate" in s.prompt and "Fix the tests on main" in s.prompt)
    assert "Red base branch: main of api has failed its checks for" in told and "(Tests)" in told
    assert "Finished branches wait for its repair (1 now), at most" in told and "delegate the repair with base_repair=true" in told
    land_tick_at(client, now + timedelta(minutes=10))
    wait_for(lambda: idle(client, pid))
    assert landed() == [["feat-a"], ["fix-1"]]

    # Green with the fix: what waited lands.
    fake.commit_checks_of["train-2"] = green
    land_tick_at(client, now + timedelta(minutes=25))
    wait_for(lambda: idle(client, pid))
    assert landed() == [["feat-a"], ["fix-1"], ["feat-b"]]

    # Red again, and this time no fix comes. Finished work waits 45 minutes from the first red check run, not longer.
    fake.commit_checks_of["train-3"] = red
    hand_over(client, pid, fake, "Dev3", "feat-c")
    land_tick_at(client, now + timedelta(minutes=40))
    wait_for(lambda: idle(client, pid))
    land_tick_at(client, now + timedelta(minutes=80))
    assert len(landed()) == 3
    land_tick_at(client, now + timedelta(minutes=86))
    wait_for(lambda: idle(client, pid))
    assert landed()[-1] == ["feat-c"]
    events = [e["kind"] for e in flow_events(client, pid) if e["kind"] in ("base.hold", "base.red", "base.green")]
    assert events == ["base.red", "base.hold", "base.green", "base.red", "base.hold"]


def test_a_new_failure_on_a_red_base_gets_its_own_repair_without_what_is_being_repaired(client, project, lands_on_red):
    from plainboard.models import utcnow

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b"], url="https://github.com/acme/api.git", check_command="", land_first=True)
    land_tick_at(client, utcnow() - timedelta(hours=1))
    hand_over(client, pid, fake, "Dev3", "feat-c")
    fake.commit_checks_of["train-1"] = [PullRequestCheck(name="Tests", state="fail", link="https://ci/t1")]
    now = utcnow()
    land_tick_at(client, now)  # red: the first repair; feat-c lands meanwhile
    wait_for(lambda: idle(client, pid) and len(repairs(client, pid)) == 1 and repairs(client, pid)[0]["status"] == "done")
    # The next batch is red for the same reason (the fix has not landed) and for a new one.
    fake.commit_checks_of["train-2"] = [PullRequestCheck(name="Tests", state="fail", link="https://ci/t2"), PullRequestCheck(name="Licenses", state="fail", link="https://ci/l2")]
    land_tick_at(client, now + timedelta(minutes=30))
    wait_for(lambda: idle(client, pid) and len(repairs(client, pid)) == 2)
    first, second = repairs(client, pid)
    assert second["agent_id"] == agents_by_slug(client, pid)["dev3"]["id"]  # it wrote what landed with the new failure
    prompt = next(s.prompt for s in reversed(fake.specs) if "Nothing is taken back" in s.prompt)
    assert "- Licenses: https://ci/l2" in prompt and "- Tests" not in prompt
    assert f"These failed too, and assignment #{first['id']} repairs them (its fix has not landed yet): Tests." in prompt


def test_other_work_of_the_member_that_repaired_is_not_taken_for_the_fix(client, project, lands_on_red):
    from plainboard.models import utcnow

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"], url="https://github.com/acme/api.git", check_command="", land_first=True)
    dev1 = agents_by_slug(client, pid)["dev1"]["id"]
    red = [PullRequestCheck(name="Tests", state="fail", link="https://ci/t")]

    def dev1_hands_over(branch: str) -> None:
        fake.branches[branch] = f"sha-{branch}"
        chat(client, dev1, "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": branch}))
        wait_for(lambda: idle(client, pid))

    land_tick_at(client, utcnow() - timedelta(hours=1))
    fake.commit_checks_of["train-1"] = red
    dev1_hands_over("work-1")
    now = utcnow()
    land_tick_at(client, now)  # red: the repair goes to Dev1, whose report hands over no fix; work-1 lands
    wait_for(lambda: idle(client, pid) and len(repairs(client, pid)) == 1 and repairs(client, pid)[0]["status"] == "done")
    fake.commit_checks_of["train-2"] = red
    dev1_hands_over("work-2")
    land_tick_at(client, now + timedelta(minutes=30))  # still red, but the repair ended after this batch was pushed: its fix could not be in it
    wait_for(lambda: idle(client, pid))
    assert len(repairs(client, pid)) == 1
    # Dev1 goes on with its own work and hands that over. It is no fix: the base branch is red, and nobody repairs it.
    fake.commit_checks_of["train-3"] = red
    dev1_hands_over("work-3")
    land_tick_at(client, now + timedelta(minutes=60))
    wait_for(lambda: idle(client, pid) and len(repairs(client, pid)) == 2)
    assert "repair 2 of 3" in next(s.prompt for s in reversed(fake.specs) if "Nothing is taken back" in s.prompt)


def test_a_repair_that_was_cancelled_is_not_ordered_again_with_every_batch(client, project, lands_on_red):
    """Seen in production: the manager repaired a red base branch with assignments of its own and cancelled
    PlainBoard's. PlainBoard ordered it again with every red batch, and it was cancelled again: 28 times in a day."""
    from plainboard.models import utcnow

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"], url="https://github.com/acme/api.git", check_command="", land_first=True)
    manager = manager_of(client, pid)
    dev1 = agents_by_slug(client, pid)["dev1"]["id"]
    red = [PullRequestCheck(name="Tests", state="fail", link="https://ci/t")]
    start = utcnow() - timedelta(hours=3)
    land_tick_at(client, start)
    hand_over(client, pid, fake, "Dev2", "work-2")
    # Dev1 wrote what landed and is in the middle of something: the repair waits for it, and the manager cancels it.
    chat(client, dev1, "HOLD\nSAY done")
    wait_for(lambda: any(r["status"] == "running" for r in runs_of(client, pid)))
    fake.commit_checks_of["train-1"] = red
    land_tick_at(client, start + timedelta(minutes=15))
    wait_for(lambda: len(repairs(client, pid)) == 1)
    chat(client, manager["id"], "TOOL cancel_assignment " + json.dumps({"assignment_id": repairs(client, pid)[0]["id"]}))
    wait_for(lambda: repairs(client, pid)[0]["status"] == "cancelled")
    fake.released = True
    wait_for(lambda: idle(client, pid))

    # Red again and again: four batches order nothing. Then it is ordered once more, so the branch cannot stay red for good.
    for batch in range(2, 7):
        fake.commit_checks_of[f"train-{batch}"] = red
        hand_over(client, pid, fake, f"Dev{batch + 1}", f"work-{batch + 1}")
        land_tick_at(client, start + timedelta(minutes=15 * batch))
        wait_for(lambda: idle(client, pid))
        assert len(repairs(client, pid)) == (1 if batch < 6 else 2), batch
    events = [e["kind"] for e in flow_events(client, pid) if e["kind"].startswith("base.")]
    assert events == ["base.red", "base.repair_ordered", *["base.red", "base.left_alone"] * 4, "base.red", "base.repair_ordered"]


def test_a_base_that_is_red_for_a_new_reason_each_time_keeps_getting_repairs(client, project, lands_on_red):
    """Seen in production: eight red landings, each for other checks. After three repairs everything
    went to the manager, although none of the new failures had been repaired even once."""
    from plainboard.models import utcnow

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-1"], url="https://github.com/acme/api.git", check_command="", land_first=True)
    now = utcnow()
    land_tick_at(client, now - timedelta(hours=1))

    def next_batch(batch: int, failing: str, minutes: int) -> None:
        """The batch that landed last fails on ``failing``; another branch lands with this look."""
        wait_for(lambda: idle(client, pid) and all(a["status"] == "done" for a in repairs(client, pid)))  # the last repair has reported
        fake.commit_checks_of[f"train-{batch}"] = [PullRequestCheck(name=failing, state="fail", link=f"https://ci/{batch}")]
        hand_over(client, pid, fake, f"Dev{batch + 1}", f"feat-{batch + 1}")
        land_tick_at(client, now + timedelta(minutes=minutes))
        wait_for(lambda: idle(client, pid))

    # Four landings, each red for another check: four repairs, each the first for its failure.
    for batch, failing in enumerate(["Tests", "Firmware", "Android", "Docs"], start=1):
        next_batch(batch, failing, 30 * batch)
    wait_for(lambda: len(repairs(client, pid)) == 4)
    prompts_ = [s.prompt for s in fake.specs if "Nothing is taken back" in s.prompt]
    assert all("repair 1 of 3" in p for p in prompts_) and "does not pass its checks" not in last_update(fake)

    # One that stays red for the same reason: three repairs, then the manager, once.
    for batch in (5, 6):
        next_batch(batch, "Docs", 30 * batch)
    wait_for(lambda: len(repairs(client, pid)) == 6)
    assert "repair 3 of 3" in next(s.prompt for s in reversed(fake.specs) if "Nothing is taken back" in s.prompt)
    next_batch(7, "Docs", 210)
    told = lambda: [s.prompt for s in fake.specs if s.toolset == "board" and "main of api does not pass its checks (Docs), and 3 repairs have not made it pass" in s.prompt]  # noqa: E731
    wait_for(lambda: idle(client, pid) and len(told()) == 1)
    next_batch(8, "Docs", 240)  # still the same: the manager has it, nobody is told again
    assert len(repairs(client, pid)) == 6 and len(told()) == 1


def test_land_first_with_a_check_command_and_without_any_checks(client, project, lands_on_red):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"], land_first=True)
    land_tick(client)
    # Pushed to the base branch first, then the repository's own check command runs on what landed.
    assert sorted(fake.land_specs[-1].delete_branches) == ["feat-a"] and [c.command for c in fake.check_specs] == ["./ci.sh"]
    fake.jobs[fake.check_specs[-1].id] = JobState(running=False, exit_code=1, log_path="/jobs/check.log", tail="FAILED test_x")
    land_tick(client)
    wait_for(lambda: idle(client, pid) and repairs(client, pid))
    assert "FAILED test_x" in next(s.prompt for s in fake.specs if "Nothing is taken back" in s.prompt)

    # A repository PlainBoard has no checks for can land first too: plain git is enough.
    repo = client.get(f"/api/projects/{pid}/repositories").json()[0]
    assert repo["land_first"] is True
    assert client.patch(f"/api/repositories/{repo['id']}", json={"check_command": ""}).status_code == 200
    hand_over(client, pid, fake, "Late", "feat-z")
    assert "is with PlainBoard now" in runs_of(client, pid)[0]["result_text"]
    land_tick(client)
    wait_for(lambda: idle(client, pid) and "feat-z (Late). This repository has no checks." in last_update(fake))


# --- what the second night showed (2026-11-17) -------------------------------------------------


def landing_row(client, branch: str) -> dict:
    from plainboard.db import sessionmaker
    from plainboard.models import Landing
    from sqlalchemy import select

    async def read() -> dict:
        async with sessionmaker()() as s:
            row = await s.scalar(select(Landing).where(Landing.branch == branch).order_by(Landing.id.desc()).limit(1))
            return {"status": row.status, "failures": row.failures, "agent_id": row.agent_id}

    return client.portal.call(read)


def test_a_conflict_uses_up_no_round_and_a_branch_the_manager_hands_over_keeps_its_author(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b"])
    dev2, manager = agents_by_slug(client, pid)["dev2"], manager_of(client, pid)
    fake.conflicting = {"feat-b"}
    land_tick(client)
    wait_for(lambda: idle(client, pid) and assignments(client, pid))
    assert assignments(client, pid)[0]["title"] == "Resolve the conflicts of branch feat-b" and landing_row(client, "feat-b")["failures"] == 0
    finish_check(client, fake)  # feat-a lands; feat-b comes back unchanged, so it is not merged again: the manager decides
    wait_for(lambda: idle(client, pid) and "feat-b (Dev2) still fails, and nothing new was pushed since" in last_update(fake))

    # The manager hands it over again. It does not become the author: the next conflict goes to Dev2 again.
    chat(client, manager["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-b"}))
    wait_for(lambda: idle(client, pid))
    land_tick(client)
    wait_for(lambda: idle(client, pid) and len(assignments(client, pid)) == 2)
    assert {a["agent_id"] for a in assignments(client, pid)} == {dev2["id"]} and landing_row(client, "feat-b") == {"status": "back", "failures": 0, "agent_id": dev2["id"]}

    # A branch only the manager ever handed over has nobody to give it back to: the manager hears that, and gets no assignment.
    fake.branches["old-work"] = "sha-old"
    fake.conflicting.add("old-work")
    chat(client, manager["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "old-work"}))
    wait_for(lambda: idle(client, pid))
    land_tick(client)
    wait_for(lambda: idle(client, pid) and "old-work (Manager) conflicts with main" in last_update(fake))
    assert "it has no author who could repair it" in last_update(fake) and len(assignments(client, pid)) == 2


def test_a_repair_may_hand_over_a_branch_of_its_own_that_carries_the_trains_branches(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b"])
    land_tick(client)
    # The member that repairs the train merges both into a new branch and hands that one over (scripted through the check's output).
    fake.branches["merge/both"] = "sha-merge"
    carrier = "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "merge/both"})
    fake.jobs[fake.check_specs[-1].id] = JobState(running=False, exit_code=1, log_path="/jobs/check.log", tail=carrier)
    land_tick(client)
    wait_for(lambda: idle(client, pid) and assignments(client, pid) and assignments(client, pid)[0]["status"] == "done")
    assert "It carries feat-a, feat-b: they land with it" in tool_results(client, agents_by_slug(client, pid)["dev1"]["id"])[-1]
    assert landing_row(client, "feat-a")["status"] == "closed"

    # That is a repair: the carrier is checked, and nobody is told that the repair changed nothing.
    land_tick(client)
    assert fake.train_specs[-1].members == ["merge/both"] and "changed none of them" not in last_update(fake)
    finish_check(client, fake)
    wait_for(lambda: idle(client, pid) and "Landed on main in api: merge/both carrying feat-a, feat-b (Dev1)" in last_update(fake))


def test_merged_branches_are_removed_once_a_day_where_a_repository_asks_for_it(client, project):
    from plainboard.models import utcnow

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"], url="https://github.com/acme/api.git", check_command="", land_first=True)
    fake.merged_branches = ["old-1", "old-2", "feat-a"]
    tick = lambda when: client.portal.call(client.app_ref.state.tidy.tick, when)  # noqa: E731
    now = utcnow()

    # Off unless asked for: nothing is deleted in a repository on PlainBoard's own account.
    tick(now)
    assert not getattr(fake, "tidy_specs", [])
    (repo,) = client.get(f"/api/projects/{pid}/repositories").json()
    assert repo["tidy_branches"] is False
    assert client.patch(f"/api/repositories/{repo['id']}", json={"tidy_branches": True}).json()["tidy_branches"] is True

    # What PlainBoard still has in flight is named, so it stays whatever git says about it.
    tick(now)
    (spec,) = fake.tidy_specs
    assert spec.key == f"p{pid}-r{repo['id']}" and "feat-a" in spec.keep and spec.min_age_hours == 24
    (event,) = [e for e in flow_events(client, pid) if e["kind"] == "branches.tidied"]
    assert event["data"] == {"repository": "api", "deleted": 2, "branches": ["old-1", "old-2"]}

    # Once a day, not with every look.
    tick(now + timedelta(hours=23))
    assert len(fake.tidy_specs) == 1
    tick(now + timedelta(hours=25))
    assert len(fake.tidy_specs) == 2


def test_a_file_a_member_shows_in_its_own_chat_is_named_to_the_manager(client, project):
    """Seen in production: a member put the access data for the user into its own chat, and the manager told the
    user about a "private chat file" it could not point to. The user reads the manager's chat."""
    from plainboard.orchestration.run_manager import RunContext

    pid = project["id"]
    manager = manager_of(client, pid)
    dev = make_member(client, pid, "Dev1")
    fake = runner(client)
    order = {"agent": "dev1", "title": "Set up the test server", "instructions": "HOLD\nSAY set up, the access data is in the chat"}
    chat(client, manager["id"], "TOOL delegate " + json.dumps(order))
    wait_for(lambda: any(r["status"] == "running" and r["agent_id"] == dev["id"] for r in runs_of(client, pid)))
    turn = next(r for r in runs_of(client, pid) if r["status"] == "running" and r["agent_id"] == dev["id"])
    client.app_ref.state.runs._tokens["tok"] = RunContext(run_id=turn["id"], project_id=pid, agent_id=dev["id"], token="tok")
    sent = client.post(f"/api/internal/runs/{turn['id']}/chat-files", headers={"Authorization": "Bearer tok"}, files=[("files", ("access.txt", b"secret", "text/plain"))]).json()
    assert not sent["is_error"]
    fake.released = True
    # The report's own words: what the member was told is in the manager's first turn already, as the order it gave.
    wait_for(lambda: idle(client, pid) and "showed files in its own chat" in last_update(fake))
    file_id = client.get(f"/api/agents/{dev['id']}/conversation").json()["messages"][-2]["meta"]["attachments"][0]["id"]
    assert f"Dev1 showed files in its own chat, which the user may not have open: access.txt (attachment {file_id})" in last_update(fake)
    assert "show in your chat: send_files attachment_ids" in last_update(fake)


def test_checks_that_cannot_be_read_do_not_stop_landing(client, project, lands_on_red):
    """Seen in production: the token of a project could not read the checks of GitHub. The watcher asked again with
    every look, for hours, without a word, and fifteen finished branches waited behind one batch."""
    from plainboard.models import utcnow

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"], url="https://github.com/acme/api.git", check_command="", land_first=True)
    landed = lambda: [sorted(spec.delete_branches) for spec in fake.land_specs]  # noqa: E731
    now = utcnow()
    land_tick_at(client, now - timedelta(minutes=30))
    hand_over(client, pid, fake, "Dev2", "feat-b")

    # A short failure (GitHub or the runner does not answer) is waited out.
    fake.checks_error = "gh api failed: Resource not accessible by personal access token (HTTP 403)"
    land_tick_at(client, now - timedelta(minutes=25))
    assert landed() == [["feat-a"]]

    # It lasts: the batch counts as unchecked, the next one lands, and the manager hears why, once.
    land_tick_at(client, now - timedelta(minutes=15))
    wait_for(lambda: idle(client, pid) and any("cannot be read from GitHub" in s.prompt for s in fake.specs))
    assert landed() == [["feat-a"], ["feat-b"]]
    said = next(s.prompt for s in fake.specs if "cannot be read from GitHub" in s.prompt)
    assert "Resource not accessible by personal access token" in said and "read access to Actions" in said and "set a check command" in said
    told = len([s for s in fake.specs if "cannot be read from GitHub" in s.prompt])

    # The next batch is not waited for at all, and nobody is told again.
    hand_over(client, pid, fake, "Dev3", "feat-c")
    land_tick_at(client, now - timedelta(minutes=14))
    wait_for(lambda: idle(client, pid))
    assert landed()[-1] == ["feat-c"]
    assert len([s for s in fake.specs if "cannot be read from GitHub" in s.prompt]) == told

    # Readable again (the token got the permission): checked as usual.
    fake.checks_error = ""
    fake.commit_checks_of["train-3"] = [PullRequestCheck(name="Tests", state="pass")]
    land_tick_at(client, now)
    events = [e["kind"] for e in flow_events(client, pid) if e["kind"].startswith("base.")]
    assert events == ["base.unchecked", "base.unchecked", "base.green"]


def test_pausing_a_project_does_not_wait_for_the_runner(client, project):
    """Telling the turns to wrap up goes to the runner (an external one through its tunnel): the switch does not wait for it."""
    pid = project["id"]
    manager = manager_of(client, pid)
    fake = runner(client)
    gate: list[bool] = []
    told = fake.notify_run

    async def slow_notify(run_id: int, text: str, urgent: bool = False) -> bool:
        while not gate:
            await asyncio.sleep(0.01)
        return await told(run_id, text, urgent)

    fake.notify_run = slow_notify
    chat(client, manager["id"], "HOLD\nSAY wrapped up")
    wait_for(lambda: any(r["status"] == "running" for r in runs_of(client, pid)))
    assert client.post(f"/api/projects/{pid}/pause").json()["paused_at"]
    assert not fake.notices
    gate.append(True)
    wait_for(lambda: any("paused this project" in text for _, text in fake.notices))
    fake.released = True
    wait_for(lambda: idle(client, pid))
    assert client.delete(f"/api/projects/{pid}/pause").json()["paused_at"] is None


def test_a_paused_project_starts_nothing_until_a_person_resumes_it(client, project):
    from datetime import datetime

    pid = project["id"]
    other = client.post("/api/projects", json={"name": "Other"}).json()["id"]
    manager = manager_of(client, pid)
    dev = make_member(client, pid, "Dev1")
    fake = runner(client)
    turns = lambda: len(fake.specs)  # noqa: E731

    # A turn that runs is told to wrap up, and the job of an agent without a turn is stopped.
    chat(client, dev["id"], 'TOOL start_job {"command": "npm run dev"}')
    wait_for(lambda: idle(client, pid) and agents_by_slug(client, pid)["dev1"]["running_jobs"] == 1)
    chat(client, manager["id"], "HOLD\nSAY wrapped up")
    wait_for(lambda: any(r["status"] == "running" for r in runs_of(client, pid)))
    paused = client.post(f"/api/projects/{pid}/pause").json()
    assert paused["paused_at"] and paused["paused_by"]
    wait_for(lambda: any("You are being paused" in text and "paused this project" in text for _, text in fake.notices))
    wait_for(lambda: agents_by_slug(client, pid)["dev1"]["running_jobs"] == 0)
    fake.released = True
    wait_for(lambda: idle(client, pid))
    before = turns()

    # Whoever asks: a chat, a hand-over to the manager, a schedule, the end of "pause everything". Nothing starts.
    chat(client, manager["id"], "SAY one")
    chat(client, dev["id"], "SAY two")
    when = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    assert client.post(f"/api/projects/{pid}/schedules", json={"agent_id": dev["id"], "name": "Nightly", "prompt": "SAY nightly", "kind": "once", "run_at": when}).status_code == 201
    client.portal.call(client.app_ref.state.scheduler.tick)
    assert client.post("/api/pause").json()["paused"]
    assert client.delete("/api/pause").json()["paused"] is False
    client.portal.call(client.app_ref.state.standstill.tick)
    # Another project works on meanwhile.
    chat(client, manager_of(client, other)["id"], "SAY elsewhere")
    wait_for(lambda: idle(client, other) and turns() == before + 1)
    waiting = [r["status"] for r in runs_of(client, pid) if r["status"] in ("queued", "running")]
    assert waiting and set(waiting) == {"queued"}  # ("pause everything" drops what a member had not started: its own rule)
    assert not any("SAY one" in s.prompt or "SAY two" in s.prompt for s in fake.specs)
    assert client.get(f"/api/projects/{pid}").json()["paused_at"]

    # A person resumes it: what waited starts. The schedule's time passed without a turn.
    assert client.delete(f"/api/projects/{pid}/pause").json()["paused_at"] is None
    wait_for(lambda: idle(client, pid) and any("SAY one" in s.prompt for s in fake.specs))
    assert not any("SAY nightly" in s.prompt for s in fake.specs)
    kinds = [e["kind"] for e in flow_events(client, pid) if e["kind"].startswith("project.")]
    assert kinds == ["project.paused", "project.resumed"]


def test_a_conflict_costs_its_author_one_turn_not_one_per_train(client, project):
    """Seen in production: 44 of 99 finished branches came back for conflicts in one night, one of them seven
    times in an hour. Twice its author could do nothing, because the conflict was with a branch on the same
    train that had not landed yet. And each time it had merged the lock file, another branch that changed
    the lock file had landed in between."""
    from plainboard.models import utcnow

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b"])
    dev2 = agents_by_slug(client, pid)["dev2"]
    kinds = lambda: [e["kind"] for e in flow_events(client, pid) if e["kind"] in ("landing.waits", "landing.held", "landing.back")]  # noqa: E731

    def check_passes() -> None:  # like finish_check, while a member is in the middle of a turn
        fake.jobs[fake.check_specs[-1].id] = JobState(running=False, exit_code=0, log_path="/jobs/check.log", tail="all passed")
        land_tick(client)

    def finished(name: str, branch: str, files: set[str]) -> None:  # like hand_over, while a member is in the middle of a turn
        member = make_member(client, pid, name)
        fake.branches[branch], fake.touches[branch] = f"sha-{branch}", files
        chat(client, member["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": branch}))
        wait_for(lambda: any(r["agent_id"] == member["id"] and r["status"] == "succeeded" for r in runs_of(client, pid)))

    # feat-b merges onto main, but not with feat-a, which goes before it. Its author could resolve nothing
    # yet: no assignment. It waits for the next train.
    fake.behind, fake.touches["feat-b"] = {"feat-b"}, {"package-lock.json"}
    land_tick(client)
    assert fake.train_results[-1].merged == {"feat-a": "sha-feat-a"} and fake.train_results[-1].waits == ["feat-b"]
    assert not assignments(client, pid) and landing_row(client, "feat-b")["status"] == "open" and kinds() == ["landing.waits"]

    # feat-a lands, so feat-b conflicts with main: that is its author's, with the file named. The author is
    # busy, so the resolving takes a while.
    fake.behind, fake.conflicting = set(), {"feat-b"}
    chat(client, dev2["id"], "HOLD\nSAY busy")
    wait_for(lambda: any(r["status"] == "running" for r in runs_of(client, pid)))
    check_passes()
    (order,) = assignments(client, pid)
    assert order["title"] == "Resolve the conflicts of branch feat-b" and "It conflicts in: package-lock.json." in order["instructions"]
    assert "the other finished branches that change these files wait for you" in order["instructions"]

    # Meanwhile two more branches are finished. The one that changes the lock file waits: landing it would put
    # feat-b in conflict again the moment its author is done. The other one goes.
    finished("Dev3", "feat-c", {"package-lock.json", "src/app.ts"})
    finished("Dev4", "feat-d", {"README.md"})
    land_tick(client)
    assert fake.train_specs[-1].busy_files == ["package-lock.json"]
    assert fake.train_results[-1].held == ["feat-c"] and fake.train_results[-1].merged == {"feat-d": "sha-feat-d"}
    assert len(assignments(client, pid)) == 1 and kinds() == ["landing.waits", "landing.back", "landing.held"]
    check_passes()
    trains = len(fake.train_specs)
    land_tick(client)
    land_tick(client)
    assert len(fake.train_specs) == trains and kinds().count("landing.held") == 1  # not tried again while nothing changed

    # The author is done: feat-b goes first, and what waited for it goes with it.
    fake.conflicting, fake.branches["feat-b"] = set(), "sha-feat-b-2"
    fake.released = True
    wait_for(lambda: idle(client, pid) and assignments(client, pid)[0]["status"] == "done")
    land_tick(client)
    assert fake.train_specs[-1].members == ["feat-b", "feat-c"] and fake.train_specs[-1].busy_files == []
    assert fake.train_results[-1].merged == {"feat-b": "sha-feat-b-2", "feat-c": "sha-feat-c"}
    finish_check(client, fake)
    assert len(assignments(client, pid)) == 1  # one conflict, one turn

    # An author that does not come back holds nobody for long.
    finished("Dev5", "feat-e", {"styles.css"})
    finished("Dev6", "feat-f", {"styles.css"})
    fake.conflicting = {"feat-e"}
    fake.released = False
    chat(client, agents_by_slug(client, pid)["dev5"]["id"], "HOLD\nSAY busy")
    wait_for(lambda: any(r["status"] == "running" for r in runs_of(client, pid)))
    now = utcnow()
    land_tick_at(client, now)
    # Both were ready together: feat-e conflicts and goes to its author. feat-f, after it, merged in the same go;
    # the next one that changes styles.css is held.
    finished("Dev7", "feat-g", {"styles.css"})
    fake.jobs[fake.check_specs[-1].id] = JobState(running=False, exit_code=0, log_path="/jobs/check.log", tail="all passed")
    land_tick_at(client, now + timedelta(minutes=1))
    assert fake.train_results[-1].held == ["feat-g"]
    land_tick_at(client, now + timedelta(minutes=21))
    assert fake.train_specs[-1].busy_files == [] and fake.train_results[-1].merged == {"feat-g": "sha-feat-g"}
    fake.released = True
    wait_for(lambda: idle(client, pid))


def test_a_status_schedule_spends_no_turn_on_nothing_new(client, project):
    """Seen in production: a status report every two hours said "nothing new" through a quiet morning, a turn
    each. And a project from before the language setting answered its user in English after a night of notices."""
    from plainboard.db import sessionmaker
    from plainboard.models import Schedule, utcnow

    pid = project["id"]
    manager = manager_of(client, pid)
    fake = runner(client)
    scheduler = client.app_ref.state.scheduler

    def due(schedule_id: int) -> None:  # its time has come again
        async def set_due() -> None:
            async with sessionmaker()() as s:
                (await s.get(Schedule, schedule_id)).next_run_at = utcnow() - timedelta(minutes=1)
                await s.commit()

        client.portal.call(set_due)
        client.portal.call(scheduler.tick)
        wait_for(lambda: idle(client, pid))

    def ran_at(schedule_id: int):  # as stored: the API's seconds are too coarse for a test that takes less than one
        async def read():
            async with sessionmaker()() as s:
                return (await s.get(Schedule, schedule_id)).last_run_at

        return client.portal.call(read)

    body = {"agent_id": manager["id"], "name": "Status", "prompt": "SAY status", "kind": "cron", "cron": "0 */2 * * *", "skip_quiet": True}
    status = client.post(f"/api/projects/{pid}/schedules", json=body).json()
    assert status["skip_quiet"] is True
    said = lambda: len([s for s in fake.specs if "SAY status" in s.prompt])  # noqa: E731

    due(status["id"])
    wait_for(lambda: idle(client, pid) and said() == 1)  # its first run: there is no "since the last one" yet
    # No language is set: the manager is asked to set it, once, with the call that ends the asking.
    assert "No language is set for this project" in fake.specs[-1].prompt and "update_project language" in fake.specs[-1].prompt
    last = ran_at(status["id"])

    due(status["id"])
    assert said() == 1  # nothing happened since: no turn
    row = next(row for row in client.get(f"/api/projects/{pid}/schedules").json() if row["id"] == status["id"])
    assert ran_at(status["id"]) == last and row["next_run_at"] > row["last_run_at"]  # its next time is set; the last run is the one that happened
    due(status["id"])
    assert said() == 1  # still nothing, and the note in the work log about the run left out is not "something"
    assert [e["kind"] for e in flow_events(client, pid) if e["kind"] == "schedule.skipped"] == ["schedule.skipped"] * 2

    # Something happens: a person writes to the manager, who sets the language. The next run takes place.
    chat(client, manager["id"], 'TOOL update_project {"language": "German"}')
    wait_for(lambda: idle(client, pid))
    due(status["id"])
    assert said() == 2 and "No language is set" not in fake.specs[-1].prompt
    assert ran_at(status["id"]) > last

    # Without the setting a schedule runs whatever happened.
    client.patch(f"/api/projects/{pid}/schedules/{status['id']}", json={"skip_quiet": False})
    due(status["id"])
    due(status["id"])
    assert said() == 4


def test_a_ticket_that_is_done_does_not_keep_open_points_from_before(client, project):
    """Seen in production: a ticket on done still said "Open: not yet on main", from the last rewrite of its
    state before its branch landed. Whoever reopens it must not start that work again."""
    pid = project["id"]
    make_member(client, pid)
    manager = manager_of(client, pid)
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Support tickets", "description": "With e-mail", "status": "in_progress"}).json()
    fake = runner(client)
    scheduler = client.app_ref.state.scheduler

    def order(title: str, report: str) -> None:
        chat(client, manager["id"], delegate("dev", title, f"SAY {report}", ticket_number=1))
        wait_for(lambda: idle(client, pid) and all(a["status"] == "done" for a in assignments(client, pid)))

    for title in ("Build", "Review fixes", "Screenshots"):
        order(title, f"{title}: done on feat/support, not yet on main")
    wait_for(lambda: client.get(f"/api/tasks/{ticket['id']}").json()["state"] == "SUMMARY #1")
    order("Lock file", "merged origin/main, pushed")  # one more report: not enough for a rewrite
    client.portal.call(scheduler.tick)
    assert len(fake.completions) == 1  # the ticket is still worked on: nothing to bring up to date

    # It is moved to done: the state is written once more, with the report it lacked and with what
    # PlainBoard knows about the ticket being finished.
    assert client.patch(f"/api/tasks/{ticket['id']}", json={"status": "done"}).status_code == 200
    client.portal.call(scheduler.tick)
    wait_for(lambda: client.get(f"/api/tasks/{ticket['id']}").json()["state"] == "SUMMARY #2")
    closing = fake.completions[-1]
    assert 'call the first part "Left open:" instead of "Open:"' in closing.system_prompt
    assert "State so far:\nSUMMARY #1" in closing.prompt and "Assignment #4 (Dev, done): Lock file" in closing.prompt and "Assignment #3 " not in closing.prompt
    assert "The ticket is closed: it was moved to done on " in closing.prompt and "landed on the base branch: none recorded." in closing.prompt
    client.portal.call(scheduler.tick)
    client.portal.call(scheduler.tick)
    assert len(fake.completions) == 2  # once
    written = [e for e in flow_events(client, pid) if e["kind"] == "ticket.state_written"]
    assert [e["data"]["closed"] for e in written] == [False, True]

    # Reopened, the next piece of work starts from that state.
    client.patch(f"/api/tasks/{ticket['id']}", json={"status": "in_progress"})
    order("One more thing", "done")
    prompt = next(s.prompt for s in reversed(fake.specs) if "Assignment #5 from Manager" in s.prompt)
    assert "SUMMARY #2" in prompt
    # Moved to done again with a report it lacks: written again, with no report at all it would be too.
    client.patch(f"/api/tasks/{ticket['id']}", json={"status": "done"})
    client.portal.call(scheduler.tick)
    wait_for(lambda: client.get(f"/api/tasks/{ticket['id']}").json()["state"] == "SUMMARY #3")
    assert "Assignment #5 (Dev, done): One more thing" in fake.completions[-1].prompt
