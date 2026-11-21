import json

import pytest
from datetime import datetime, timedelta

from conftest import FakeRunner, agents_by_slug, idle, runs_of, wait_for
from plainboard_protocol import JobState, LimitWindow


def chat(client, agent_id: int, text: str) -> dict:
    resp = client.post(f"/api/agents/{agent_id}/messages", json={"text": text})
    assert resp.status_code == 202, resp.text
    return resp.json()


def conversation(client, agent_id: int) -> dict:
    return client.get(f"/api/agents/{agent_id}/conversation").json()


def make_dev(client, pid: int, **extra) -> dict:
    body = {"name": "Dev", "role": "Developer", "description": "Backend API", **extra}
    resp = client.post(f"/api/projects/{pid}/agents", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def runner(client) -> FakeRunner:
    return client.app_ref.state.runner


def test_chat_run_persists_messages_and_usage(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, manager["id"], "SAY hello there")
    wait_for(lambda: idle(client, pid))
    conv = conversation(client, manager["id"])
    kinds = [(m["role"], m["kind"]) for m in conv["messages"]]
    assert kinds == [("user", "text"), ("assistant", "text")]
    assert conv["messages"][1]["content"] == "hello there"
    assert conv["provider_session_id"] and conv["context_tokens"] == 1000
    spec = runner(client).specs[-1]
    assert spec.toolset == "board" and "delegate" in {t.name for t in spec.bridge.tools}
    assert "full shell access" in spec.system_prompt and "Small things you do yourself" in spec.system_prompt
    assert "order no tests, screenshots" in spec.system_prompt and "Time here is the opposite" in spec.system_prompt
    assert spec.workspace.shared is False
    assert "Current time:" in spec.prompt


def test_manager_setup_script_reaches_the_runner(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, manager["id"], 'TOOL update_project {"setup_script": "command -v cargo || install-rust"}\nTOOL get_project')
    wait_for(lambda: idle(client, pid))
    tool_msg = [m for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"][-1]
    assert "command -v cargo || install-rust" in tool_msg["meta"]["result"]
    assert "never the user's" in runner(client).specs[-1].system_prompt
    dev = make_dev(client, pid)
    chat(client, dev["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    spec = runner(client).specs[-1]
    assert spec.workspace.setup_script == "command -v cargo || install-rust"
    assert "install it yourself" in spec.system_prompt.replace("\n", " ")


def interrupted_assignment(client, pid: int, dev: dict, manager: dict, ticket: dict, cut_off_before: int = 0, worked_minutes: int = 0) -> None:
    """An assignment whose turn was running when the app stopped, then the restart.
    ``worked_minutes``: how long each earlier turn had worked before it was cut off."""
    from datetime import timedelta

    from plainboard.db import sessionmaker
    from plainboard.models import Assignment, Run, utcnow

    async def interrupt_and_restart():
        async with sessionmaker()() as s:
            assignment = Assignment(project_id=pid, task_id=ticket["id"], agent_id=dev["id"], created_by_agent_id=manager["id"], title="Do the work", instructions="x", status="running")
            s.add(assignment)
            await s.commit()
            for _ in range(cut_off_before):
                times = {"started_at": utcnow() - timedelta(minutes=worked_minutes + 60), "finished_at": utcnow() - timedelta(minutes=60)} if worked_minutes else {}
                s.add(Run(project_id=pid, agent_id=dev["id"], trigger="wake", input_text="", status="failed", error="Interrupted by a restart", assignment_id=assignment.id, **times))
            s.add(Run(project_id=pid, agent_id=dev["id"], trigger="assignment", input_text="x", status="running", assignment_id=assignment.id, task_id=ticket["id"]))
            await s.commit()
        await client.app_ref.state.runs._recover_interrupted()

    client.portal.call(interrupt_and_restart)


def test_an_assignment_interrupted_by_a_restart_goes_on_by_itself(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Build it", "status": "in_progress"}).json()
    interrupted_assignment(client, pid, dev, manager, ticket)
    wait_for(lambda: idle(client, pid) and client.get(f"/api/tasks/{ticket['id']}").json()["assignments"][0]["status"] == "done")
    # Its agent continued in its own turn: no manager turn and no new assignment were needed for that.
    resumed = next(s for s in runner(client).specs if "PlainBoard was restarted while you were working" in s.prompt)
    assert 'Go on with assignment #1 "Do the work"' in resumed.prompt and "delegate" not in {t.name for t in resumed.bridge.tools}
    assert len(client.get(f"/api/tasks/{ticket['id']}").json()["assignments"]) == 1
    assert not any("interrupted by a restart" in s.prompt for s in runner(client).specs)


def test_an_assignment_that_restarts_keep_cutting_off_is_reported_to_the_manager(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Build it", "status": "in_progress"}).json()
    interrupted_assignment(client, pid, dev, manager, ticket, cut_off_before=2)
    wait_for(lambda: idle(client, pid))
    spec = runner(client).specs[-1]
    assert spec.bridge.tools and "Do the work" in spec.prompt and "interrupted by a restart" in spec.prompt
    assert "delegate" in {t.name for t in spec.bridge.tools}  # it went to the manager
    detail = client.get(f"/api/tasks/{ticket['id']}").json()
    assert detail["assignments"][0]["status"] == "failed" and "restart" in detail["assignments"][0]["result"]
    assert detail["comments"] == []  # the assignment on the ticket says it


def test_many_updates_do_not_fail_an_assignment_whose_turns_had_time_to_work(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Build it", "status": "in_progress"}).json()
    # A day with many updates: four turns of a long assignment were cut off, each after 40 minutes of work.
    interrupted_assignment(client, pid, dev, manager, ticket, cut_off_before=4, worked_minutes=40)
    wait_for(lambda: idle(client, pid) and client.get(f"/api/tasks/{ticket['id']}").json()["assignments"][0]["status"] == "done")
    assert any("PlainBoard was restarted while you were working" in s.prompt for s in runner(client).specs)


def test_interrupted_manager_turn_is_reported_to_itself(client, project):
    from plainboard.db import sessionmaker
    from plainboard.models import Run

    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]

    async def interrupt_and_restart():
        async with sessionmaker()() as s:
            s.add(Run(project_id=pid, agent_id=manager["id"], trigger="chat", input_text="Plan the release", status="running"))
            await s.commit()
        await client.app_ref.state.runs._recover_interrupted()

    client.portal.call(interrupt_and_restart)
    wait_for(lambda: idle(client, pid))
    assert "interrupted by a restart" in runner(client).specs[-1].prompt and "Plan the release" in runner(client).specs[-1].prompt


def test_delegating_moves_the_ticket_and_the_manager_is_reminded_when_work_ends(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Build it", "status": "todo"}).json()
    chat(client, manager["id"], f'TOOL delegate {json.dumps({"agent": dev["slug"], "title": "Work", "instructions": "SAY finished", "ticket_number": ticket["number"]})}')
    # The wake-up after the report.
    wait_for(lambda: idle(client, pid) and any("Board check" in spec.prompt and f"#{ticket['number']} Build it" in spec.prompt for spec in runner(client).specs))
    assert client.get(f"/api/tasks/{ticket['id']}").json()["status"] == "in_progress"

    client.patch(f"/api/tasks/{ticket['id']}", json={"user_action": "Test on a phone"})
    chat(client, manager["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    assert "Board check" not in runner(client).specs[-1].prompt


def test_board_check_skips_tickets_being_worked_on_and_reaches_only_the_manager(client, project):
    from plainboard.db import sessionmaker
    from plainboard.models import Decision, Run
    from plainboard.services import board as board_service

    pid = project["id"]
    dev = make_dev(client, pid)
    idle_one, running, deciding = (client.post(f"/api/projects/{pid}/tasks", json={"title": t, "status": "in_progress"}).json() for t in ("A", "B", "C"))

    async def numbers():
        async with sessionmaker()() as s:
            s.add(Run(project_id=pid, agent_id=dev["id"], trigger="assignment", input_text="x", status="queued", task_id=running["id"]))
            s.add(Decision(project_id=pid, task_id=deciding["id"], question="Which?"))
            await s.flush()
            found = [t.number for t in await board_service.idle_in_progress(s, pid)]
            await s.rollback()
            return found

    assert client.portal.call(numbers) == [idle_one["number"]]
    chat(client, dev["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    assert "Board check" not in runner(client).specs[-1].prompt


def test_review_is_only_for_what_the_user_has_to_look_at(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Login", "status": "todo"}).json()
    number = ticket["number"]
    results = lambda: [m["meta"].get("result") for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"]  # noqa: E731

    chat(client, manager["id"], f'TOOL update_task {json.dumps({"number": number, "status": "review"})}')
    wait_for(lambda: idle(client, pid) and results() and results()[-1])
    assert "set user_action with what to check" in results()[-1]
    chat(client, manager["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    assert f"In review without a note for the user: #{number}." in runner(client).specs[-1].prompt

    chat(client, manager["id"], f'TOOL update_task {json.dumps({"number": number, "status": "review", "user_action": "Try the login"})}')
    wait_for(lambda: idle(client, pid) and len(results()) == 2 and results()[-1])
    assert "set user_action" not in results()[-1]
    chat(client, manager["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    assert "In review without" not in runner(client).specs[-1].prompt


def test_manager_always_sees_the_use_of_parallel_agents(client, project):
    pid = project["id"]
    make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, manager["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    prompt = runner(client).specs[-1].prompt
    assert "Parallel members: 0 of 3 working, 3 free. Idle: Dev." in prompt and "delegate it in this turn" not in prompt  # no open ticket
    assert "In flight: nothing." in prompt

    client.post(f"/api/projects/{pid}/tasks", json={"title": "Build it", "status": "todo"})
    chat(client, manager["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    assert "Parallel members: 0 of 3 working, 3 free. Idle: Dev. If work is ready for them, delegate it in this turn." in runner(client).specs[-1].prompt
    assert "Open tasks per member: Dev 0." in runner(client).specs[-1].prompt


def test_manager_is_told_when_delegating_reaches_the_limit(client, project):
    pid = project["id"]
    dev, qa, ops = make_dev(client, pid), make_dev(client, pid, name="QA"), make_dev(client, pid, name="Ops")
    manager = agents_by_slug(client, pid)["manager"]
    client.put("/api/settings", json={"values": {"max_parallel_runs": 2}})
    chat(client, dev["id"], "HOLD")
    wait_for(lambda: any(r["status"] == "running" for r in runs_of(client, pid)))

    order = lambda agent: "TOOL delegate " + json.dumps({"agent": agent["slug"], "title": "Work", "instructions": "SAY done"})
    chat(client, manager["id"], chr(10).join(order(a) for a in (dev, qa, ops)))
    wait_for(lambda: len([m for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool" and "result" in m["meta"]]) == 3)
    results = [m["meta"]["result"] for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"]
    assert "It starts after dev's current work (1 of 2 parallel agents in use)." in results[0]
    assert "2 of 2 parallel agents in use. That is the limit" in results[1]
    assert "ops has to wait for a free slot: 2 of 2 parallel agents in use." in results[2]

    runner(client).released = True
    wait_for(lambda: idle(client, pid))


def test_capacity_line_says_when_the_limit_is_reached():
    from plainboard.orchestration.prompts import capacity_line
    from plainboard.services.board import Capacity

    full = capacity_line(Capacity(limit=2, busy=3, waiting=["Ops"], idle=["QA"], open_work=True))
    assert full.startswith("Parallel members: 2 of 2 working: the limit is reached. 1 more waiting for a free slot (Ops). Don't start further members now.")
    assert "Idle" not in full
    assert capacity_line(Capacity(limit=0, busy=0, waiting=[], idle=["QA"], open_work=True)).startswith("Parallel members: paused by the user")


def test_worker_tools_are_restricted(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    chat(client, dev["id"], 'TOOL create_agent {"name": "X", "role": "r", "description": "d"}')
    wait_for(lambda: idle(client, pid))
    tool_msg = next(m for m in conversation(client, dev["id"])["messages"] if m["kind"] == "tool")
    assert tool_msg["meta"]["is_error"] and "not available" in tool_msg["meta"]["result"]
    names = {t.name for t in runner(client).specs[-1].bridge.tools}
    assert "delegate" not in names and "update_task" not in names and "ask_agent" in names
    assert len(agents_by_slug(client, pid)) == 2


def test_ticket_handoff_and_delegation_flow(client, project):
    pid = project["id"]
    make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Build the API", "description": "REST endpoints", "status": "todo"}).json()

    # Moving the ticket to In Progress hands it to the manager.
    client.patch(f"/api/tasks/{ticket['id']}", json={"status": "in_progress"})
    wait_for(lambda: idle(client, pid))
    handoff = next(r for r in runs_of(client, pid) if r["trigger"] == "ticket")
    assert handoff["task_id"] == ticket["id"] and "Build the API" in handoff["input_text"]

    # The manager delegates a clear assignment; the worker's report comes back.
    args = {"agent": "dev", "title": "Implement endpoints", "instructions": "SAY API done on branch feat/api", "ticket_number": 1}
    chat(client, manager["id"], "TOOL delegate " + json.dumps(args))
    wait_for(lambda: any(r["trigger"] == "wake" and r["status"] == "succeeded" for r in runs_of(client, pid)))

    (assignment,) = client.get(f"/api/projects/{pid}/assignments").json()
    assert assignment["result"] == "API done on branch feat/api" and assignment["task_id"] == ticket["id"]
    detail = client.get(f"/api/tasks/{ticket['id']}").json()
    assert detail["assignments"][0]["status"] == "done"
    assert detail["comments"] == []  # the report stays with its assignment: it is not repeated as a comment

    # Manager was woken up with the report in its inbox.
    wake_msg = next(m for m in conversation(client, manager["id"])["messages"] if m["meta"].get("trigger") == "wake")
    assert "Assignment #1" in wake_msg["meta"]["inbox"][0]


def test_effort_per_assignment(client, project):
    pid = project["id"]
    make_dev(client, pid, effort="low")
    manager = agents_by_slug(client, pid)["manager"]
    args = {"agent": "dev", "title": "Hard bug", "instructions": "SAY fixed", "effort": "high"}
    chat(client, manager["id"], "TOOL delegate " + json.dumps(args))
    wait_for(lambda: any(r["trigger"] == "wake" and r["status"] == "succeeded" for r in runs_of(client, pid)))
    (assignment,) = client.get(f"/api/projects/{pid}/assignments").json()
    assert assignment["effort"] == "high"
    dev_specs = [s for s in runner(client).specs if s.workspace.path.endswith("/dev")]
    assert dev_specs[-1].effort == "high"
    # The agent's own setting is untouched for normal runs.
    dev = agents_by_slug(client, pid)["dev"]
    assert dev["effort"] == "low"
    chat(client, dev["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))
    assert [s for s in runner(client).specs if s.workspace.path.endswith("/dev")][-1].effort == "low"


def test_ask_agent_returns_answer(client, project):
    pid = project["id"]
    make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, manager["id"], 'TOOL ask_agent {"agent": "dev", "question": "SAY 42"}')
    wait_for(lambda: idle(client, pid))
    texts = [m["content"] for m in conversation(client, manager["id"])["messages"] if m["role"] == "assistant" and m["kind"] == "text"]
    assert texts[-1] == "Dev: 42"
    ask_run = next(r for r in runs_of(client, pid) if r["trigger"] == "ask")
    assert ask_run["parent_run_id"] is not None


def test_messages_reach_a_teammate_with_its_next_turn_and_share_it(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, dev["id"], "HOLD")
    wait_for(lambda: any(r["status"] == "running" for r in runs_of(client, pid)))
    both = [json.dumps({"agent": "dev", "message": text}) for text in ("first", "second")]
    chat(client, manager["id"], chr(10).join(f"TOOL message_agent {args}" for args in both))
    wait_for(lambda: any(r["agent_id"] == manager["id"] and r["status"] == "succeeded" for r in runs_of(client, pid)))
    runner(client).released = True
    wait_for(lambda: idle(client, pid) and any(r["trigger"] == "wake" and r["status"] == "succeeded" for r in runs_of(client, pid)))
    # One turn for both messages, and its final text wakes nobody.
    assert sorted(r["trigger"] for r in runs_of(client, pid)) == ["chat", "chat", "wake"]
    prompt = runner(client).specs[-1].prompt
    assert "Message from Manager (Project Manager): first" in prompt and "Message from Manager (Project Manager): second" in prompt
    assert "reaches nobody" in prompt


def test_compression_resets_session_and_carries_memory(client, project):
    pid = project["id"]
    dev = make_dev(client, pid, context_limit_tokens=5000, keep_recent_messages=2, summary_effort="low")
    chat(client, dev["id"], "SAY first answer")  # fresh session: base context 1000
    wait_for(lambda: idle(client, pid))
    assert conversation(client, dev["id"])["session_base_tokens"] == 1000
    chat(client, dev["id"], "SAY grown\nCONTEXT 9000")  # same session grew by 8000
    wait_for(lambda: idle(client, pid) and conversation(client, dev["id"])["compressions"] == 1)  # it compresses after the run has ended
    conv = conversation(client, dev["id"])
    assert conv["compressions"] == 1 and conv["provider_session_id"] is None
    assert conv["messages"][-1]["kind"] == "summary" and conv["messages"][-1]["content"] == "SUMMARY #1"
    assert runner(client).completions[-1].model == "claude-haiku-5-5"
    assert runner(client).completions[-1].effort == "low"  # the agent's own effort for its summaries

    chat(client, dev["id"], "SAY second")
    wait_for(lambda: idle(client, pid))
    spec = runner(client).specs[-1]
    assert spec.resume_session_id is None
    assert "## Memory" in spec.system_prompt and "SUMMARY #1" in spec.system_prompt

    chat(client, dev["id"], "SAY third")
    wait_for(lambda: idle(client, pid))
    assert runner(client).specs[-1].resume_session_id is not None


def test_no_compression_below_base_context(client, project):
    """A limit below the fixed base context must not compress (and reset the session) every turn."""
    pid = project["id"]
    dev = make_dev(client, pid, context_limit_tokens=5000, keep_recent_messages=0)
    for _ in range(2):
        chat(client, dev["id"], "SAY hi\nCONTEXT 9000")
        wait_for(lambda: idle(client, pid))
    conv = conversation(client, dev["id"])
    assert conv["compressions"] == 0 and conv["provider_session_id"] is not None
    assert conv["session_base_tokens"] == 9000


def test_should_compress_rule():
    from plainboard.orchestration.context import should_compress

    assert should_compress(70_000, 18_000, 60_000)
    assert not should_compress(50_000, 18_000, 60_000)  # below limit
    assert not should_compress(20_000, 18_000, 15_000)  # limit below base: needs real growth
    assert should_compress(23_000, 18_000, 15_000)


def test_system_prompt_frozen_per_session(client, project):
    """Briefing changes mid-session arrive as an update; the system prompt (cache prefix) stays identical."""
    pid = project["id"]
    dev = make_dev(client, pid, context_limit_tokens=5000, keep_recent_messages=1)
    chat(client, dev["id"], "SAY one")
    wait_for(lambda: idle(client, pid))
    first = runner(client).specs[-1].system_prompt

    client.post(f"/api/projects/{pid}/notes", json={"title": "Stack", "content": "Use FastAPI", "pinned": True})
    chat(client, dev["id"], "SAY two")
    wait_for(lambda: idle(client, pid))
    spec = runner(client).specs[-1]
    assert spec.resume_session_id and spec.system_prompt == first
    assert spec.prompt.startswith("[Briefing update]") and "Use FastAPI" in spec.prompt and "### Pinned notes" in spec.prompt
    msg = [m for m in conversation(client, dev["id"])["messages"] if m["role"] == "user"][-1]
    assert msg["meta"]["briefing_update"] == ["pinned_notes"]

    chat(client, dev["id"], "SAY three")  # already delivered: no repeated update
    wait_for(lambda: idle(client, pid))
    assert "[Briefing update]" not in runner(client).specs[-1].prompt

    # After compression the new session gets a fresh system prompt with everything current.
    chat(client, dev["id"], "SAY four\nCONTEXT 9000")
    wait_for(lambda: idle(client, pid) and conversation(client, dev["id"])["compressions"] == 1)  # it follows the turn
    chat(client, dev["id"], "SAY five")
    wait_for(lambda: idle(client, pid))
    spec = runner(client).specs[-1]
    assert spec.resume_session_id is None and "Use FastAPI" in spec.system_prompt and "[Briefing update]" not in spec.prompt


def test_provider_compaction_keeps_session_and_resends_briefing(client, project):
    """Updates from before a provider compaction may be lost, so they are sent again; the session stays."""
    pid = project["id"]
    dev = make_dev(client, pid, context_limit_tokens=500_000)
    chat(client, dev["id"], "SAY one")
    wait_for(lambda: idle(client, pid))
    first = runner(client).specs[-1].system_prompt

    client.post(f"/api/projects/{pid}/notes", json={"title": "Stack", "content": "Use FastAPI", "pinned": True})
    chat(client, dev["id"], "SAY two")
    wait_for(lambda: idle(client, pid))
    assert "Use FastAPI" in runner(client).specs[-1].prompt

    chat(client, dev["id"], "SAY three\nCOMPACT 190000")
    wait_for(lambda: idle(client, pid))
    conv = conversation(client, dev["id"])
    assert conv["compressions"] == 1 and conv["provider_session_id"]
    marker = [m for m in conv["messages"] if m["kind"] == "summary"][-1]
    assert marker["meta"]["provider"] and marker["meta"]["tokens_before"] == 190000
    assert not runner(client).completions  # no extra summary with our own model

    chat(client, dev["id"], "SAY four")
    wait_for(lambda: idle(client, pid))
    spec = runner(client).specs[-1]
    assert spec.resume_session_id and spec.system_prompt == first
    assert spec.prompt.startswith("[Briefing update]") and "Use FastAPI" in spec.prompt
    assert "### Team" not in spec.prompt  # only what differs from the frozen prompt

    chat(client, dev["id"], "SAY five")  # delivered again: no repeated update
    wait_for(lambda: idle(client, pid))
    assert "[Briefing update]" not in runner(client).specs[-1].prompt


def test_session_without_frozen_prompt_is_kept(client, project):
    """Sessions created before prompts were frozen continue (no forced new session)."""
    from sqlalchemy import update

    from plainboard.db import sessionmaker
    from plainboard.models import Conversation

    pid = project["id"]
    dev = make_dev(client, pid)
    chat(client, dev["id"], "SAY one")
    wait_for(lambda: idle(client, pid))
    session_id = runner(client).specs[-1].resume_session_id or conversation(client, dev["id"])["provider_session_id"]

    async def forget_snapshot():
        async with sessionmaker()() as s:
            await s.execute(update(Conversation).where(Conversation.agent_id == dev["id"]).values(system_prompt=None, prompt_sections=None))
            await s.commit()

    client.portal.call(forget_snapshot)
    chat(client, dev["id"], "SAY two")
    wait_for(lambda: idle(client, pid))
    assert runner(client).specs[-1].resume_session_id == session_id


def decision_ticket(client, pid: int) -> tuple[dict, int]:
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "New design"}).json()
    upload = client.post(f"/api/tasks/{ticket['id']}/attachments", files={"files": ("a.png", b"png", "image/png")})
    assert upload.status_code == 201, upload.text
    return ticket, upload.json()[0]["id"]


def request_decision(client, pid: int, manager_id: int, number: int, attachment_id: int) -> None:
    args = {
        "task_number": number,
        "question": "Which layout?",
        "options": [{"label": "Compact", "attachment_ids": [attachment_id]}, {"label": "Airy", "description": "More whitespace"}],
    }
    chat(client, manager_id, f"TOOL request_decision {json.dumps(args)}")
    wait_for(lambda: idle(client, pid))


def test_user_decision_answer_reaches_the_manager(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    ticket, attachment_id = decision_ticket(client, pid)
    request_decision(client, pid, manager["id"], ticket["number"], attachment_id)

    detail = client.get(f"/api/tasks/{ticket['id']}").json()
    assert detail["open_decisions"] == 1
    decision = detail["decisions"][0]
    assert decision["status"] == "open" and decision["requested_by_agent_id"] == manager["id"]
    assert decision["options"][0]["attachment_ids"] == [attachment_id]
    board = client.get(f"/api/projects/{pid}/tasks").json()
    assert next(t for t in board if t["id"] == ticket["id"])["open_decisions"] == 1

    chat(client, manager["id"], f'TOOL get_task {{"number": {ticket["number"]}}}')
    wait_for(lambda: idle(client, pid))
    tool_msg = [m for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"][-1]
    assert "a.png" in tool_msg["meta"]["result"] and "(for the user's review)" in tool_msg["meta"]["result"]
    assert f"Decision {decision['id']}: Which layout? (1. Compact; 2. Airy)" in tool_msg["meta"]["result"]

    resp = client.post(f"/api/decisions/{decision['id']}/answer", json={"option_index": 1, "text": "but darker"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "answered" and resp.json()["answered_by"] == "user"
    wait_for(lambda: idle(client, pid))
    spec = runner(client).specs[-1]
    assert 'User decided on ticket #%d ("Which layout?"): Airy — but darker' % ticket["number"] in spec.prompt

    detail = client.get(f"/api/tasks/{ticket['id']}").json()
    assert detail["open_decisions"] == 0
    assert "Airy — but darker" in detail["comments"][-1]["body"] and detail["comments"][-1]["author_kind"] == "user"
    assert client.post(f"/api/decisions/{decision['id']}/answer", json={"option_index": 0}).status_code == 409


def test_decision_answer_needs_content_and_a_known_option(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    ticket, attachment_id = decision_ticket(client, pid)
    request_decision(client, pid, manager["id"], ticket["number"], attachment_id)
    decision = client.get(f"/api/tasks/{ticket['id']}").json()["decisions"][0]
    assert client.post(f"/api/decisions/{decision['id']}/answer", json={"text": "  "}).status_code == 400
    assert client.post(f"/api/decisions/{decision['id']}/answer", json={"option_index": 5}).status_code == 400
    assert client.get(f"/api/tasks/{ticket['id']}").json()["open_decisions"] == 1


def test_manager_records_or_withdraws_decision_from_chat(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    ticket, attachment_id = decision_ticket(client, pid)
    request_decision(client, pid, manager["id"], ticket["number"], attachment_id)
    request_decision(client, pid, manager["id"], ticket["number"], attachment_id)
    first, second = client.get(f"/api/tasks/{ticket['id']}").json()["decisions"]

    chat(client, manager["id"], f'TOOL resolve_decision {{"decision_id": {first["id"]}, "option": 1, "note": "said in chat"}}')
    wait_for(lambda: idle(client, pid))
    chat(client, manager["id"], f'TOOL resolve_decision {{"decision_id": {second["id"]}, "withdraw": true}}')
    wait_for(lambda: idle(client, pid))

    detail = client.get(f"/api/tasks/{ticket['id']}").json()
    assert detail["open_decisions"] == 0
    answered, withdrawn = detail["decisions"]
    assert answered["status"] == "answered" and answered["answered_by"] == "agent" and answered["answer_option"] == 0
    assert withdrawn["status"] == "withdrawn"
    assert "Compact — said in chat" in detail["comments"][-1]["body"]
    # The manager decided this itself: no extra wake-up with the answer.
    assert "User decided" not in runner(client).specs[-1].prompt


def test_decision_rejects_foreign_attachments_and_workers(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    dev = make_dev(client, pid)
    ticket, _ = decision_ticket(client, pid)
    _, foreign_id = decision_ticket(client, pid)
    args = {"task_number": ticket["number"], "question": "Which?", "attachment_ids": [foreign_id]}
    chat(client, manager["id"], f"TOOL request_decision {json.dumps(args)}")
    wait_for(lambda: idle(client, pid))
    tool_msg = [m for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"][-1]
    assert tool_msg["meta"]["is_error"] and str(foreign_id) in tool_msg["meta"]["result"]
    assert client.get(f"/api/tasks/{ticket['id']}").json()["decisions"] == []

    chat(client, dev["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))
    names = {t.name for t in runner(client).specs[-1].bridge.tools}
    assert "request_decision" not in names and "resolve_decision" not in names and "attach_file" in names


def test_queued_run_can_be_cancelled(client, project):
    pid = project["id"]
    client.put("/api/settings", json={"values": {"max_parallel_runs": 0}})
    dev = make_dev(client, pid)
    run = chat(client, dev["id"], "SAY never")
    assert client.post(f"/api/runs/{run['id']}/cancel").json() == {"cancelled": True}
    assert runs_of(client, pid)[0]["status"] == "cancelled"


def test_schedule_fires_once(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    past = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    sched = client.post(
        f"/api/projects/{pid}/schedules",
        json={"name": "Report", "agent_id": manager["id"], "prompt": "SAY weekly report", "kind": "once", "run_at": past},
    ).json()
    assert sched["next_run_at"] is not None
    fired = client.portal.call(client.app_ref.state.scheduler.tick)
    assert fired == 1
    wait_for(lambda: idle(client, pid))
    run = runs_of(client, pid)[0]
    assert run["trigger"] == "schedule" and run["status"] == "succeeded"
    tidy, sched = client.get(f"/api/projects/{pid}/schedules").json()
    assert sched["enabled"] is False and sched["next_run_at"] is None
    # Every project starts with the manager's tidy-up schedule.
    assert (tidy["name"], tidy["cron"], tidy["agent_id"], tidy["enabled"]) == ("Tidy up", "0 4 * * *", manager["id"], True)
    assert tidy["next_run_at"] and "Work carefully" in tidy["prompt"]


def test_cron_schedule_validation(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    bad = client.post(f"/api/projects/{pid}/schedules", json={"name": "x", "agent_id": manager["id"], "prompt": "p", "kind": "cron", "cron": "nope"})
    assert bad.status_code == 400
    good = client.post(
        f"/api/projects/{pid}/schedules", json={"name": "x", "agent_id": manager["id"], "prompt": "p", "kind": "cron", "cron": "0 9 * * 1-5"}
    ).json()
    assert good["next_run_at"] and good["timezone"] == "Europe/Berlin"


def test_manager_tools_manage_notes_schedules_agents(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    lines = [
        'TOOL create_agent {"name": "QA", "role": "Tester", "description": "Testing", "model": "claude-haiku-5-5"}',
        'TOOL create_note {"title": "Stack", "content": "FastAPI + React", "pinned": true}',
        'TOOL create_schedule {"name": "Standup", "prompt": "Summarize progress", "cron": "0 9 * * 1-5"}',
        'TOOL create_tasks {"tickets": [{"title": "Follow-up", "status": "todo"}]}',
        'TOOL update_task {"number": 1, "status": "review"}',
    ]
    chat(client, manager["id"], "\n".join(lines))
    wait_for(lambda: idle(client, pid))
    tool_msgs = [m for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"]
    assert len(tool_msgs) == 5 and not any(m["meta"]["is_error"] for m in tool_msgs), [m["meta"] for m in tool_msgs]
    assert agents_by_slug(client, pid)["qa"]["model"] == "claude-haiku-5-5"
    assert client.get(f"/api/projects/{pid}/notes").json()[0]["title"] == "Stack"
    assert client.get(f"/api/projects/{pid}/schedules").json()[-1]["cron"] == "0 9 * * 1-5"
    assert client.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "review"

    # Pinned notes reach the agent: as a briefing update now, in the system prompt of its next session.
    chat(client, manager["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    assert "FastAPI + React" in runner(client).specs[-1].prompt
    client.post(f"/api/agents/{manager['id']}/reset-session")
    chat(client, manager["id"], "SAY fresh")
    wait_for(lambda: idle(client, pid))
    assert "FastAPI + React" in runner(client).specs[-1].system_prompt


def test_deadlock_detection():
    from plainboard.orchestration.run_manager import RunManager

    rm = RunManager.__new__(RunManager)
    rm._waits_for = {}
    rm._add_wait(1, 2)
    rm._add_wait(2, 3)
    assert rm._would_deadlock(3, 1)
    assert not rm._would_deadlock(1, 3)
    rm._remove_wait(2, 3)
    assert not rm._would_deadlock(3, 1)


def test_tool_schemas_are_compact():
    from plainboard.tools import registry

    def has_title_keyword(node, mapping=False) -> bool:
        if isinstance(node, dict):
            if not mapping and isinstance(node.get("title"), str):
                return True
            return any(has_title_keyword(v, mapping=(k in ("properties", "$defs") and not mapping)) for k, v in node.items())
        if isinstance(node, list):
            return any(has_title_keyword(v) for v in node)
        return False

    for tool in registry.all_tools():
        schema = tool.definition().input_schema
        assert not has_title_keyword(schema), tool.name
        assert '"anyOf"' not in json.dumps(schema) and '"default": null' not in json.dumps(schema), tool.name


def test_chat_message_with_files(client, project):
    manager = agents_by_slug(client, project["id"])["manager"]
    files = [("files", ("shot.png", b"png", "image/png")), ("files", ("notes.txt", b"text", "text/plain"))]
    uploaded = client.post(f"/api/agents/{manager['id']}/attachments", files=files)
    assert uploaded.status_code == 201, uploaded.text
    ids = [a["id"] for a in uploaded.json()]

    run = client.post(f"/api/agents/{manager['id']}/messages", json={"attachment_ids": ids})
    assert run.status_code == 202, run.text
    assert run.json()["attachment_ids"] == ids
    wait_for(lambda: idle(client, project["id"]))

    prompt = runner(client).specs[-1].prompt
    assert "shot.png: /uploads/" in prompt and "notes.txt: /uploads/" in prompt
    assert "Updates since" not in prompt
    message = next(m for m in conversation(client, manager["id"])["messages"] if m["role"] == "user")
    assert message["content"] == ""
    assert [f["filename"] for f in message["meta"]["attachments"]] == ["shot.png", "notes.txt"]
    assert client.get(f"/api/attachments/{ids[0]}").content == b"png"


def test_chat_message_rejects_empty_and_unknown_files(client, project):
    manager = agents_by_slug(client, project["id"])["manager"]
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "  "}).status_code == 422
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "x", "attachment_ids": [999]}).status_code == 400


def test_chat_files_stay_known_in_a_fresh_session(client, project):
    manager = agents_by_slug(client, project["id"])["manager"]
    uploaded = client.post(f"/api/agents/{manager['id']}/attachments", files=[("files", ("shot.png", b"png", "image/png"))]).json()
    client.post(f"/api/agents/{manager['id']}/messages", json={"text": "look", "attachment_ids": [uploaded[0]["id"]]})
    wait_for(lambda: idle(client, project["id"]))
    client.post(f"/api/agents/{manager['id']}/reset-session")
    chat(client, manager["id"], "SAY again")
    wait_for(lambda: idle(client, project["id"]))
    assert "shot.png: /uploads/" in runner(client).specs[-1].prompt


def start_job(client, agent_id: int, command: str = "cargo build") -> None:
    chat(client, agent_id, f"TOOL start_job {json.dumps({'command': command})}")


def job_tick(client) -> None:
    client.portal.call(client.app_ref.state.job_watcher.tick)


def test_pausing_an_agent_waiting_for_a_job_gets_it_a_turn_to_stop(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    start_job(client, dev["id"])
    wait_for(lambda: idle(client, pid))
    chat(client, manager["id"], 'TOOL stop_agent {"agent": "dev", "pause": true, "reason": "release"}')
    wait_for(lambda: idle(client, pid) and any("You are being paused" in spec.prompt for spec in runner(client).specs))
    wait_for(lambda: idle(client, pid))
    run = next(r for r in runs_of(client, pid, agent_id=dev["id"]) if r["trigger"] == "wake")
    assert run["status"] == "succeeded" and run["stop_requested_at"]
    prompt = next(spec.prompt for spec in runner(client).specs if "You are being paused" in spec.prompt)
    assert "cargo build" in prompt and "stop_job" in prompt and "release" in prompt
    assert agents_by_slug(client, pid)["dev"]["enabled"] is False

    resp = client.post(f"/api/agents/{dev['id']}/messages", json={"text": "more work"})
    assert resp.status_code != 202  # still paused for everything else


def test_an_agent_with_only_a_background_job_is_not_shown_as_idle(client, project):
    from plainboard.orchestration.prompts import capacity_line
    from plainboard.services.board import Capacity

    pid = project["id"]
    dev = make_dev(client, pid)
    start_job(client, dev["id"])
    wait_for(lambda: idle(client, pid))
    assert agents_by_slug(client, pid)["dev"]["status"] == "background"

    # A job outside an assignment (e.g. a server) keeps nobody from new work.
    line = capacity_line(Capacity(limit=3, busy=0, waiting=[], idle=["QA", "Dev"], open_work=True))
    assert "Idle: QA, Dev. If work is ready" in line


def test_finished_job_is_reported_to_its_agent_and_wakes_it(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    start_job(client, manager["id"])
    wait_for(lambda: idle(client, pid))
    spec = runner(client).job_specs[-1]
    assert spec.command == "cargo build" and spec.workspace.endswith(f"/{project['slug']}/manager")
    assert agents_by_slug(client, pid)["manager"]["running_jobs"] == 1

    before = len(runner(client).specs)
    job_tick(client)  # still running: nothing happens
    assert len(runner(client).specs) == before and idle(client, pid)

    runner(client).jobs[spec.id] = JobState(running=False, exit_code=2, log_path="/jobs/x.log", tail="error: boom")
    job_tick(client)
    wait_for(lambda: len(runner(client).specs) == before + 1 and idle(client, pid))
    prompt = runner(client).specs[-1].prompt
    assert "cargo build" in prompt and "exit code 2" in prompt and "error: boom" in prompt
    assert agents_by_slug(client, pid)["manager"]["running_jobs"] == 0

    job_tick(client)  # reported once only
    wait_for(lambda: idle(client, pid))
    assert len(runner(client).specs) == before + 1


def test_job_unknown_to_the_runner_is_lost_but_unreachable_runner_decides_nothing(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    start_job(client, manager["id"])
    wait_for(lambda: idle(client, pid))
    before = len(runner(client).specs)
    runner(client).jobs.clear()

    runner(client).reachable = False
    job_tick(client)
    assert agents_by_slug(client, pid)["manager"]["running_jobs"] == 1 and len(runner(client).specs) == before

    runner(client).reachable = True
    job_tick(client)
    wait_for(lambda: len(runner(client).specs) == before + 1 and idle(client, pid))
    assert "was lost" in runner(client).specs[-1].prompt


def test_agents_only_see_and_stop_their_own_jobs(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    start_job(client, manager["id"])
    wait_for(lambda: idle(client, pid))

    chat(client, dev["id"], 'TOOL stop_job {"id": 1}\nTOOL list_jobs')
    wait_for(lambda: idle(client, pid))
    assert agents_by_slug(client, pid)["manager"]["running_jobs"] == 1
    text = runs_of(client, pid)[0]["result_text"]
    assert "no job" in text and "No jobs." in text

    fake = runner(client)
    chat(client, manager["id"], 'TOOL list_jobs\nTOOL stop_job {"id": 1}')
    wait_for(lambda: idle(client, pid))
    text = runs_of(client, pid)[0]["result_text"]
    assert "cargo build" in text and "stopped." in text
    assert agents_by_slug(client, pid)["manager"]["running_jobs"] == 0

    before = len(fake.specs)
    next(iter(fake.jobs.values())).running = False
    job_tick(client)  # a job stopped on purpose is not reported
    wait_for(lambda: idle(client, pid))
    assert len(fake.specs) == before


def test_a_turn_can_wait_for_its_job_and_is_not_woken_for_it_again(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    chat(client, dev["id"], f"TOOL start_job {json.dumps({'command': 'cargo build', 'wait_minutes': 1})}")
    wait_for(lambda: runner(client).job_specs and agents_by_slug(client, pid)["dev"]["running_jobs"] == 1)
    spec = runner(client).job_specs[-1]
    waiting = lambda: bool(client.app_ref.state.runs._job_waiters)  # noqa: E731
    wait_for(waiting)
    runner(client).jobs[spec.id] = JobState(running=False, exit_code=0, log_path="/jobs/x.log", tail="Finished release")
    job_tick(client)
    wait_for(lambda: idle(client, pid))
    (run,) = runs_of(client, pid)  # no wake-up, no update for the manager
    assert "exit code 0" in run["result_text"] and "Finished release" in run["result_text"]

    chat(client, dev["id"], 'TOOL wait_job {"id": 1}')
    wait_for(lambda: idle(client, pid))
    assert "has ended (succeeded, exit code 0)" in runs_of(client, pid)[0]["result_text"]


def test_a_members_turn_outside_an_assignment_reaches_nobody(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    start_job(client, dev["id"])
    wait_for(lambda: idle(client, pid))
    spec = runner(client).job_specs[-1]
    runner(client).jobs[spec.id] = JobState(running=False, exit_code=0, log_path="/jobs/x.log", tail="SAY Build is green")
    job_tick(client)
    wait_for(lambda: idle(client, pid) and len(runs_of(client, pid)) == 2)
    prompt = runner(client).specs[-1].prompt
    assert "exit code 0 after" in prompt and "reaches nobody" in prompt  # the job's duration is said: an agent has no clock
    assert [r["trigger"] for r in runs_of(client, pid)] == ["wake", "chat"]  # the manager is not woken


def test_session_cost_totals_are_not_counted_twice(client, project, monkeypatch):
    manager = agents_by_slug(client, project["id"])["manager"]
    totals = iter([0.10, 0.25])
    original = FakeRunner.stream_run

    async def stream_run(self, spec):
        async for event in original(self, spec):
            if event.type == "result":
                event = event.model_copy(update={"cost_usd": 0.0, "session_cost_usd": next(totals)})
            yield event

    monkeypatch.setattr(FakeRunner, "stream_run", stream_run)
    for _ in range(2):
        chat(client, manager["id"], "SAY hi")
        wait_for(lambda: idle(client, project["id"]))
    costs = [r["cost_usd"] for r in runs_of(client, project["id"])]
    assert sorted(round(c, 2) for c in costs) == [0.10, 0.15]
    assert round(agents_by_slug(client, project["id"])["manager"]["total_cost_usd"], 2) == 0.25


def test_disabling_an_agent_cancels_its_waiting_work(client, project):
    pid = project["id"]
    client.put("/api/settings", json={"values": {"max_parallel_runs": 0}})
    dev = make_dev(client, pid)
    chat(client, dev["id"], "SAY hi")
    assert client.patch(f"/api/agents/{dev['id']}", json={"enabled": False}).status_code == 200
    client.put("/api/settings", json={"values": {"max_parallel_runs": 3}})
    wait_for(lambda: idle(client, pid))
    assert [r["status"] for r in runs_of(client, pid, agent_id=dev["id"])] == ["cancelled"]


def test_manager_pauses_a_teammate_and_drops_its_queue(client, project):
    pid = project["id"]
    client.put("/api/settings", json={"values": {"max_parallel_runs": 1}})
    dev, ops = make_dev(client, pid), make_dev(client, pid, name="Ops")
    manager = agents_by_slug(client, pid)["manager"]
    # Ops takes the only slot, so Dev's turn for the message stays queued. The manager needs no slot.
    chat(client, ops["id"], "HOLD")
    wait_for(lambda: any(r["status"] == "running" for r in runs_of(client, pid)))
    chat(client, manager["id"], 'TOOL message_agent {"agent": "dev", "message": "hi"}\nTOOL stop_agent {"agent": "dev"}\nTOOL stop_agent {"agent": "manager"}')
    wait_for(lambda: any(r["agent_id"] == manager["id"] and r["status"] == "succeeded" for r in runs_of(client, pid)))
    assert [r["status"] for r in runs_of(client, pid, agent_id=dev["id"])] == ["cancelled"]
    assert agents_by_slug(client, pid)["dev"]["enabled"] is False
    tools = [m for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"]
    assert tools[2]["meta"]["is_error"] is True
    runner(client).released = True
    wait_for(lambda: idle(client, pid))


def test_a_paused_agent_keeps_its_assignments_and_goes_on_when_enabled_again(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    first = json.dumps({"agent": "dev", "title": "First", "instructions": "HOLD"})
    second = json.dumps({"agent": "dev", "title": "Second", "instructions": "SAY second done"})
    chat(client, manager["id"], f"TOOL delegate {first}\nTOOL delegate {second}")
    wait_for(lambda: any(r["status"] == "running" for r in runs_of(client, pid, agent_id=dev["id"])))
    assert client.patch(f"/api/agents/{dev['id']}", json={"enabled": False}).status_code == 200
    runner(client).released = True  # it wraps up
    wait_for(lambda: idle(client, pid) or all(r["status"] == "queued" for r in runs_of(client, pid, active=True)))
    states = {a["title"]: a["status"] for a in client.get(f"/api/projects/{pid}/assignments").json()}
    assert states == {"First": "running", "Second": "queued"}  # nothing was closed or dropped, and nobody got a report
    assert [r["trigger"] for r in runs_of(client, pid, agent_id=manager["id"])] == ["chat"]

    assert client.patch(f"/api/agents/{dev['id']}", json={"enabled": True}).status_code == 200
    wait_for(lambda: idle(client, pid) and all(a["status"] == "done" for a in client.get(f"/api/projects/{pid}/assignments").json()))
    assert any("You were paused and are enabled again" in s.prompt and "Go on with assignment #1" in s.prompt for s in runner(client).specs)


def test_a_turn_the_runner_lost_ends_and_its_assignment_goes_on(client, project, monkeypatch):
    from plainboard.orchestration import run_manager

    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    work = json.dumps({"agent": "dev", "title": "Work", "instructions": "HOLD"})
    chat(client, manager["id"], f"TOOL delegate {work}")
    turn = wait_for(lambda: next((r for r in runs_of(client, pid, agent_id=dev["id"]) if r["status"] == "running"), None))
    # Running in the database comes a moment before the runner has it; with no grace below, wait for that.
    wait_for(lambda: turn["id"] in runner(client).active)
    monkeypatch.setattr(run_manager, "LOST_AFTER_SECONDS", 0)
    client.portal.call(client.app_ref.state.job_watcher.tick)
    assert runs_of(client, pid, agent_id=dev["id"])[0]["status"] == "running"  # the runner still has it

    runner(client).forgotten.add(turn["id"])  # its stream broke off, the app still waits
    client.portal.call(client.app_ref.state.job_watcher.tick)
    wait_for(lambda: any(r["trigger"] == "wake" for r in runs_of(client, pid, agent_id=dev["id"])))
    runner(client).released = True  # the turn that goes on repeats the instructions
    wait_for(lambda: idle(client, pid) and client.get(f"/api/projects/{pid}/assignments").json()[0]["status"] == "done")
    lost = next(r for r in runs_of(client, pid, agent_id=dev["id"]) if r["id"] == turn["id"])
    assert lost["status"] == "failed" and lost["error"] == run_manager.LOST
    assert any("Your last turn broke off" in s.prompt and int(s.run_id) != turn["id"] for s in runner(client).specs)


def test_working_teammate_is_told_to_wrap_up_not_aborted(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    # Dev's run stays active while it waits for the manager, who stops it meanwhile.
    question = json.dumps({"agent": "manager", "question": chr(10) + 'TOOL stop_agent {"agent": "dev", "reason": "release"}' + chr(10)})
    chat(client, dev["id"], f"TOOL ask_agent {question}")
    wait_for(lambda: idle(client, pid))
    (run_id, text), = runner(client).notices
    assert run_id == runs_of(client, pid, agent_id=dev["id"])[0]["id"]
    assert "safe point" in text and "release" in text
    run = runs_of(client, pid, agent_id=dev["id"])[0]
    assert run["status"] == "succeeded" and run["stop_requested_at"]
    assert agents_by_slug(client, pid)["dev"]["enabled"] is False


def test_agents_can_read_each_others_chat(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, dev["id"], "SAY the build is green")
    wait_for(lambda: idle(client, pid))
    chat(client, manager["id"], 'TOOL read_chat {"agent": "dev"}')
    wait_for(lambda: idle(client, pid))
    tool = next(m for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool")
    assert "the build is green" in tool["meta"]["result"] and not tool["meta"]["is_error"]


def test_manager_marks_and_sorts_the_board(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    tickets = [client.post(f"/api/projects/{pid}/tasks", json={"title": title, "status": "todo", "labels": ["bug"]}).json() for title in ("A", "B", "C")]
    chat(client, manager["id"], 'TOOL sort_tickets {"status": "todo", "numbers": [3, 2]}\nTOOL color_label {"label": "bug", "color": "red"}\nTOOL update_task {"number": 1, "user_action": "Test it on a phone"}')
    wait_for(lambda: idle(client, pid))
    board = client.get(f"/api/projects/{pid}/tasks").json()
    assert [t["number"] for t in board] == [3, 2, 1]
    assert client.get(f"/api/projects/{pid}").json()["label_colors"] == {"bug": "red"}
    assert next(t for t in board if t["number"] == 1)["user_action"] == "Test it on a phone"

    url = f"/api/tasks/{tickets[0]['id']}/user-action/done"
    assert client.post(url, json={"note": "works"}).json()["user_action"] == ""
    assert client.post(url, json={}).status_code == 400
    assert "Test it on a phone" in client.get(f"/api/tasks/{tickets[0]['id']}").json()["comments"][-1]["body"]
    wait_for(lambda: idle(client, pid))
    assert 'User finished "Test it on a phone" on ticket #1: works' in runner(client).specs[-1].prompt

    # Two clicks that both saw the ticket waiting (the first one was slow): the manager hears it once.
    from plainboard.db import sessionmaker
    from plainboard.models import Task
    from plainboard.services import board
    from plainboard.services.errors import ServiceError

    client.patch(f"/api/tasks/{tickets[0]['id']}", json={"user_action": "Test it again"})

    async def twice() -> list[str]:
        async with sessionmaker()() as a, sessionmaker()() as b:
            first, second = await a.get(Task, tickets[0]["id"]), await b.get(Task, tickets[0]["id"])
            outcome = [await board.finish_user_action(a, first)]
            try:
                outcome.append(await board.finish_user_action(b, second))
            except ServiceError as exc:
                outcome.append(str(exc))
            return outcome

    assert client.portal.call(twice) == ["Test it again", "This ticket is not waiting for you"]
    bodies = [c["body"] for c in client.get(f"/api/tasks/{tickets[0]['id']}").json()["comments"]]
    assert len([body for body in bodies if "Test it again" in body]) == 1


def test_manager_handles_tickets_in_batches(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    tickets = [{"title": f"T{i}", "labels": ["bug"] if i % 2 else []} for i in range(1, 6)]
    chat(
        client,
        manager["id"],
        f"TOOL create_tasks {json.dumps({'tickets': tickets})}\n"
        'TOOL update_tasks {"numbers": [1, 3, 5, 9], "status": "todo", "priority": "high", "add_labels": ["ui"], "remove_labels": ["BUG"]}\n'
        'TOOL list_tasks {"label": "ui", "limit": 2}\n'
        'TOOL list_tasks {"priority": "high", "offset": 5}\n'
        'TOOL update_tasks {"numbers": [2]}\n'
        'TOOL delete_tasks {"numbers": [2, 4]}',
    )
    wait_for(lambda: idle(client, pid))
    said = conversation(client, manager["id"])["messages"][-1]["content"].split(" | ")
    assert said[0] == "Created #1, #2, #3, #4, #5."
    assert said[1] == "Updated #1, #3, #5. Not found: #9."
    assert said[2].endswith("(showing 1-2 of 3)") and said[2].count("[todo] (high)") == 2
    assert said[3] == "No tickets at offset 5 (3 match)."
    assert said[4] == "Nothing to change."
    assert said[5] == "Deleted #2, #4."
    board = client.get(f"/api/projects/{pid}/tasks").json()
    assert [(t["number"], t["status"], t["labels"]) for t in board] == [(1, "todo", ["ui"]), (3, "todo", ["ui"]), (5, "todo", ["ui"])]


def test_workers_cannot_sort_or_color(client, project):
    dev = make_dev(client, project["id"])
    chat(client, dev["id"], "SAY hi")
    wait_for(lambda: idle(client, project["id"]))
    names = {t.name for t in runner(client).specs[-1].bridge.tools}
    assert "sort_tickets" not in names and "color_label" not in names


def test_work_report_shows_where_the_work_went_without_texts(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Secret plan", "status": "todo"}).json()
    chat(client, manager["id"], f'TOOL delegate {json.dumps({"agent": dev["slug"], "title": "Work", "instructions": "SAY finished", "ticket_number": ticket["number"]})}')
    wait_for(lambda: idle(client, pid) and len(runs_of(client, pid)) >= 3)

    resp = client.get(f"/api/projects/{pid}/work-report?days=7")
    assert "attachment" in resp.headers["content-disposition"] and "Secret plan" not in resp.text
    report = resp.json()
    by_name = {a["name"]: a for a in report["agents"]}
    assert by_name["Dev"]["by_trigger"]["assignment"]["runs"] == 1
    assert by_name["Manager"]["tool_calls"]["delegate"] == 1 and by_name["Manager"]["by_trigger"]["wake"]["runs"] >= 1
    (row,) = report["tickets"]
    assert row["ticket"] == ticket["number"] and row["assignments"] == 1 and row["members"]["runs"] == 1
    assert report["without_ticket"]["manager"]["runs"] >= 1 and report["assignments"]["by_status"] == {"done": 1}
    # Per day and per account, to compare before and after a change; work against waiting.
    (today,) = [d for d in report["per_day"] if d["runs"]]
    assert today["by_trigger"]["assignment"] == 1 and {"cache_read_tokens", "cache_creation_tokens", "turns", "job_wait_seconds"} <= set(today)
    assert sum(a["runs"] for a in report["accounts"]) == today["runs"] and all("sign_in" in a for a in report["accounts"])
    assert report["time"]["assignments_finished"] == 1 and report["time"]["slow_commands"] == []


def test_the_full_log_holds_what_happened_with_its_texts_but_no_credentials(client, project):
    import gzip

    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    assert client.patch(f"/api/projects/{pid}", json={"github_token": "ghp_secret"}).status_code == 200
    assert client.post(f"/api/projects/{pid}/repositories", json={"url": "https://user:pw@example.com/acme/api.git"}).status_code == 201
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Secret plan", "status": "todo"}).json()
    work = 'TOOL add_comment {"number": 1, "body": "said twice"}\nSAY finished on feat-a'
    chat(client, manager["id"], f'TOOL delegate {json.dumps({"agent": dev["slug"], "title": "Work", "instructions": work, "ticket_number": ticket["number"]})}')
    wait_for(lambda: idle(client, pid) and any(r["trigger"] == "wake" and r["status"] == "succeeded" for r in runs_of(client, pid)))

    resp = client.get(f"/api/projects/{pid}/work-log?days=7")
    assert resp.status_code == 200 and resp.headers["content-disposition"].endswith('.jsonl.gz"')
    rows = [json.loads(line) for line in gzip.decompress(resp.content).splitlines()]
    assert rows[0]["type"] == "export" and rows[0]["days"] == 7 and rows[1]["type"] == "project"
    by_type: dict[str, list[dict]] = {}
    for row in rows:
        by_type.setdefault(row["type"], []).append(row)
    assert {"agent", "conversation", "repository", "ticket", "ticket_move", "assignment", "run", "message", "update"} <= set(by_type)
    # The texts, as they were: what was ordered, what each turn was given and answered, every tool call.
    assert by_type["ticket"][0]["title"] == "Secret plan"
    (assignment,) = by_type["assignment"]
    assert assignment["instructions"] == work and assignment["result"].endswith("finished on feat-a") and assignment["created_at"].endswith("Z")
    turn = next(r for r in by_type["run"] if r["trigger"] == "assignment")
    assert "Assignment #1 from Manager" in turn["input_text"] and turn["assignment_id"] == assignment["id"]
    refused = next(m for m in by_type["message"] if m["kind"] == "tool" and m["content"] == "add_comment")
    assert refused["agent_id"] == dev["id"] and refused["meta"]["is_error"] and json.loads(refused["meta"]["input"])["body"] == "said twice"
    assert any('"Work"' in u["text"] and u["consumed_at"] for u in by_type["update"])  # the report as the manager got it
    assert any(c["system_prompt"] for c in by_type["conversation"])
    # No credentials, in whatever column they are.
    text = gzip.decompress(resp.content).decode()
    assert "ghp_secret" not in text and "user:pw" not in text and "https://[login]@example.com/acme/api.git" in text
    assert not any(key.endswith(("_enc", "_hash")) for row in rows for key in row)
    assert client.get("/api/projects/9999/work-log").status_code == 404

    # The numbers-only report counts the refused call, and the comments by who wrote them.
    report = client.get(f"/api/projects/{pid}/work-report?days=7").json()
    assert {a["name"]: a["tool_errors"] for a in report["agents"]} == {"Manager": {}, "Dev": {"add_comment": 1}}
    assert report["tickets"][0]["comments"] == {} and report["updates"]["count"] >= 1


def test_stats_count_tickets_and_runs(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    first = client.post(f"/api/projects/{pid}/tasks", json={"title": "A"}).json()
    client.post(f"/api/projects/{pid}/tasks", json={"title": "B"})
    client.patch(f"/api/tasks/{first['id']}", json={"status": "done"})
    client.patch(f"/api/tasks/{first['id']}", json={"status": "review"})
    client.patch(f"/api/tasks/{first['id']}", json={"status": "done"})
    chat(client, dev["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))

    stats = client.get(f"/api/projects/{pid}/stats?days=7").json()
    assert len(stats["days"]) == 7
    today = stats["days"][-1]
    assert (today["tickets_created"], today["tickets_done"], today["runs"]) == (2, 1, 1)
    assert stats["open_tickets"] == 1 and stats["median_cycle_seconds"] is not None
    assert [a["agent_id"] for a in stats["agents"]] == [dev["id"]]
    client.delete(f"/api/tasks/{first['id']}")
    assert client.get(f"/api/projects/{pid}/stats?days=7").json()["days"][-1]["tickets_done"] == 1  # history stays


def test_waiting_chat_messages_go_out_together(client, project):
    pid = project["id"]
    client.put("/api/settings", json={"values": {"max_parallel_runs": 0}})
    dev = make_dev(client, pid)
    first = chat(client, dev["id"], "SAY one")
    assert chat(client, dev["id"], "SAY two")["id"] == first["id"]
    assert chat(client, dev["id"], "SAY three")["input_text"] == "SAY one\n\nSAY two\n\nSAY three"
    client.put("/api/settings", json={"values": {"max_parallel_runs": 3}})
    client.app_ref.state.runs._wake.set()
    wait_for(lambda: idle(client, pid))
    assert len(runs_of(client, pid, agent_id=dev["id"])) == 1
    texts = [m["content"] for m in conversation(client, dev["id"])["messages"] if m["role"] == "assistant"]
    assert texts == ["one | two | three"]


def test_a_waiting_chat_message_can_be_deleted(client, project):
    pid = project["id"]
    client.put("/api/settings", json={"values": {"max_parallel_runs": 0}})
    dev = make_dev(client, pid)
    upload = client.post(f"/api/agents/{dev['id']}/attachments", files={"files": ("a.txt", b"hi", "text/plain")}).json()[0]
    waiting = client.post(f"/api/agents/{dev['id']}/messages", json={"text": "SAY never", "attachment_ids": [upload["id"]]}).json()
    assert client.delete(f"/api/runs/{waiting['id']}/message").status_code == 204
    assert runs_of(client, pid, agent_id=dev["id"]) == []
    assert client.post(f"/api/agents/{dev['id']}/messages", json={"text": "x", "attachment_ids": [upload["id"]]}).status_code >= 400  # the file is gone

    client.put("/api/settings", json={"values": {"max_parallel_runs": 3}})
    sent = chat(client, dev["id"], "SAY kept")
    wait_for(lambda: idle(client, pid))
    assert [m["content"] for m in conversation(client, dev["id"])["messages"] if m["role"] == "user"] == ["SAY kept"]
    # Once the agent has the message, it stays.
    assert client.delete(f"/api/runs/{sent['id']}/message").status_code == 409
    assert client.delete("/api/runs/9999/message").status_code == 404


def test_asking_a_busy_teammate_notifies_it_and_does_not_block_for_long(client, project, monkeypatch):
    monkeypatch.setattr("plainboard.orchestration.run_manager.ASK_BUSY_TIMEOUT_SECONDS", 0.05)
    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, dev["id"], "HOLD")
    wait_for(lambda: runs_of(client, pid, agent_id=dev["id"])[0]["status"] == "running")
    held = runs_of(client, pid, agent_id=dev["id"])[0]["id"]
    chat(client, manager["id"], 'TOOL ask_agent {"agent": "dev", "question": "SAY the answer"}')
    tool = wait_for(lambda: next((m for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool" and m["meta"].get("result")), None))
    assert "has not answered yet" in tool["meta"]["result"]
    (run_id, text), = runner(client).notices
    assert run_id == held and "SAY the answer" in text
    runner(client).released = True
    wait_for(lambda: idle(client, pid))
    assert any(r["trigger"] == "ask" and r["status"] == "succeeded" for r in runs_of(client, pid, agent_id=dev["id"]))
    # Nobody waited any more, so the answer comes as an update.
    wait_for(lambda: any("Late answer from Dev: the answer" in s.prompt for s in runner(client).specs))
    wait_for(lambda: idle(client, pid))


def test_a_busy_agent_reads_a_chat_message_right_away_and_answers_it_next(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    chat(client, dev["id"], "HOLD")
    wait_for(lambda: runs_of(client, pid, agent_id=dev["id"])[0]["status"] == "running")
    held = runs_of(client, pid, agent_id=dev["id"])[0]["id"]
    waiting = client.post(f"/api/agents/{dev['id']}/messages", json={"text": "SAY from the user"}).json()
    (run_id, text), = runner(client).notices
    assert run_id == held and "The user just wrote to you: SAY from the user" in text
    assert waiting["status"] == "queued"  # nothing is lost if the agent never sees the notice
    runner(client).released = True
    wait_for(lambda: idle(client, pid))
    messages = conversation(client, dev["id"])["messages"]
    texts = [m["content"] for m in messages if m["role"] == "assistant" and m["kind"] == "text"]
    assert texts[-1] == "from the user"
    # The agent never got to read it in the turn it was in (no step ended): it is read when its own turn begins,
    # and the chat shows it there, not at the time it was sent.
    sent = next(m for m in messages if m["content"] == "SAY from the user")
    answered = next(r for r in runs_of(client, pid) if r["id"] == waiting["id"])
    assert answered["read_at"] is None and sent["created_at"] >= answered["started_at"] >= waiting["created_at"]


def test_a_waiting_chat_message_is_shown_where_the_busy_agent_read_it(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    chat(client, dev["id"], "HOLD" + chr(10) + "TOOL list_tasks" + chr(10) + "SAY done with the old work")
    wait_for(lambda: runs_of(client, pid, agent_id=dev["id"])[0]["status"] == "running")
    waiting = client.post(f"/api/agents/{dev['id']}/messages", json={"text": "SAY from the user"}).json()
    assert waiting["status"] == "queued" and waiting["read_at"] is None  # at the bottom of the chat until it is read
    runner(client).released = True  # the agent's step ends: that is where it gets the message
    wait_for(lambda: idle(client, pid))
    answered = next(r for r in runs_of(client, pid) if r["id"] == waiting["id"])
    assert answered["read_at"] is not None and waiting["created_at"] <= answered["read_at"] <= answered["started_at"]
    messages = conversation(client, dev["id"])["messages"]
    sent = next(m for m in messages if m["content"] == "SAY from the user")
    assert sent["created_at"] == answered["read_at"]  # above what the agent wrote after reading it, although its own turn began later


def waiting_started(client, pid, run_id):
    return next(r for r in runs_of(client, pid) if r["id"] == run_id)["started_at"]


def test_chat_goes_before_other_waiting_turns(client, project):
    from plainboard.db import sessionmaker
    from plainboard.orchestration import inbox

    pid = project["id"]
    client.put("/api/settings", json={"values": {"max_parallel_runs": 0}})
    dev = make_dev(client, pid)

    async def wake() -> None:
        async with sessionmaker()() as s:
            await inbox.push(s, dev["id"], "Job 1 finished")
            await client.app_ref.state.runs.request_wake(s, dev["id"])

    client.portal.call(wake)
    chat(client, dev["id"], "SAY hello")
    client.put("/api/settings", json={"values": {"max_parallel_runs": 3}})
    client.app_ref.state.runs._wake.set()
    wait_for(lambda: idle(client, pid))
    # The chat turn took the update with it, and the wake-up that had nothing left to do is gone.
    runs = runs_of(client, pid, agent_id=dev["id"])
    assert [(r["trigger"], r["status"]) for r in runs] == [("chat", "succeeded")]
    assert "Job 1 finished" in runner(client).specs[-1].prompt


def test_the_manager_answers_while_the_members_are_paused(client, project):
    pid = project["id"]
    client.put("/api/settings", json={"values": {"max_parallel_runs": 0}})
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, dev["id"], "SAY never")
    chat(client, manager["id"], 'TOOL delegate {"agent": "dev", "title": "Work", "instructions": "SAY done"}')
    wait_for(lambda: not runs_of(client, pid, agent_id=manager["id"], active=True))
    (run,) = runs_of(client, pid, agent_id=manager["id"])
    # The limit is about the members: the manager's turn ran, and it was told why its assignment waits.
    assert run["status"] == "succeeded" and "Members are paused by the user (limit 0)" in run["result_text"]
    assert {r["status"] for r in runs_of(client, pid, agent_id=dev["id"])} == {"queued"}


def test_the_users_message_ends_a_wait_for_a_job(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, manager["id"], f"TOOL start_job {json.dumps({'command': 'make all', 'wait_minutes': 5})}")
    wait_for(lambda: bool(client.app_ref.state.runs._job_waiters))
    chat(client, manager["id"], "SAY hello")
    wait_for(lambda: idle(client, pid))
    answer, waited = runs_of(client, pid, agent_id=manager["id"])
    # The turn did not sit out the five minutes: the user got the answer, and the job goes on.
    assert "Stopped waiting for job #1: the user wrote to you." in waited["result_text"]
    assert answer["result_text"] == "hello"
    assert agents_by_slug(client, pid)["manager"]["running_jobs"] == 1 and not client.app_ref.state.runs._job_waiters


def test_the_users_message_ends_a_wait_for_an_answer_which_arrives_later(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, dev["id"], "HOLD")
    wait_for(lambda: runs_of(client, pid, agent_id=dev["id"])[0]["status"] == "running")
    chat(client, manager["id"], 'TOOL ask_agent {"agent": "dev", "question": "SAY forty-two"}')
    wait_for(lambda: bool(client.app_ref.state.runs._turn_waits))
    chat(client, manager["id"], "SAY hello")
    wait_for(lambda: len([r for r in runs_of(client, pid, agent_id=manager["id"]) if r["status"] == "succeeded"]) == 2)
    asked = runs_of(client, pid, agent_id=manager["id"])[-1]
    assert "Stopped waiting for Dev: the user wrote to you." in asked["result_text"]
    runner(client).released = True
    # The answer is not lost: it wakes the manager as an update.
    wait_for(lambda: any("Late answer from Dev: forty-two" in s.prompt for s in runner(client).specs))
    wait_for(lambda: idle(client, pid))


def test_agents_on_another_provider_get_its_models(client, project):
    pid = project["id"]
    dev = make_dev(client, pid, provider="codex")
    assert (dev["provider"], dev["model"], dev["summary_model"]) == ("codex", "gpt-6.1-terra", "gpt-6-luna")
    other = make_dev(client, pid, name="Other")
    assert (other["provider"], other["model"]) == ("claude_code", "claude-sonnet-5-5")
    chat(client, other["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))
    assert client.get(f"/api/agents/{other['id']}/conversation").json()["provider_session_id"]
    moved = client.patch(f"/api/agents/{other['id']}", json={"provider": "codex"}).json()
    assert (moved["model"], moved["summary_model"]) == ("gpt-6.1-terra", "gpt-6-luna")
    conv = client.get(f"/api/agents/{other['id']}/conversation").json()
    assert conv["provider_session_id"] is None  # a Claude session means nothing to Codex
    assert client.post(f"/api/projects/{pid}/agents", json={"name": "X", "provider": "nope"}).status_code == 400


def test_long_comment_threads_are_paged(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Chatty"}).json()
    for i in range(1, 26):
        client.post(f"/api/tasks/{ticket['id']}/comments", json={"body": f"note {i}"})

    detail = client.get(f"/api/tasks/{ticket['id']}").json()
    assert detail["comment_count"] == 25 and [c["body"] for c in detail["comments"]][0] == "note 6" and len(detail["comments"]) == 20
    older = client.get(f"/api/tasks/{ticket['id']}/comments?before={detail['comments'][0]['id']}").json()
    assert [c["body"] for c in older] == [f"note {i}" for i in range(1, 6)]

    chat(client, manager["id"], f'TOOL get_task {{"number": {ticket["number"]}}}\nTOOL get_comments {{"number": {ticket["number"]}, "offset": 10, "limit": 10}}')
    wait_for(lambda: idle(client, pid))
    first, second = [m["meta"]["result"] for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"][-2:]
    assert "(last 10 of 25; 15 older: get_comments)" in first and "note 16" in first and "note 15" not in first
    assert second.startswith("Comments 6-15 of 25 (oldest first); 5 older: offset=20:") and "note 6" in second and "note 16" not in second


def test_manager_sets_parallel_agents_within_the_users_limit(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, manager["id"], 'TOOL set_parallel_agents {"count": 5}\nTOOL set_parallel_agents {"count": 7}\nTOOL get_project')
    wait_for(lambda: idle(client, pid))
    results = [m["meta"]["result"] for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"]
    # The manager sets it for its own project; the number for projects without one stays.
    assert client.get(f"/api/projects/{pid}").json()["parallel_agents"] == 5 and client.get("/api/settings").json()["values"]["max_parallel_runs"] == 3
    assert any("between 1 and 6" in r for r in results) and any("Parallel agents: 5 (you may set 1-6; this project only)" in r for r in results)

    client.put("/api/settings", json={"values": {"manager_parallel_limit": 0}})
    chat(client, manager["id"], 'TOOL set_parallel_agents {"count": 2}')
    wait_for(lambda: idle(client, pid))
    assert client.get(f"/api/projects/{pid}").json()["parallel_agents"] == 5


def test_the_limit_of_parallel_agents_counts_per_project(client, project):
    """Two projects worked at the same time shared one limit: a busy project left the other one waiting."""
    one = project["id"]
    two = client.post("/api/projects", json={"name": "Second Project"}).json()["id"]
    client.put("/api/settings", json={"values": {"max_parallel_runs": 1}})
    a, b, c = make_dev(client, one), make_dev(client, one, name="Ops"), make_dev(client, two, name="Dev")
    working = lambda pid: sorted(r["agent_id"] for r in runs_of(client, pid) if r["status"] == "running")

    chat(client, a["id"], "HOLD")
    wait_for(lambda: working(one) == [a["id"]])
    chat(client, b["id"], "HOLD")
    chat(client, c["id"], "HOLD")
    wait_for(lambda: working(two) == [c["id"]])  # the other project has its own slot
    assert working(one) == [a["id"]]  # its teammate waits for the project's only one

    # A project with its own number: only that project gets more.
    assert client.patch(f"/api/projects/{one}", json={"parallel_agents": 2}).json()["parallel_agents"] == 2
    wait_for(lambda: working(one) == sorted([a["id"], b["id"]]))
    assert client.get(f"/api/projects/{two}").json()["parallel_agents"] is None

    # The manager's view counts its own project only.
    manager = agents_by_slug(client, two)["manager"]
    chat(client, manager["id"], "SAY ok")
    wait_for(lambda: any("Parallel members: 1 of 1 working" in s.prompt for s in runner(client).specs))
    runner(client).released = True
    wait_for(lambda: idle(client, one) and idle(client, two))


def test_manager_sees_and_stops_preview_servers_of_the_team(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    start_job(client, dev["id"], "npm run dev -- --host 0.0.0.0")
    wait_for(lambda: idle(client, pid))

    chat(client, manager["id"], "TOOL list_jobs")
    wait_for(lambda: idle(client, pid))
    listed = [m for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"][-1]["meta"]["result"]
    assert "Running in the team:" in listed and "#1 Dev, since <1 h: npm run dev" in listed

    chat(client, dev["id"], 'TOOL stop_job {"id": 1}')  # its own job: allowed, checked below for a stranger
    chat(client, manager["id"], 'TOOL stop_job {"id": 1}')
    wait_for(lambda: idle(client, pid))
    assert agents_by_slug(client, pid)["dev"]["running_jobs"] == 0

    tidy = next(s for s in client.get(f"/api/projects/{pid}/schedules").json() if s["name"] == "Tidy up")
    assert "preview or dev server" in tidy["prompt"]


def test_running_previews_are_listed_for_their_project_and_a_member_stops_one(client, project):
    from plainboard_protocol import JobPort

    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    other = client.post("/api/projects", json={"name": "Other"}).json()
    assert client.get(f"/api/projects/{pid}/previews").json() == []

    start_job(client, dev["id"], "npm run dev -- --host 0.0.0.0")
    wait_for(lambda: idle(client, pid))
    (spec,) = runner(client).job_specs
    # A server that listens on 127.0.0.1 only is a preview for the browser window; the runner's own port never is one.
    runner(client).ports = {spec.id: [JobPort(port=9000, local=True, title="Docs"), JobPort(port=8090, title="Runner")]}
    assert [(p["port"], p["title"], p["local"]) for p in client.get(f"/api/projects/{pid}/previews").json()] == [(9000, "Docs", True)]
    # The dev server, and what stands behind it on 127.0.0.1: only the first is a preview. One with a base path says so.
    runner(client).ports = {spec.id: [JobPort(port=5173, title="Shop"), JobPort(port=6000, base=True), JobPort(port=9000, local=True, title="Shop")]}
    shown = client.get(f"/api/projects/{pid}/previews").json()
    assert [(p["port"], p["title"], p["local"], p["base"], p["access"]) for p in shown] == [(5173, "Shop", False, False, "private"), (6000, "", False, True, "private")]
    assert shown[0]["agent_id"] == dev["id"] and shown[0]["command"].startswith("npm run dev") and shown[0]["started_at"].endswith("Z")
    assert client.get(f"/api/projects/{other['id']}/previews").json() == []  # another project's servers are not this one's

    chat(client, manager["id"], 'TOOL share_preview {"port": 5173, "access": "public", "days": 2}')
    wait_for(lambda: idle(client, pid))
    shared = client.get(f"/api/projects/{pid}/previews").json()[0]
    assert shared["access"] == "public" and shared["shared_until"].endswith("Z")

    runner(client).reachable = False  # restarting: nothing is known, and nothing breaks
    assert client.get(f"/api/projects/{pid}/previews").json() == []
    runner(client).reachable = True

    assert client.post(f"/api/projects/{pid}/previews/{shown[0]['job_id']}/stop").status_code == 204
    assert agents_by_slug(client, pid)["dev"]["running_jobs"] == 0
    assert client.get(f"/api/projects/{pid}/previews").json() == []
    # Whoever started it reads that with its next turn, without being woken for it.
    assert all(run["status"] != "queued" for run in runs_of(client, pid))
    chat(client, dev["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    assert "was stopped by Admin in PlainBoard" in runner(client).specs[-1].prompt


def test_work_ordered_from_a_paused_agent_waits_for_it_and_an_assignment_without_a_turn_gets_one(client, project):
    from plainboard.db import sessionmaker
    from plainboard.models import Assignment

    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Docs", "status": "todo"})
    chat(client, manager["id"], "TOOL pause_team")
    wait_for(lambda: idle(client, pid) and not agents_by_slug(client, pid)["dev"]["enabled"])

    # Ordered while the team is paused: the assignment waits with its turn, it is not left behind without one.
    chat(client, manager["id"], "TOOL delegate " + json.dumps({"agent": "dev", "title": "Write the docs", "instructions": "SAY docs written", "ticket_number": 1}))
    def answer() -> str | None:
        return next((m["meta"].get("result") for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool" and m["content"] == "delegate"), None)

    wait_for(answer)  # the assignment exists a moment before the tool's answer is written
    result = answer()
    assert "queued for dev" in result and "dev is paused: it starts when the agent is enabled again" in result
    (waiting,) = [r for r in runs_of(client, pid, agent_id=dev["id"]) if r["trigger"] == "assignment"]
    assert waiting["status"] == "queued"
    chat(client, manager["id"], "TOOL resume_team")
    wait_for(lambda: idle(client, pid) and client.get(f"/api/projects/{pid}/assignments").json()[0]["status"] == "done")

    # One that has no turn at all (left behind by an earlier version): it gets its turn when PlainBoard starts.
    async def orphan() -> None:
        async with sessionmaker()() as s:
            s.add(Assignment(project_id=pid, agent_id=dev["id"], created_by_agent_id=manager["id"], title="Left behind", instructions="SAY found again"))
            await s.commit()

    client.portal.call(orphan)
    client.portal.call(client.app_ref.state.runs._recover_interrupted)
    wait_for(lambda: idle(client, pid) and all(a["status"] == "done" for a in client.get(f"/api/projects/{pid}/assignments").json()))
    assert sorted(a["result"] for a in client.get(f"/api/projects/{pid}/assignments").json()) == ["docs written", "found again"]


def test_a_message_sent_while_the_runner_is_not_up_yet_waits_for_it(client, project, monkeypatch):
    """Right after an update the app answers a moment before the runner does. In production the
    user's first message after an update failed with "Runner unreachable"."""
    monkeypatch.setattr("plainboard.orchestration.run_manager.RUNNER_RETRY_SECONDS", 0.02)
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    runner(client).unavailable = 3
    sent = chat(client, manager["id"], "SAY here I am")
    wait_for(lambda: idle(client, pid))
    (run,) = [r for r in runs_of(client, pid) if r["id"] == sent["id"]]
    assert run["status"] == "succeeded" and run["result_text"] == "here I am" and runner(client).unavailable == 0

    # A runner that stays away is an error after all, said as one.
    monkeypatch.setattr("plainboard.orchestration.run_manager.RUNNER_TRIES", 2)
    runner(client).unavailable = 5
    lost = chat(client, manager["id"], "SAY anyone?")
    wait_for(lambda: idle(client, pid))
    (run,) = [r for r in runs_of(client, pid) if r["id"] == lost["id"]]
    assert run["status"] == "failed" and "Runner unreachable" in run["error"]


def test_a_stream_that_breaks_before_the_first_step_runs_once_more(client, project, monkeypatch):
    """Production: turns ended after a few seconds with "Runner unreachable: " (a reset connection)."""
    monkeypatch.setattr("plainboard.orchestration.run_manager.RUNNER_RETRY_SECONDS", 0.02)
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    sent = chat(client, manager["id"], "BREAK_ONCE\nSAY here I am")
    wait_for(lambda: idle(client, pid))
    (run,) = [r for r in runs_of(client, pid) if r["id"] == sent["id"]]
    assert run["status"] == "succeeded" and run["result_text"] == "here I am" and runner(client).breaks == 1

    sent = chat(client, manager["id"], "BREAK")  # broken again on the second try: the run fails with the error
    wait_for(lambda: idle(client, pid))
    (run,) = [r for r in runs_of(client, pid) if r["id"] == sent["id"]]
    assert run["status"] == "failed" and run["error"] == "Runner unreachable: ReadError" and runner(client).breaks == 3


async def test_the_runner_client_names_a_broken_stream():
    import httpx
    from plainboard.orchestration.runner_client import RunnerClient, RunnerStreamBroken
    from types import SimpleNamespace

    class Reset(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"type": "session", "session_id": "s"}\n'
            raise httpx.ReadError("")  # what a reset connection looks like: no text

    client = RunnerClient("http://runner")
    client._client = httpx.AsyncClient(base_url="http://runner", transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Reset())))
    spec = SimpleNamespace(run_id="7", model_dump=lambda **kw: {})
    with pytest.raises(RunnerStreamBroken, match=r"^Runner unreachable: ReadError$") as caught:
        async for _ in client.stream_run(spec):
            pass
    assert caught.value.events == 1
