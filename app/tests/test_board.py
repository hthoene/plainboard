from conftest import ADMIN_PASSWORD, POSTGRES, agents_by_slug


def test_project_gets_a_manager(client, project):
    agents = list(agents_by_slug(client, project["id"]).values())
    assert len(agents) == 1
    manager = agents[0]
    assert manager["is_manager"] and manager["toolset"] == "board"
    assert manager["summary_model"] == "claude-haiku-5-5"
    assert client.delete(f"/api/agents/{manager['id']}").status_code == 400


def test_a_project_made_in_the_ui_starts_with_the_two_tickets_that_set_it_up(client, project):
    from conftest import agents_by_slug, idle, wait_for

    assert client.get(f"/api/projects/{project['id']}/tasks").json() == []  # made over the API: nothing is put on its board
    new = client.post("/api/projects", json={"name": "Shop", "first_steps": True}).json()
    tickets = sorted(client.get(f"/api/projects/{new['id']}/tasks").json(), key=lambda t: t["number"])
    assert [(t["number"], t["status"], t["priority"]) for t in tickets] == [(1, "todo", "high"), (2, "todo", "high")]
    assert "what this project is about" in tickets[0]["title"] and "team" in tickets[1]["title"]
    # What the second one has to carry: which class of model for whom, a reviewer, the limit, how finished work lands,
    # and that all of it is a recommendation the user may overrule.
    text = tickets[1]["description"]
    for said in ("list_models", "newest", "A reviewer on a strong model", "core work", "set_parallel_agents", "land_first", "a recommendation", "They may decide differently"):
        assert said in text, said
    # No model is named: a name would still be picked when newer models exist.
    assert not any(name in (tickets[0]["description"] + text).lower() for name in ("gpt", "claude", "opus", "sonnet", "astra", "sol ", "fable", "haiku"))

    # The manager reads of them with its first turn, whatever the user writes first; no turn was spent before.
    manager = agents_by_slug(client, new["id"])["manager"]
    assert client.get(f"/api/projects/{new['id']}/runs").json() == []
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "SAY hello"}).status_code == 202
    wait_for(lambda: idle(client, new["id"]))
    assert "Two tickets are in To do to set it up" in client.app_ref.state.runner.specs[-1].prompt

    # The language people are written to in: theirs until the project has one, which the first ticket asks for.
    assert "update_project language" in tickets[0]["description"]
    assert "write to a person in the language they write in" in client.app_ref.state.runner.specs[-1].system_prompt
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": 'TOOL update_project {"language": "German"}'}).status_code == 202
    wait_for(lambda: idle(client, new["id"]))
    assert client.get(f"/api/projects/{new['id']}").json()["language"] == "German"
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "SAY ok"}).status_code == 202
    wait_for(lambda: idle(client, new["id"]))
    spec = client.app_ref.state.runner.specs[-1]
    assert "write to people in German" in spec.system_prompt + spec.prompt

    # What it chooses from: the models of the installation with their class, the strongest first.
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "TOOL list_models {}"}).status_code == 202
    wait_for(lambda: idle(client, new["id"]))
    messages = client.get(f"/api/agents/{manager['id']}/conversation").json()["messages"]
    listed = next(m["meta"]["result"] for m in messages if m["kind"] == "tool" and m["content"] == "list_models")
    assert "claude_code: claude-sonnet-5-5 (medium), claude-haiku-5-5 (small)" in listed


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


