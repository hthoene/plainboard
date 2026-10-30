import json
from datetime import datetime, timedelta

from conftest import FakeRunner, agents_by_slug, idle, runs_of, wait_for
from plainboard_protocol import JobState


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
    assert "full shell access" in spec.system_prompt and "Do small things yourself" in spec.system_prompt
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


def test_restart_reports_interrupted_assignment_to_the_manager(client, project):
    from plainboard.db import sessionmaker
    from plainboard.models import Assignment, Run

    pid = project["id"]
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Build it", "status": "in_progress"}).json()

    async def interrupt_and_restart():
        async with sessionmaker()() as s:
            assignment = Assignment(project_id=pid, task_id=ticket["id"], agent_id=dev["id"], created_by_agent_id=manager["id"], title="Do the work", instructions="x", status="running")
            s.add(assignment)
            await s.commit()
            s.add(Run(project_id=pid, agent_id=dev["id"], trigger="assignment", input_text="x", status="running", assignment_id=assignment.id, task_id=ticket["id"]))
            await s.commit()
        await client.app_ref.state.runs._recover_interrupted()

    client.portal.call(interrupt_and_restart)
    wait_for(lambda: idle(client, pid))
    spec = runner(client).specs[-1]
    assert spec.bridge.tools and "Do the work" in spec.prompt and "interrupted by a restart" in spec.prompt
    assert "delegate" in {t.name for t in spec.bridge.tools}  # it went to the manager
    detail = client.get(f"/api/tasks/{ticket['id']}").json()
    assert detail["assignments"][0]["status"] == "failed"
    assert "interrupted by a restart" in detail["comments"][-1]["body"]


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
    assert any("Assignment #1" in c["body"] for c in detail["comments"])
    assert detail["assignments"][0]["status"] == "done"

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


def test_message_agent_reply_wakes_sender(client, project):
    pid = project["id"]
    make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, manager["id"], 'TOOL message_agent {"agent": "dev", "message": "SAY pong"}')
    wait_for(lambda: any(r["trigger"] == "wake" and r["status"] == "succeeded" for r in runs_of(client, pid)))
    wake_msg = next(m for m in conversation(client, manager["id"])["messages"] if m["meta"].get("trigger") == "wake")
    assert wake_msg["meta"]["inbox"] == ["Reply from Dev: pong"]


def test_compression_resets_session_and_carries_memory(client, project):
    pid = project["id"]
    dev = make_dev(client, pid, context_limit_tokens=5000, keep_recent_messages=2)
    chat(client, dev["id"], "SAY first answer")  # fresh session: base context 1000
    wait_for(lambda: idle(client, pid))
    assert conversation(client, dev["id"])["session_base_tokens"] == 1000
    chat(client, dev["id"], "SAY grown\nCONTEXT 9000")  # same session grew by 8000
    wait_for(lambda: idle(client, pid))
    conv = conversation(client, dev["id"])
    assert conv["compressions"] == 1 and conv["provider_session_id"] is None
    assert conv["messages"][-1]["kind"] == "summary" and conv["messages"][-1]["content"] == "SUMMARY #1"
    assert runner(client).completions[-1].model == "claude-haiku-5-5"

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
    wait_for(lambda: idle(client, pid))
    assert conversation(client, dev["id"])["compressions"] == 1
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
    manager = agents_by_slug(client, pid)["manager"]
    run = chat(client, manager["id"], "SAY never")
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
    sched = client.get(f"/api/projects/{pid}/schedules").json()[0]
    assert sched["enabled"] is False and sched["next_run_at"] is None


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
        'TOOL create_task {"title": "Follow-up", "status": "todo"}',
        'TOOL update_task {"number": 1, "status": "review"}',
    ]
    chat(client, manager["id"], "\n".join(lines))
    wait_for(lambda: idle(client, pid))
    tool_msgs = [m for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"]
    assert len(tool_msgs) == 5 and not any(m["meta"]["is_error"] for m in tool_msgs), [m["meta"] for m in tool_msgs]
    assert agents_by_slug(client, pid)["qa"]["model"] == "claude-haiku-5-5"
    assert client.get(f"/api/projects/{pid}/notes").json()[0]["title"] == "Stack"
    assert client.get(f"/api/projects/{pid}/schedules").json()[0]["cron"] == "0 9 * * 1-5"
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


def test_worker_wake_result_goes_to_the_manager(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    start_job(client, dev["id"])
    wait_for(lambda: idle(client, pid))
    spec = runner(client).job_specs[-1]
    runner(client).jobs[spec.id] = JobState(running=False, exit_code=0, log_path="/jobs/x.log", tail="SAY Build is green")
    job_tick(client)
    wait_for(lambda: idle(client, pid) and "Update from Dev" in runner(client).specs[-1].prompt)
    assert "delegate" in {t.name for t in runner(client).specs[-1].bridge.tools}


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
    dev = make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    # The manager's own run takes the only slot, so the message to Dev stays queued.
    chat(client, manager["id"], 'TOOL message_agent {"agent": "dev", "message": "hi"}\nTOOL stop_agent {"agent": "dev"}\nTOOL stop_agent {"agent": "manager"}')
    wait_for(lambda: idle(client, pid))
    assert [r["status"] for r in runs_of(client, pid, agent_id=dev["id"])] == ["cancelled"]
    assert agents_by_slug(client, pid)["dev"]["enabled"] is False
    tools = [m for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"]
    assert tools[2]["meta"]["is_error"] is True


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
    assert runs_of(client, pid, agent_id=dev["id"])[0]["status"] == "succeeded"
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


def test_runs_report_plan_limits(client, project):
    pid = project["id"]
    client.put("/api/settings", json={"values": {"claude_auth_mode": "oauth_token"}})
    dev = make_dev(client, pid)
    assert client.get("/api/limits").json()["windows"] == []
    chat(client, dev["id"], "LIMITS five_hour 0.4 3600\nLIMITS seven_day 0.7 -10")
    wait_for(lambda: idle(client, pid))
    body = client.get("/api/limits").json()
    used = {w["name"]: w["utilization"] for w in body["windows"]}
    assert used == {"five_hour": 0.4, "seven_day": 0.0} and body["updated_at"]  # the weekly window has reset
    client.put("/api/settings", json={"values": {"claude_auth_mode": "api_key"}})
    assert client.get("/api/limits").json()["windows"] == []
