import json

from conftest import agents_by_slug, idle, wait_for
from test_orchestration import chat, conversation, make_dev, runner


def add_repo(client, pid, url, **extra):
    resp = client.post(f"/api/projects/{pid}/repositories", json={"url": url, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_repositories_and_agent_access(client, project):
    pid = project["id"]
    front = add_repo(client, pid, "https://github.com/acme/web-frontend.git", description="React UI")
    back = add_repo(client, pid, "https://github.com/acme/api.git", name="backend", default_branch="main")
    assert front["name"] == "web-frontend" and back["name"] == "backend"
    assert client.post(f"/api/projects/{pid}/repositories", json={"url": "https://x/y.git", "name": "backend"}).status_code == 400
    assert client.post(f"/api/projects/{pid}/repositories", json={"url": "https://x/y.git", "name": "../evil"}).status_code == 400

    dev = make_dev(client, pid)  # default: all repositories
    ui = client.post(f"/api/projects/{pid}/agents", json={"name": "UI", "all_repos": False, "repository_ids": [front["id"]]}).json()
    assert ui["all_repos"] is False and ui["repository_ids"] == [front["id"]]

    chat(client, dev["id"], "SAY hi")
    chat(client, ui["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))
    specs = {s.bridge.run_id: s for s in runner(client).specs}
    by_agent = {}
    for spec in specs.values():
        by_agent[spec.workspace.path.rsplit("/", 1)[-1]] = spec
    assert {r.name for r in by_agent["dev"].workspace.repos} == {"web-frontend", "backend"}
    assert [r.name for r in by_agent["ui"].workspace.repos] == ["web-frontend"]
    assert next(r for r in by_agent["dev"].workspace.repos if r.name == "backend").branch == "main"
    assert "/workspaces/demo-project/ui/web-frontend" in by_agent["ui"].system_prompt
    assert "backend" not in by_agent["ui"].system_prompt.split("## Team")[0]

    # Deleting a repository removes it from every agent.
    assert client.delete(f"/api/repositories/{front['id']}").status_code == 204
    assert agents_by_slug(client, pid)["ui"]["repository_ids"] == []


def test_github_repo_picker_uses_project_token_override(client, project):
    pid = project["id"]
    assert client.get("/api/github/repos").json()["ok"] is False
    client.put("/api/settings", json={"values": {"github_token": "global-token"}})
    assert client.get("/api/github/repos").json()["repos"][0]["full_name"] == "acme/api"
    assert runner(client).github_token_used == "global-token"
    client.patch(f"/api/projects/{pid}", json={"github_token": "project-token"})
    client.get(f"/api/github/repos?project_id={pid}")
    assert runner(client).github_token_used == "project-token"


def test_manager_manages_repositories(client, project):
    pid = project["id"]
    make_dev(client, pid)
    client.post(f"/api/projects/{pid}/agents", json={"name": "UI"})
    manager = agents_by_slug(client, pid)["manager"]
    lines = [
        "TOOL add_repository " + json.dumps({"url": "https://github.com/acme/web.git", "description": "Frontend"}),
        "TOOL add_repository " + json.dumps({"url": "https://github.com/acme/api.git", "name": "api"}),
        "TOOL set_agent_repositories " + json.dumps({"agent": "ui", "repositories": ["web"]}),
        "TOOL list_repositories {}",
    ]
    chat(client, manager["id"], "\n".join(lines))
    wait_for(lambda: idle(client, pid))
    tools = [m for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"]
    assert not any(t["meta"]["is_error"] for t in tools), [t["meta"] for t in tools]
    listing = tools[-1]["meta"]["result"]
    assert "web: https://github.com/acme/web.git — Frontend | agents: dev, ui" in listing
    assert "api: https://github.com/acme/api.git | agents: dev" in listing

    # The manager sees which agent has which repositories.
    chat(client, manager["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    spec = runner(client).specs[-1]
    # Same session: the changes arrive as a briefing update in the turn (system prompt stays frozen).
    assert "## Project repositories" in spec.prompt and "repos: web" in spec.prompt
    assert "## Project repositories" not in spec.system_prompt


def test_note_duplicates_are_rejected(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    lines = [
        'TOOL create_note {"title": "Coding conventions", "content": "Use ruff"}',
        'TOOL create_note {"title": "coding conventions", "content": "dup"}',
        'TOOL create_note {"title": "Coding convention", "content": "near dup"}',
        'TOOL create_note {"title": "Deployment", "content": "Docker"}',
        'TOOL list_notes {}',
    ]
    chat(client, manager["id"], "\n".join(lines))
    wait_for(lambda: idle(client, pid))
    tools = [m["meta"] for m in conversation(client, manager["id"])["messages"] if m["kind"] == "tool"]
    assert [t["is_error"] for t in tools] == [False, True, True, False, False]
    assert "update_note" in tools[1]["result"]
    assert "updated today" in tools[4]["result"]
    assert len(client.get(f"/api/projects/{pid}/notes").json()) == 2


def test_pinned_notes_respect_budget():
    from plainboard.models import Note
    from plainboard.orchestration.prompts import PINNED_BUDGET_CHARS, pinned_notes_block

    notes = [Note(id=1, title="Small", content="a" * 100), Note(id=2, title="Huge", content="b" * PINNED_BUDGET_CHARS)]
    block = pinned_notes_block(notes)
    assert "a" * 100 in block and "b" * 50 not in block
    assert "Huge (note 2)" in block and "Condense" in block


def test_many_notes_remind_manager(client, project):
    pid = project["id"]
    from plainboard.services.notes import TIDY_EVERY

    for i in range(TIDY_EVERY):
        client.post(f"/api/projects/{pid}/notes", json={"title": f"Note {i}", "content": "x"})
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, manager["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    msg = next(m for m in conversation(client, manager["id"])["messages"] if m["role"] == "user")
    assert any(f"{TIDY_EVERY} notes" in item for item in msg["meta"]["inbox"])