def test_deleting_a_project_can_take_its_folders_along(client):
    runner = client.app_ref.state.runner
    kept = client.post("/api/projects", json={"name": "Shop"}).json()
    repo = client.post(f"/api/projects/{kept['id']}/repositories", json={"name": "api", "url": "https://github.com/acme/api.git"}).json()
    # Not asked for: the agents' folders stay, the runner's own clones of the repositories go.
    assert client.delete(f"/api/projects/{kept['id']}").status_code == 204
    assert runner.removed[-1].workspace is None and runner.removed[-1].trains == [f"p{kept['id']}-r{repo['id']}"]

    # Asked for: a new project of the same name gets the same folder, so the old one has to go.
    again = client.post("/api/projects", json={"name": "Shop"}).json()
    assert again["slug"] == kept["slug"]
    runner.remove_fails = True  # then nothing is deleted, so it can be tried again
    refused = client.delete(f"/api/projects/{again['id']}?files=true")
    assert refused.status_code == 400 and "Permission denied" in refused.json()["detail"]
    assert any(p["id"] == again["id"] for p in client.get("/api/projects").json())
    runner.remove_fails = False
    assert client.delete(f"/api/projects/{again['id']}?files=true").status_code == 204
    assert runner.removed[-1].workspace == f"/workspaces/{kept['slug']}"

    # A folder of the user's own is never deleted.
    local = client.post("/api/projects", json={"name": "Local", "workspace_path": "my-app"}).json()
    assert client.delete(f"/api/projects/{local['id']}?files=true").status_code == 204
    assert runner.removed[-1].workspace is None

    # Large folders take minutes. Until they are gone the project rests, so nothing starts in them, and
    # asking again (the window can be opened a second time, or by someone else) deletes nothing twice.
    from concurrent.futures import ThreadPoolExecutor

    from conftest import wait_for

    slow = client.post("/api/projects", json={"name": "Slow"}).json()
    runner.remove_holds, before = True, len(runner.removed)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(client.delete, f"/api/projects/{slow['id']}?files=true")
        wait_for(lambda: len(runner.removed) > before)
        assert next(p for p in client.get("/api/projects").json() if p["id"] == slow["id"])["paused_at"] is not None
        again = client.delete(f"/api/projects/{slow['id']}?files=true")
        assert again.status_code == 409 and "being deleted already" in again.json()["detail"]
        runner.remove_holds = False
        assert first.result(timeout=10).status_code == 204
    assert len(runner.removed) == before + 1 and all(p["id"] != slow["id"] for p in client.get("/api/projects").json())
    # Folders that cannot be deleted: the project stays, and goes on as before.
    stays = client.post("/api/projects", json={"name": "Stays"}).json()
    runner.remove_fails = True
    assert client.delete(f"/api/projects/{stays['id']}?files=true").status_code == 400
    assert next(p for p in client.get("/api/projects").json() if p["id"] == stays["id"])["paused_at"] is None


def test_thinking_effort_by_what_a_model_does(client):
    # A new installation: managers on medium, members and summaries on high.
    pid = client.post("/api/projects", json={"name": "Efforts"}).json()["id"]
    manager = next(a for a in client.get(f"/api/projects/{pid}/agents").json() if a["is_manager"])
    dev = client.post(f"/api/projects/{pid}/agents", json={"name": "Dev", "role": "developer"}).json()
    assert (manager["effort"], dev["effort"]) == ("medium", "high")

    client.put("/api/settings", json={"values": {"default_manager_effort": "high", "default_worker_effort": "low", "default_summary_effort": None}})
    pid = client.post("/api/projects", json={"name": "Efforts 2"}).json()["id"]
    manager = next(a for a in client.get(f"/api/projects/{pid}/agents").json() if a["is_manager"])
    dev = client.post(f"/api/projects/{pid}/agents", json={"name": "Dev", "role": "developer"}).json()
    assert (manager["effort"], dev["effort"]) == ("high", "low")

    # Summaries: the installation's effort unless the agent has its own; "" gives it up again.
    assert dev["summary_effort"] is None
    assert client.patch(f"/api/agents/{dev['id']}", json={"summary_effort": "medium"}).json()["summary_effort"] == "medium"
    assert client.patch(f"/api/agents/{dev['id']}", json={"summary_effort": ""}).json()["summary_effort"] is None


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
    client.put("/api/settings", json={"values": {"github_token": "sk-secret", "max_parallel_runs": 2}})
    body = client.get("/api/settings").json()
    assert body["values"]["github_token"] is True
    assert body["values"]["max_parallel_runs"] == 2
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


