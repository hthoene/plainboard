from conftest import agents_by_slug


def test_project_gets_a_manager(client, project):
    agents = list(agents_by_slug(client, project["id"]).values())
    assert len(agents) == 1
    manager = agents[0]
    assert manager["is_manager"] and manager["toolset"] == "board"
    assert manager["summary_model"] == "claude-haiku-5-5"
    assert client.delete(f"/api/agents/{manager['id']}").status_code == 400


def test_ticket_crud_comments_attachments(client, project):
    pid = project["id"]
    t1 = client.post(f"/api/projects/{pid}/tasks", json={"title": "First", "status": "todo", "labels": ["ui"]}).json()
    t2 = client.post(f"/api/projects/{pid}/tasks", json={"title": "Second"}).json()
    assert (t1["number"], t2["number"]) == (1, 2)
    assert t2["status"] == "backlog"

    updated = client.patch(f"/api/tasks/{t1['id']}", json={"title": "First!", "priority": "high"}).json()
    assert updated["title"] == "First!" and updated["priority"] == "high"

    assert client.post(f"/api/tasks/{t1['id']}/comments", json={"body": "hello"}).status_code == 201
    files = {"files": ("note.txt", b"content", "text/plain")}
    uploaded = client.post(f"/api/tasks/{t1['id']}/attachments", files=files).json()
    assert uploaded[0]["filename"] == "note.txt"
    assert client.get(f"/api/attachments/{uploaded[0]['id']}").content == b"content"

    detail = client.get(f"/api/tasks/{t1['id']}").json()
    assert detail["comment_count"] == 1 and detail["attachment_count"] == 1
    for value in (detail["created_at"], detail["updated_at"], detail["comments"][0]["created_at"]):
        assert value.endswith("Z") and "+" not in value, value
    assert detail["comments"][0]["body"] == "hello"

    assert client.delete(f"/api/tasks/{t2['id']}").status_code == 204
    assert [t["number"] for t in client.get(f"/api/projects/{pid}/tasks").json()] == [1]

    from plainboard.config import get_config

    assert len(list(get_config().uploads_dir.iterdir())) == 1
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert list(get_config().uploads_dir.iterdir()) == []


def test_local_workspace_folder(client):
    from plainboard.models import Agent, Project
    from plainboard.orchestration.context import workspace_path

    shared = client.post("/api/projects", json={"name": "Local", "workspace_path": "./MyApp\\src/"}).json()
    assert shared["workspace_path"] == "MyApp/src"
    assert client.post("/api/projects", json={"name": "Bad", "workspace_path": "../etc"}).status_code == 400
    agent = Agent(slug="dev")
    assert workspace_path(Project(slug="local", workspace_path="MyApp/src"), agent) == "/workspaces/MyApp/src"
    assert workspace_path(Project(slug="srv", workspace_path=None), agent) == "/workspaces/srv/dev"


def test_settings_mask_secrets(client):
    client.put("/api/settings", json={"values": {"claude_oauth_token": "sk-secret", "max_parallel_runs": 2}})
    body = client.get("/api/settings").json()
    assert body["values"]["claude_oauth_token"] is True
    assert body["values"]["max_parallel_runs"] == 2
    assert body["values"]["claude_auth_mode_effective"] == "oauth_token"
    assert "sk-secret" not in client.get("/api/settings").text
    assert client.put("/api/settings", json={"values": {"bogus": 1}}).status_code == 400


def test_notes_and_mcp_servers(client, project):
    pid = project["id"]
    note = client.post(f"/api/projects/{pid}/notes", json={"title": "Conventions", "content": "Use ruff", "pinned": True}).json()
    assert note["pinned"]
    server = client.post(
        f"/api/projects/{pid}/mcp-servers",
        json={"name": "fs", "transport": "stdio", "command": "npx", "args": ["-y", "server"], "env": {"TOKEN": "abc"}},
    ).json()
    assert server["env"] == {"TOKEN": "***"}
    patched = client.patch(f"/api/projects/{pid}/mcp-servers/{server['id']}", json={"env": {"TOKEN": "***", "MORE": "x"}}).json()
    assert patched["env"] == {"TOKEN": "***", "MORE": "***"}
    assert client.post(f"/api/projects/{pid}/mcp-servers", json={"name": "plainboard", "transport": "http", "url": "http://x"}).status_code == 400
    assert client.post(f"/api/projects/{pid}/mcp-servers", json={"name": "bad", "transport": "stdio"}).status_code == 400


def test_password_protection(client, monkeypatch):
    from plainboard import config

    monkeypatch.setenv("PLAINBOARD_PASSWORD", "pw")
    config.get_config.cache_clear()
    try:
        assert client.get("/api/projects").status_code == 401
        assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
        assert client.post("/api/auth/login", json={"password": "pw"}).status_code == 200
        assert client.get("/api/projects").status_code == 200
    finally:
        monkeypatch.delenv("PLAINBOARD_PASSWORD")
        config.get_config.cache_clear()


def test_public_api_shows_only_customer_texts(client, project):
    pid = project["id"]
    url = "/api/public/tasks"
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Internal secret", "description": "hidden", "customer_description": "no title, so hidden"})
    shown = client.post(f"/api/projects/{pid}/tasks", json={"title": "Refactor reader", "description": "hidden", "customer_title": "Faster opening of books", "customer_description": "Large books open sooner."}).json()
    other = client.post("/api/projects", json={"name": "Other"}).json()
    client.post(f"/api/projects/{other['id']}/tasks", json={"title": "Other", "customer_title": "Other project"})

    assert client.get(url).status_code == 401
    assert client.get(url, headers={"Authorization": "Bearer nope"}).status_code == 401

    key = client.post(f"/api/projects/{pid}/public-api-key").json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    resp = client.get(url, headers=headers)
    assert resp.status_code == 200 and resp.headers["access-control-allow-origin"] == "*"
    body = resp.json()
    assert body["project"] == project["name"]
    assert len(body["tasks"]) == 1
    assert set(body["tasks"][0]) == {"number", "title", "description", "status", "updated_at"}
    assert body["tasks"][0]["title"] == "Faster opening of books" and body["tasks"][0]["number"] == shown["number"]
    assert body["tasks"][0]["description"] == "Large books open sooner."
    assert "secret" not in resp.text and "Refactor" not in resp.text
    assert client.get(f"/api/projects/{pid}").json()["has_public_api_key"] is True

    client.patch(f"/api/tasks/{shown['id']}", json={"customer_title": ""})
    assert client.get(url, headers=headers).json()["tasks"] == []

    client.delete(f"/api/projects/{pid}/public-api-key")
    assert client.get(url, headers=headers).status_code == 401
