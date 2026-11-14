from conftest import agents_by_slug, idle, wait_for
from test_orchestration import chat, make_dev, runner
from test_todos import tool_results


def test_own_notes_survive_a_new_session_without_touching_the_cache(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    dev = make_dev(client, pid)

    chat(client, dev["id"], 'TOOL remember {"text": "Use pnpm"}')
    wait_for(lambda: idle(client, pid))
    assert tool_results(client, dev["id"])[-1] == "Saved as own note 1 (8/3000 chars)."
    frozen = runner(client).specs[-1].system_prompt
    assert "Use pnpm" not in frozen

    # What the member wrote itself is not sent again, and the cached system prompt stays as it is.
    chat(client, dev["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))
    spec = runner(client).specs[-1]
    assert spec.resume_session_id and spec.system_prompt == frozen and "[Briefing update]" not in spec.prompt

    # A note from the user arrives once, in the turn.
    added = client.post(f"/api/agents/{dev['id']}/notes", json={"text": "Answer in German"}).json()
    assert added["created_by_agent_id"] is None
    chat(client, dev["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))
    spec = runner(client).specs[-1]
    assert spec.system_prompt == frozen and spec.prompt.startswith("[Briefing update]")
    assert "### Your own notes" in spec.prompt and "- [2] Answer in German" in spec.prompt and "- [1] Use pnpm" in spec.prompt
    chat(client, dev["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))
    assert "[Briefing update]" not in runner(client).specs[-1].prompt

    # A new session (reset or compression) has them in its system prompt; they are private.
    client.post(f"/api/agents/{dev['id']}/reset-session")
    chat(client, dev["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))
    spec = runner(client).specs[-1]
    assert spec.resume_session_id is None and "- [1] Use pnpm" in spec.system_prompt and "- [2] Answer in German" in spec.system_prompt
    chat(client, manager["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))
    assert "Use pnpm" not in runner(client).specs[-1].system_prompt
    assert agents_by_slug(client, pid)["dev"]["own_notes"] == 2


def test_own_notes_are_private_and_limited(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    dev = make_dev(client, pid)
    mine = client.post(f"/api/agents/{manager['id']}/notes", json={"text": "Ask before deleting"}).json()

    chat(
        client,
        dev["id"],
        f'TOOL forget {{"note_ids": [{mine["id"]}]}}\nTOOL remember {{"text": "x", "note_id": {mine["id"]}}}\n'
        'TOOL remember {"text": "First"}\nTOOL remember {"text": "Second", "note_id": 2}\nTOOL forget {"note_ids": [2]}',
    )
    wait_for(lambda: idle(client, pid))
    results = tool_results(client, dev["id"])
    assert results[0].endswith("not found") and results[1].endswith("not found")
    assert results[2:] == ["Saved as own note 2 (5/3000 chars).", "Saved as own note 2 (6/3000 chars).", "Deleted 1 own note(s)."]
    assert client.get(f"/api/agents/{dev['id']}/notes").json() == []
    assert [n["text"] for n in client.get(f"/api/agents/{manager['id']}/notes").json()] == ["Ask before deleting"]

    for _ in range(6):
        assert client.post(f"/api/agents/{dev['id']}/notes", json={"text": "y" * 500}).status_code == 201
    full = client.post(f"/api/agents/{dev['id']}/notes", json={"text": "one more"})
    assert full.status_code >= 400 and "3000 characters" in full.text
    note_id = client.get(f"/api/agents/{dev['id']}/notes").json()[0]["id"]
    assert client.patch(f"/api/agent-notes/{note_id}", json={"text": "short"}).json()["text"] == "short"
    assert client.delete(f"/api/agent-notes/{note_id}").status_code == 204