def test_a_read_key_reads_everything_and_changes_nothing(client, project):
    pid = project["id"]
    assert client.patch(f"/api/projects/{pid}", json={"github_token": "ghp_secret"}).status_code == 200
    created = client.post("/api/access-keys", json={"label": "Analysis"}).json()
    key = created["key"]
    assert key.startswith("pbr_") and created["hint"] == key[-4:]
    (listed,) = client.get("/api/access-keys").json()
    assert listed["label"] == "Analysis" and listed["key"] is None and listed["last_used_at"] is None

    client.cookies.clear()  # nobody is signed in: only the key speaks
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects", headers={"Authorization": "Bearer pbr_nope"}).status_code == 401
    # Reading: the board, the chats, the full log.
    assert [p["id"] for p in client.get("/api/projects", headers=headers).json()] == [pid]
    manager = client.get(f"/api/projects/{pid}/agents", headers=headers).json()[0]
    assert client.get(f"/api/agents/{manager['id']}/conversation", headers=headers).status_code == 200
    assert client.get(f"/api/projects/{pid}/work-log?days=1", headers=headers).status_code == 200
    # The database itself, for questions no endpoint foresaw: one SELECT, and credentials read as nothing.
    if POSTGRES:  # what keeps this safe is SQLite's own: with PostgreSQL it says so
        assert "SQLite only" in client.get("/api/inspect/schema", headers=headers).json()["detail"]
        assert client.get("/api/inspect/query", params={"sql": "select 1"}, headers=headers).status_code == 400
        return
    tables = {t["table"]: t for t in client.get("/api/inspect/schema", headers=headers).json()}
    assert tables["projects"]["rows"] == 1 and "name VARCHAR(200)" in tables["projects"]["columns"]
    assert not any(c.startswith(("github_token_enc", "key_hash")) for t in tables.values() for c in t["columns"])
    query = lambda sql, **more: client.get("/api/inspect/query", params={"sql": sql, **more}, headers=headers)  # noqa: E731
    counted = query("select is_manager, count(*) as n from agents group by is_manager").json()
    assert counted == {"columns": ["is_manager", "n"], "rows": [[1, 1]], "more": False}
    assert query("select name, github_token_enc as t, length(github_token_enc) as n from projects").json()["rows"] == [[project["name"], None, None]]
    assert query("select key_hash, hint from access_keys").json()["rows"] == [[None, key[-4:]]]
    assert query("select value from settings").json()["rows"] in ([], [[None]] * len(query("select key from settings").json()["rows"]))
    assert query("select id from agents", limit=1).json()["more"] is False and query("select 1 union all select 2", limit=1).json()["more"] is True
    for refused in ("delete from agents", "update projects set name = 'x'", "drop table agents", "pragma journal_mode = delete", "select 1; select 2", "attach database 'x.db' as x"):
        assert query(refused).status_code == 400, refused
    assert client.get(f"/api/projects/{pid}/agents", headers=headers).json()[0]["name"] == manager["name"]  # nothing was changed
    # Nothing else: no change of any kind, and nothing about the credentials.
    assert client.post(f"/api/projects/{pid}/tasks", json={"title": "x"}, headers=headers).status_code == 403
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "hi"}, headers=headers).status_code == 403
    assert client.put("/api/settings", json={"values": {"max_parallel_runs": 9}}, headers=headers).status_code == 403
    assert client.delete(f"/api/access-keys/{created['id']}", headers=headers).status_code == 403
    for hidden in ("/api/access-keys", "/api/accounts", "/api/github/repos"):
        assert client.get(hidden, headers=headers).status_code == 403
    assert client.get("/api/projects", cookies={}, headers={"Authorization": "Bearer " + key.upper()}).status_code == 401

    # Revoked by the user (who is signed in): the key is worth nothing at once.
    assert client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}).status_code == 200
    assert client.get("/api/access-keys").json()[0]["last_used_at"] is not None
    assert client.delete(f"/api/access-keys/{created['id']}").status_code == 204
    client.cookies.clear()
    assert client.get("/api/projects", headers=headers).status_code == 401


def test_public_api_shows_only_customer_texts(client, project):
    pid = project["id"]
    url = "/api/public/tasks"
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Internal secret", "description": "hidden", "customer_description": "no title, so hidden"})
    shown = client.post(f"/api/projects/{pid}/tasks", json={"title": "Refactor reader", "description": "hidden", "customer_title": "Faster opening of books", "customer_description": "Large books open sooner."}).json()
    other = client.post("/api/projects", json={"name": "Other"}).json()
    client.post(f"/api/projects/{other['id']}/tasks", json={"title": "Other", "customer_title": "Other project"})

    assert client.get(url).status_code == 401
    assert client.get(url, headers={"Authorization": "Bearer nope"}).status_code == 401

    created = client.post(f"/api/projects/{pid}/public-api-keys", json={"label": "Website"}).json()
    key = created["key"]
    assert key.startswith("pb_") and created["hint"] == key[-4:] and created["last_used_at"] is None
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
    # Several keys with labels: each works on its own, and none can be read again.
    second = client.post(f"/api/projects/{pid}/public-api-keys", json={"label": "Status page"}).json()
    listed = client.get(f"/api/projects/{pid}/public-api-keys").json()
    assert [(k["label"], k["key"]) for k in listed] == [("Website", None), ("Status page", None)]
    assert listed[0]["last_used_at"] and listed[1]["last_used_at"] is None
    assert client.get(url, headers={"Authorization": f"Bearer {second['key']}"}).status_code == 200
    assert client.patch(f"/api/public-api-keys/{second['id']}", json={"label": "Partner"}).json()["label"] == "Partner"
    assert client.delete(f"/api/public-api-keys/{second['id']}").status_code == 204
    assert client.get(url, headers={"Authorization": f"Bearer {second['key']}"}).status_code == 401

    client.patch(f"/api/tasks/{shown['id']}", json={"customer_title": ""})
    assert client.get(url, headers=headers).json()["tasks"] == []

    client.delete(f"/api/public-api-keys/{created['id']}")
    assert client.get(url, headers=headers).status_code == 401


