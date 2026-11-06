from datetime import timedelta

from conftest import agents_by_slug, idle, wait_for
from test_orchestration import chat, conversation, make_dev, runner


def tool_results(client, agent_id: int) -> list[str]:
    return [m["meta"]["result"] for m in conversation(client, agent_id)["messages"] if m["kind"] == "tool"]


def test_manager_plans_and_members_finish_tasks(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    dev = make_dev(client, pid)

    todos = '[{"title": "Add login", "description": "Use **OAuth**.\\n\\nKeep sessions."}, {"title": "Write docs"}]'
    chat(client, manager["id"], f'TOOL add_todos {{"agent": "dev", "todos": {todos}}}\nTOOL list_todos {{}}')
    wait_for(lambda: idle(client, pid))
    added, listed = tool_results(client, manager["id"])[-2:]
    assert added == "Added 1, 2 to dev's list."
    assert "Open tasks on everyone's list: 1-2 of 2" in listed and "- 1 [dev]: Add login +details" in listed
    assert next(a for a in client.get(f"/api/projects/{pid}/agents").json() if a["id"] == dev["id"])["open_todos"] == 2

    # The list reaches the member in the turn, never in the cached system prompt.
    chat(client, dev["id"], "TOOL get_todo {\"todo_id\": 1}")
    wait_for(lambda: idle(client, pid))
    spec = runner(client).specs[-1]
    assert "Your task list (2 open" in spec.prompt and "- 1: Add login +details" in spec.prompt
    assert "Add login" not in spec.system_prompt
    assert tool_results(client, dev["id"])[-1] == "Task 1 for dev [open]: Add login\n\nUse **OAuth**.\n\nKeep sessions."

    # Unchanged list: not sent again in the same session.
    chat(client, dev["id"], 'TOOL update_todo {"todo_id": 1, "done": true}')
    wait_for(lambda: idle(client, pid))
    assert runner(client).specs[-1].resume_session_id and "Your task list" not in runner(client).specs[-1].prompt
    assert tool_results(client, dev["id"])[-1] == "Task 1 done."

    # Changed list: sent again; done tasks are gone from it.
    chat(client, dev["id"], 'TOOL update_todo {"todo_id": 2, "done": true}')
    wait_for(lambda: idle(client, pid))
    prompt = runner(client).specs[-1].prompt
    assert "Your task list (1 open" in prompt and "Add login" not in prompt
    chat(client, dev["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))
    assert "Your task list has no open tasks now." in runner(client).specs[-1].prompt

    page = client.get(f"/api/agents/{dev['id']}/todos?state=done").json()
    assert page["total"] == 2 and [t["title"] for t in page["items"]] == ["Write docs", "Add login"]
    assert client.get(f"/api/agents/{dev['id']}/todos").json() == {"items": [], "total": 0}


def test_members_only_touch_their_own_list(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    qa = make_dev(client, pid, name="QA", role="Tester", description="Testing")
    other = client.post(f"/api/agents/{qa['id']}/todos", json={"title": "Test login"}).json()

    chat(client, dev["id"], f'TOOL get_todo {{"todo_id": {other["id"]}}}\nTOOL update_todo {{"todo_id": {other["id"]}, "done": true}}\nTOOL add_todos {{"agent": "qa", "todos": [{{"title": "Mine"}}]}}')
    wait_for(lambda: idle(client, pid))
    results = tool_results(client, dev["id"])
    assert results[0].endswith("is not on your list.") and results[1].endswith("is not on your list.")
    assert results[2] == "Added 2 to dev's list."  # members can only add to their own list
    assert "delete_todos" not in {t.name for t in runner(client).specs[-1].bridge.tools}
    assert client.get(f"/api/agents/{qa['id']}/todos").json()["total"] == 1


def test_done_tasks_move_to_the_archive_and_are_deleted(client, project):
    from plainboard.db import sessionmaker
    from plainboard.models import Todo, utcnow
    from plainboard.services import todos as todo_service
    from sqlalchemy import update

    pid = project["id"]
    dev = make_dev(client, pid)
    ids = [client.post(f"/api/agents/{dev['id']}/todos", json={"title": f"T{i}"}).json()["id"] for i in range(3)]
    for todo_id in ids:
        assert client.patch(f"/api/todos/{todo_id}", json={"done": True}).json()["done_at"]

    async def age(todo_id: int, days: int) -> None:
        async with sessionmaker()() as s:
            await s.execute(update(Todo).where(Todo.id == todo_id).values(done_at=utcnow() - timedelta(days=days)))
            await s.commit()

    async def purge() -> int:
        async with sessionmaker()() as s:
            return await todo_service.purge(s)

    client.portal.call(age, ids[0], 8)
    client.portal.call(age, ids[1], 91)
    states = lambda state: [t["title"] for t in client.get(f"/api/agents/{dev['id']}/todos?state={state}").json()["items"]]  # noqa: E731
    assert states("done") == ["T2"] and states("archived") == ["T0", "T1"]
    assert client.portal.call(purge) == 1
    assert states("archived") == ["T0"]

    reopened = client.patch(f"/api/todos/{ids[0]}", json={"done": False}).json()
    assert reopened["done_at"] is None and states("open") == ["T0"]