def test_done_tickets_are_archived_and_restored(client, project):
    from datetime import timedelta

    from plainboard.db import sessionmaker
    from plainboard.models import Task, utcnow
    from plainboard.services import board as board_service
    from sqlalchemy import update

    pid = project["id"]
    assert project["auto_archive_days"] == 14
    tickets = [client.post(f"/api/projects/{pid}/tasks", json={"title": title, "status": "done"}).json() for title in ("Old", "New")]
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Open", "status": "todo"})

    async def age_and_auto_archive():
        async with sessionmaker()() as s:
            await s.execute(update(Task).where(Task.id == tickets[0]["id"]).values(updated_at=utcnow() - timedelta(days=15)))
            await s.commit()
            return await board_service.auto_archive(s)

    assert client.portal.call(age_and_auto_archive) == 1
    on_board = lambda: [t["title"] for t in client.get(f"/api/projects/{pid}/tasks").json()]  # noqa: E731
    archived = lambda: [t["title"] for t in client.get(f"/api/projects/{pid}/tasks", params={"archived": True}).json()]  # noqa: E731
    assert sorted(on_board()) == ["New", "Open"] and archived() == ["Old"]
    assert client.get(f"/api/tasks/{tickets[0]['id']}").json()["archived_at"].endswith("Z")

    assert client.post(f"/api/projects/{pid}/tasks/archive").json() == {"archived": 1}
    assert on_board() == ["Open"] and sorted(archived()) == ["New", "Old"]

    assert client.patch(f"/api/tasks/{tickets[0]['id']}", json={"archived": False}).json()["archived_at"] is None
    client.patch(f"/api/tasks/{tickets[1]['id']}", json={"status": "review"})  # moving it brings it back too
    assert sorted(on_board()) == ["New", "Old", "Open"] and archived() == []

    client.patch(f"/api/projects/{pid}", json={"auto_archive_days": 0})
    assert client.portal.call(age_and_auto_archive) == 0


def test_tickets_left_in_review_move_to_done_unless_they_wait_for_the_user(client, project):
    from datetime import timedelta

    from plainboard.db import sessionmaker
    from plainboard.models import Task, utcnow
    from plainboard.services import board as board_service
    from sqlalchemy import update

    pid = project["id"]
    assert project["review_days"] == 3
    old, marked, new = (client.post(f"/api/projects/{pid}/tasks", json={"title": t, "status": "review"}).json() for t in ("Old", "Marked", "New"))
    client.patch(f"/api/tasks/{marked['id']}", json={"user_action": "Try the login"})

    async def age_and_accept():
        async with sessionmaker()() as s:
            await s.execute(update(Task).where(Task.id.in_((old["id"], marked["id"]))).values(updated_at=utcnow() - timedelta(days=4)))
            await s.commit()
            return await board_service.accept_reviews(s)

    client.patch(f"/api/projects/{pid}", json={"review_days": 0})
    assert client.portal.call(age_and_accept) == 0
    client.patch(f"/api/projects/{pid}", json={"review_days": 3})
    assert client.portal.call(age_and_accept) == 1
    detail = lambda t: client.get(f"/api/tasks/{t['id']}").json()  # noqa: E731
    assert [detail(t)["status"] for t in (old, marked, new)] == ["done", "review", "review"]
    comment = detail(old)["comments"][-1]
    assert comment["author_kind"] == "system" and "3 days in review" in comment["body"]


def test_preview_opens_a_server_in_the_runner_through_plainboard(client, monkeypatch):
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, RedirectResponse

    seen = {}
    site = FastAPI()

    @site.get("/")
    async def home(request: Request):
        seen.update(host=request.headers["host"], cookie=request.headers.get("cookie"), url=str(request.url))
        return {"page": "home"}

    @site.get("/old")
    async def old():
        return RedirectResponse("/new?x=1")

    @site.get("/preview-base/5173/about")
    async def based():
        return {"page": "based"}

    @site.get("/login")
    async def login():
        out = JSONResponse({"page": "login"}, headers={"Referrer-Policy": "no-referrer", "Strict-Transport-Security": "max-age=600", "Clear-Site-Data": '"cookies"'})
        out.raw_headers += [
            (b"set-cookie", b"sid=1; Domain=localhost; Path=/; HttpOnly; Secure"),
            (b"set-cookie", b"theme=dark; SameSite=None; Secure"),
            (b"set-cookie", b"pb_session=taken"),
        ]
        return out

    client.app_ref.state.preview_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=site))
    client.cookies.set("theme", "dark")  # next to the PlainBoard login the client already has

    resp = client.get("/preview/5173/?q=1")
    assert resp.json() == {"page": "home"} and resp.cookies["plainboard_preview"] == "5173"
    # The server sees itself as localhost and never the PlainBoard login.
    assert seen == {"host": "localhost:5173", "cookie": "theme=dark", "url": "http://localhost:5173/?q=1"}
    assert client.get("/preview/5173/old", follow_redirects=False).headers["location"] == "/preview/5173/new?x=1"
    assert client.get("/preview/5173", follow_redirects=False).headers["location"] == "/preview/5173/"

    # A preview page that asks for a path at the root is sent back to its server, even for /api.
    from_page = {"referer": "http://testserver/preview/5173/"}
    assert client.get("/api/items?x=1", headers=from_page, follow_redirects=False).headers["location"] == "/preview/5173/api/items?x=1"
    assert client.get("/api/health", headers={"referer": "http://elsewhere/preview/5173/"}).status_code == 200

    # Without a Referer (the page hides it), a part of a page goes to the last opened preview, unless
    # it is PlainBoard's own: its API, its files, a page.
    part = {"sec-fetch-site": "same-origin", "sec-fetch-dest": "script"}
    assert client.get("/static/app.js", headers=part, follow_redirects=False).headers["location"] == "/preview/5173/static/app.js"
    assert client.get("/static/app.js", headers=part | {"referer": "http://testserver/"}, follow_redirects=False).status_code == 307
    assert client.get("/api/health", headers=part).status_code == 200
    assert client.get("/static/app.js", headers={"sec-fetch-site": "same-origin", "sec-fetch-dest": "document"}, follow_redirects=False).status_code != 307
    assert client.get("/static/app.js", headers=part | {"referer": "http://testserver/some/page"}, follow_redirects=False).status_code != 307

    # A server that was told its base path gets the path as it is.
    assert client.get("/preview-base/5173/about?x=1").json() == {"page": "based"}
    assert client.get("/preview-base/5173", follow_redirects=False).headers["location"] == "/preview-base/5173/"
    assert client.get("/data.json", headers={"referer": "http://testserver/preview-base/5173/about"}, follow_redirects=False).headers["location"] == "/preview/5173/data.json"

    # What the server sends for localhost is made to fit PlainBoard's address, and must not act on PlainBoard itself.
    headers = client.get("/preview/5173/login").headers
    cookies = headers.get_list("set-cookie")
    assert "sid=1; Path=/; HttpOnly" in cookies and "theme=dark; SameSite=Lax" in cookies
    assert not any(c.startswith("pb_session") for c in cookies)
    assert headers["referrer-policy"] == "same-origin" and "strict-transport-security" not in headers and "clear-site-data" not in headers

    assert client.get("/preview/8090/").status_code == 404  # the runner's own API
    assert client.get("/preview/80/").status_code == 404

    client.cookies.clear()
    assert client.get("/preview/5173/").status_code == 401


def test_preview_says_when_nothing_listens(client):
    import httpx

    def refuse(request):
        raise httpx.ConnectError("refused")

    client.app_ref.state.preview_client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    resp = client.get("/preview/3000/")
    assert resp.status_code == 502 and "0.0.0.0" in resp.text


def test_an_agent_shows_ticket_attachments_and_its_own_files_in_its_chat(client, project):
    from conftest import agents_by_slug
    from plainboard.orchestration.run_manager import RunContext

    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Logo"}).json()
    on_ticket = client.post(f"/api/tasks/{ticket['id']}/attachments", files=[("files", ("logo.png", b"png", "image/png"))]).json()[0]
    client.app_ref.state.runs._tokens["tok"] = RunContext(run_id=1, project_id=pid, agent_id=manager["id"], token="tok")
    auth = {"Authorization": "Bearer tok"}

    sent = client.post(
        "/api/internal/runs/1/chat-files", headers=auth, data={"attachment_ids": str(on_ticket["id"])}, files=[("files", ("shot.png", b"new", "image/png"))]
    ).json()
    message = client.get(f"/api/agents/{manager['id']}/conversation").json()["messages"][-1]
    new = message["meta"]["attachments"][1]["id"]
    assert not sent["is_error"] and sent["text"] == f"Shown in your chat: logo.png (attachment {on_ticket['id']}), shot.png (attachment {new})."
    assert (message["role"], message["kind"]) == ("assistant", "files")
    assert [f["filename"] for f in message["meta"]["attachments"]] == ["logo.png", "shot.png"]
    assert client.get(f"/api/attachments/{message['meta']['attachments'][1]['id']}").content == b"new"

    assert client.post("/api/internal/runs/1/chat-files", headers=auth, data={"attachment_ids": "999"}).json()["is_error"]
    assert client.post("/api/internal/runs/1/chat-files", headers=auth, data={"attachment_ids": ""}).json()["is_error"]


def test_a_preview_is_private_until_it_is_shared_with_a_password_or_publicly(client, project, monkeypatch):
    import httpx
    from fastapi import FastAPI

    from plainboard.db import sessionmaker
    from plainboard.services import preview as preview_service

    site = FastAPI()

    @site.get("/")
    async def home():
        return {"page": "home"}

    client.app_ref.state.preview_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=site))
    client.cookies.clear()  # a visitor without a PlainBoard account

    def share(access, **kwargs):
        async def call():
            async with sessionmaker()() as s:
                return (await preview_service.share(s, project["id"], 5173, access, **kwargs))[1]

        return client.portal.call(call)

    assert client.get("/preview/5173/").status_code == 401  # private: only the signed-in user

    share("public")
    assert client.get("/preview/5173/").json() == {"page": "home"}
    assert client.get("/preview/5174/").status_code == 401  # other ports stay private

    generated = share("password")
    locked = client.get("/preview/5173/")
    assert locked.status_code == 401 and "Protected preview" in locked.text
    assert client.post("/api/public/preview/5173/unlock", data={"password": "wrong-one"}, follow_redirects=False).status_code == 401
    opened = client.post("/api/public/preview/5173/unlock", data={"password": generated}, follow_redirects=False)
    assert opened.status_code == 303 and opened.headers["location"] == "/preview/5173/"
    assert client.get("/preview/5173/").json() == {"page": "home"}

    share("password", password="another-one")  # a new password locks earlier visitors out
    assert client.get("/preview/5173/").status_code == 401
    share("private")
    assert client.post("/api/public/preview/5173/unlock", data={"password": "another-one"}, follow_redirects=False).status_code == 401

    assert client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}).status_code == 200
    assert client.get("/preview/5173/").json() == {"page": "home"}


def test_messages_to_members_need_the_setting(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    dev = client.post(f"/api/projects/{pid}/agents", json={"name": "Dev", "role": "developer"}).json()
    assert client.put("/api/settings", json={"values": {"member_chat": False}}).status_code == 200
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "SAY hi"}).status_code == 202
    resp = client.post(f"/api/agents/{dev['id']}/messages", json={"text": "SAY hi"})
    assert resp.status_code == 403 and "Direct messages to members are off" in resp.json()["detail"]
    assert client.get(f"/api/agents/{dev['id']}/conversation").status_code == 200  # reading stays possible
    assert client.put("/api/settings", json={"values": {"member_chat": True}}).status_code == 200
    assert client.post(f"/api/agents/{dev['id']}/messages", json={"text": "SAY hi"}).status_code == 202


def test_messages_to_members_are_off_by_default(client, project):
    dev = client.post(f"/api/projects/{project['id']}/agents", json={"name": "Dev", "role": "developer"}).json()
    assert client.put("/api/settings", json={"values": {"member_chat": None}}).status_code == 200  # as on a new installation
    assert client.get("/api/settings").json()["values"]["member_chat"] is False
    assert client.post(f"/api/agents/{dev['id']}/messages", json={"text": "SAY hi"}).status_code == 403
