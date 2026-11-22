import pytest
from conftest import ADMIN_PASSWORD, agents_by_slug
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def sign_in(client, username: str, password: str) -> None:
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200


def as_admin(client) -> None:
    sign_in(client, "admin", ADMIN_PASSWORD)


def new_user(client, username: str, **more) -> dict:
    resp = client.post("/api/users", json={"username": username, "name": username.title(), "password": f"{username}-password", **more})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_a_new_installation_gets_its_first_admin_in_the_browser(client, monkeypatch):
    from plainboard import config

    # The fixture did the set-up; what it looked like before is shown by a second installation's answers.
    status = client.get("/api/auth/status").json()
    assert status["setup_required"] is False and status["user"]["username"] == "admin" and status["user"]["is_admin"]
    assert client.post("/api/auth/setup", json={"username": "second", "password": "another-password"}).status_code == 409  # only once

    client.cookies.clear()
    assert client.get("/api/auth/status").json() == {"setup_required": False, "setup_password_required": False, "user": None}
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "nobody", "password": ADMIN_PASSWORD}).status_code == 401
    as_admin(client)
    assert client.get("/api/projects").status_code == 200
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/projects").status_code == 401

    # On a server the setup password keeps strangers from claiming a fresh installation.
    monkeypatch.setenv("PLAINBOARD_PASSWORD", "pw")
    config.get_config.cache_clear()
    assert client.get("/api/auth/status").json()["setup_password_required"] is True


def test_the_setup_password_guards_a_fresh_installation(tmp_path, monkeypatch):
    from conftest import FakeRunner, empty_database

    from plainboard import config, main

    for name, value in (("DATA_DIR", tmp_path / "data"), ("UPLOADS_DIR", tmp_path / "uploads"), ("STATIC_DIR", tmp_path / "static"), ("PASSWORD", "pw")):
        monkeypatch.setenv(f"PLAINBOARD_{name}", str(value))
    config.get_config.cache_clear()
    empty_database()
    monkeypatch.setattr(main, "RunnerClient", FakeRunner)
    try:
        with TestClient(main.create_app()) as client:
            assert client.get("/api/auth/status").json() == {"setup_required": True, "setup_password_required": True, "user": None}
            body = {"username": "Hanna", "name": "Hanna", "password": "long-enough"}
            assert client.post("/api/auth/setup", json=body).status_code == 403
            assert client.post("/api/auth/setup", json={**body, "setup_password": "nope"}).status_code == 403
            assert client.post("/api/auth/setup", json={**body, "password": "short", "setup_password": "pw"}).status_code == 400
            created = client.post("/api/auth/setup", json={**body, "setup_password": "pw"})
            assert created.status_code == 201 and created.json()["username"] == "hanna" and created.json()["is_admin"]
            assert client.get("/api/projects").status_code == 200  # signed in at once
    finally:
        config.get_config.cache_clear()


def test_a_project_is_seen_by_its_members_only_and_a_role_limits_what_they_do(client, project):
    pid = project["id"]
    alone = client.post("/api/projects", json={"name": "Mine alone"}).json()
    viewer, member, owner = new_user(client, "vera"), new_user(client, "max"), new_user(client, "olga")
    for user, role in ((viewer, "viewer"), (member, "member"), (owner, "owner")):
        assert client.put(f"/api/projects/{pid}/members/{user['id']}", json={"role": role}).status_code == 200
    assert [(m["username"], m["role"]) for m in client.get(f"/api/projects/{pid}/members").json()] == [("admin", "owner"), ("max", "member"), ("olga", "owner"), ("vera", "viewer")]
    manager = agents_by_slug(client, pid)["manager"]
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "Build it"}).json()
    other_task = client.post(f"/api/projects/{alone['id']}/tasks", json={"title": "Private"}).json()

    # A viewer reads the project and changes nothing. The other project does not exist for them.
    sign_in(client, "vera", "vera-password")
    assert [(p["name"], p["role"]) for p in client.get("/api/projects").json()] == [(project["name"], "viewer")]
    assert client.get(f"/api/projects/{pid}/tasks").status_code == 200 and client.get(f"/api/agents/{manager['id']}/conversation").status_code == 200
    assert client.post(f"/api/projects/{pid}/tasks", json={"title": "x"}).status_code == 403
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "hi"}).status_code == 403
    assert client.patch(f"/api/tasks/{task['id']}", json={"title": "x"}).status_code == 403
    for path in (f"/api/projects/{alone['id']}", f"/api/projects/{alone['id']}/tasks", f"/api/tasks/{other_task['id']}"):
        assert client.get(path).status_code == 404, path
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/projects/{alone['id']}/ws"):
            pass
    with client.websocket_connect(f"/api/projects/{pid}/ws"):
        pass
    # Nothing about the installation either.
    for path in ("/api/users", "/api/secrets", "/api/access-keys", "/api/inspect/schema", "/api/github/repos"):
        assert client.get(path).status_code == 403, path
    assert client.put("/api/settings", json={"values": {"max_parallel_runs": 9}}).status_code == 403
    assert client.post("/api/projects", json={"name": "Not allowed"}).status_code == 403
    assert client.get("/api/settings").status_code == 200 and [u["username"] for u in client.get("/api/users/directory").json()] == ["admin", "max", "olga", "vera"]

    # A member works in it, but the project's settings, secrets and members are the owners'.
    sign_in(client, "max", "max-password")
    assert client.post(f"/api/projects/{pid}/tasks", json={"title": "From Max"}).status_code == 201
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "SAY hi"}).status_code == 202
    assert client.patch(f"/api/projects/{pid}", json={"description": "x"}).status_code == 403
    assert client.get(f"/api/projects/{pid}/secrets").status_code == 403
    assert client.put(f"/api/projects/{pid}/members/{member['id']}", json={"role": "owner"}).status_code == 403
    assert client.delete(f"/api/projects/{pid}").status_code == 403

    # Several people work here: what one writes carries the name, for the agents and for each other.
    chat = client.get(f"/api/agents/{manager['id']}/conversation").json()
    assert any(m["content"] == "[Max] SAY hi" for m in chat["messages"]) or any(r["input_text"] == "[Max] SAY hi" for r in client.get(f"/api/projects/{pid}/runs").json())
    assert any(r["author_name"] == "Max" for r in client.get(f"/api/projects/{pid}/runs").json())  # the chat shows name and picture from this
    assert client.post(f"/api/tasks/{task['id']}/comments", json={"body": "Looks good"}).json()["author_name"] == "Max"

    # An owner does all of that in this project, and nothing in another one.
    sign_in(client, "olga", "olga-password")
    assert client.patch(f"/api/projects/{pid}", json={"description": "Shared"}).json()["role"] == "owner"
    assert client.get(f"/api/projects/{pid}/secrets").status_code == 200
    assert client.delete(f"/api/projects/{pid}/members/{viewer['id']}").status_code == 200
    assert client.patch(f"/api/projects/{alone['id']}", json={"description": "x"}).status_code == 404

    # No longer a member: the project is gone for her.
    sign_in(client, "vera", "vera-password")
    assert client.get("/api/projects").json() == [] and client.get(f"/api/projects/{pid}/tasks").status_code == 404


def test_what_a_person_does_on_a_ticket_reaches_the_agents_with_the_name(client, project):
    """Seen in production: a friend answered a question on a ticket, and the manager wrote the answer into the
    ticket as the owner's decision. It was told "User decided", and put down the one name it knew."""
    import json

    from conftest import idle, wait_for
    from test_orchestration import chat, runner

    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    fake = runner(client)
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "Game repository"}).json()

    def decide(answer: str) -> None:
        question = {"task_number": task["number"], "question": "Extra service on the nodes?", "options": [{"label": "Yes"}, {"label": "No"}]}
        chat(client, manager["id"], "Ask us." + chr(10) + "TOOL request_decision " + json.dumps(question))  # a line of its own: the first one gets the name
        wait_for(lambda: idle(client, pid))
        decision = next(d for d in client.get(f"/api/tasks/{task['id']}").json()["decisions"] if d["status"] == "open")
        assert client.post(f"/api/decisions/{decision['id']}/answer", json={"option_index": 0, "text": answer}).status_code == 200
        wait_for(lambda: idle(client, pid) and "decided on ticket" in fake.specs[-1].prompt)

    # One person: "User" says it all.
    decide("go")
    assert f"User decided on ticket #{task['number']}" in fake.specs[-1].prompt

    # A friend joins and decides: the manager reads who it was, and so does everyone in the ticket.
    friend = new_user(client, "luchs")
    assert client.put(f"/api/projects/{pid}/members/{friend['id']}", json={"role": "member"}).status_code == 200
    sign_in(client, "luchs", "luchs-password")
    decide("build it now")
    assert f"User Luchs decided on ticket #{task['number']}" in fake.specs[-1].prompt and "build it now" in fake.specs[-1].prompt
    assert client.get(f"/api/tasks/{task['id']}").json()["comments"][-1]["author_name"] == "Luchs"

    # The same for what only a person can do, and for handing a ticket to the manager.
    as_admin(client)
    assert client.patch(f"/api/tasks/{task['id']}", json={"user_action": "Try it on a node"}).status_code == 200
    sign_in(client, "luchs", "luchs-password")
    assert client.post(f"/api/tasks/{task['id']}/user-action/done", json={"note": "works"}).status_code == 200
    wait_for(lambda: idle(client, pid) and "finished" in fake.specs[-1].prompt)
    assert f'User Luchs finished "Try it on a node" on ticket #{task["number"]}: works' in fake.specs[-1].prompt
    assert client.get(f"/api/tasks/{task['id']}").json()["comments"][-1]["author_name"] == "Luchs"
    assert client.post(f"/api/tasks/{task['id']}/start").status_code == 200
    wait_for(lambda: idle(client, pid) and "handed you this ticket" in fake.specs[-1].prompt)
    assert "User Luchs handed you this ticket" in fake.specs[-1].prompt


def test_admins_manage_users_and_one_admin_always_remains(client, project):
    user = new_user(client, "max")
    assert client.post("/api/users", json={"username": "Max", "password": "max-password"}).status_code == 400  # taken, whatever the case
    assert client.post("/api/users", json={"username": "a b", "password": "max-password"}).status_code == 400
    admin = next(u for u in client.get("/api/users").json() if u["username"] == "admin")
    assert client.patch(f"/api/users/{admin['id']}", json={"is_admin": False}).status_code == 400
    assert client.delete(f"/api/users/{admin['id']}").status_code == 400

    # Someone allowed to create projects owns what they create; nobody else sees it, except the admins.
    assert client.patch(f"/api/users/{user['id']}", json={"may_create_projects": True}).json()["may_create_projects"] is True
    sign_in(client, "max", "max-password")
    own = client.post("/api/projects", json={"name": "Max' project"}).json()
    assert own["role"] == "owner" and [p["id"] for p in client.get("/api/projects").json()] == [own["id"]]

    # One's own password: the old one is needed, and other browsers are signed out.
    other_browser = dict(client.cookies)
    assert client.patch("/api/auth/me", json={"current_password": "nope", "new_password": "a-new-password"}).status_code == 403
    assert client.patch("/api/auth/me", json={"name": "Maximilian", "current_password": "max-password", "new_password": "a-new-password"}).json()["name"] == "Maximilian"
    assert client.get("/api/projects").status_code == 200  # this browser stays signed in
    client.cookies.clear()
    client.cookies.update(other_browser)
    assert client.get("/api/projects").status_code == 401
    sign_in(client, "max", "a-new-password")

    # Switched off by an admin: signed out at once, and no sign-in.
    as_admin(client)
    assert {p["id"] for p in client.get("/api/projects").json()} == {project["id"], own["id"]}
    assert client.patch(f"/api/users/{user['id']}", json={"enabled": False}).status_code == 200
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "max", "password": "a-new-password"}).status_code == 401


def test_a_project_is_created_all_or_nothing_and_its_wizard_works_for_a_creator_who_is_no_admin(client, project):
    from test_orchestration import runner

    client.put("/api/settings", json={"values": {"github_token": "admin-token"}})
    user = new_user(client, "max", may_create_projects=True)
    sign_in(client, "max", "max-password")

    # A step that fails leaves no project behind, not even one only the admins would see.
    twins = [{"url": "https://github.com/a/app.git"}, {"url": "https://github.com/b/app.git"}]
    assert client.post("/api/projects", json={"name": "Twins", "repositories": twins}).status_code == 400
    as_admin(client)
    assert [p["name"] for p in client.get("/api/projects").json()] == [project["name"]]

    # What the wizard asks after the first step works for the creator, who owns the project.
    sign_in(client, "max", "max-password")
    own = client.post("/api/projects", json={"name": "Max' project", "first_steps": True}).json()
    pid = own["id"]
    assert own["role"] == "owner" and [m["user_id"] for m in client.get(f"/api/projects/{pid}/members").json()] == [user["id"]]
    assert client.get(f"/api/projects/{pid}/agents").status_code == 200
    # GitHub: the project's own token only, never the admin's.
    shown = client.get(f"/api/projects/{pid}/github/repos").json()
    assert shown["ok"] is False and "own" in shown["detail"]
    assert client.post(f"/api/projects/{pid}/github/repos", json={"name": "shop"}).json()["ok"] is False
    assert client.post("/api/settings/check/github", json={"token": "max-token"}).json()["ok"] is True
    assert client.post("/api/settings/check/github", json={}).json()["ok"] is False
    assert client.patch(f"/api/projects/{pid}", json={"github_token": "max-token"}).status_code == 200
    assert client.get(f"/api/projects/{pid}/github/repos").json()["ok"] is True and runner(client).github_token_used == "max-token"
    assert client.post(f"/api/projects/{pid}/repositories", json={"url": "https://github.com/acme/api.git"}).status_code == 201
    assert client.get(f"/api/projects/{pid}/secrets").status_code == 200 and client.get("/api/users/directory").status_code == 200
    # Someone else's project, and the installation's token, stay out of reach.
    assert client.get(f"/api/projects/{project['id']}/github/repos").status_code == 404
    assert client.get("/api/github/repos").status_code == 403


def test_a_profile_picture_is_set_by_its_user_and_a_chat_message_knows_who_wrote_it(client, project):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    me = client.get("/api/auth/status").json()["user"]
    assert me["avatar_version"] == 0 and client.get(f"/api/avatars/{me['id']}").status_code == 404
    version = client.put("/api/auth/me/avatar", files={"file": ("me.png", png, "image/png")}).json()["avatar_version"]
    shown = client.get(f"/api/avatars/{me['id']}")
    assert version > 0 and shown.content == png and shown.headers["content-type"] == "image/png" and "immutable" in shown.headers["cache-control"]
    assert [p["avatar_version"] for p in client.get("/api/users/directory").json()] == [version]

    # Only pictures: a file that merely says it is one is refused, and so is a large one.
    for name, data, content_type in (("x.svg", b"<svg/>", "image/svg+xml"), ("x.png", b"<html>", "image/png"), ("x.png", png + b"0" * 300_000, "image/png")):
        assert client.put("/api/auth/me/avatar", files={"file": (name, data, content_type)}).status_code == 400
    assert client.get(f"/api/avatars/{me['id']}").content == png
    assert client.delete("/api/auth/me/avatar").json()["avatar_version"] == 0 and client.get(f"/api/avatars/{me['id']}").status_code == 404

    # Alone in a project nobody's name is put into the text (the agent needs none), and the message still knows its writer.
    manager = agents_by_slug(client, project["id"])["manager"]
    run = client.post(f"/api/agents/{manager['id']}/messages", json={"text": "SAY hi"}).json()
    assert (run["input_text"], run["author_name"]) == ("SAY hi", me["name"])


def test_the_mentions_switch_is_per_account_and_mentions_are_recorded_either_way(client):
    assert client.get("/api/auth/status").json()["user"]["show_mentions"] is True  # on by default
    new_user(client, "max")
    sign_in(client, "max", "max-password")
    assert client.patch("/api/auth/me", json={"show_mentions": False}).json()["show_mentions"] is False
    assert client.get("/api/auth/status").json()["user"]["show_mentions"] is False
    assert client.patch("/api/auth/me", json={"name": "Max"}).json()["show_mentions"] is False  # other changes leave it
    as_admin(client)
    assert client.get("/api/auth/status").json()["user"]["show_mentions"] is True  # another account's own
    sign_in(client, "max", "max-password")
    assert client.patch("/api/auth/me", json={"show_mentions": True}).json()["show_mentions"] is True


def test_an_admin_sets_the_profile_of_anyone_and_nobody_else_does(client):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    user = new_user(client, "max")
    other = new_user(client, "eva")
    shown = client.put(f"/api/users/{user['id']}/avatar", files={"file": ("x.png", png, "image/png")}).json()
    assert shown["avatar_version"] > 0 and client.get(f"/api/avatars/{user['id']}").content == png
    assert client.put(f"/api/users/{user['id']}/avatar", files={"file": ("x.svg", b"<svg/>", "image/svg+xml")}).status_code == 400
    changed = client.patch(f"/api/users/{user['id']}", json={"name": "Max M.", "username": "Maxi"}).json()
    assert (changed["name"], changed["username"]) == ("Max M.", "maxi")
    assert client.patch(f"/api/users/{user['id']}", json={"username": "eva"}).status_code == 400  # taken
    assert client.delete(f"/api/users/{user['id']}/avatar").json()["avatar_version"] == 0

    sign_in(client, "maxi", "max-password")
    assert client.put(f"/api/users/{other['id']}/avatar", files={"file": ("x.png", png, "image/png")}).status_code == 403
    assert client.delete(f"/api/users/{other['id']}/avatar").status_code == 403
    assert client.patch(f"/api/users/{other['id']}", json={"name": "Hacked"}).status_code == 403


def test_every_route_has_a_rule(client):
    """A route without a rule would be for admins only: fine for safety, but nobody decided it."""
    from plainboard.api import browser, preview, ws
    from plainboard.main import API_MODULES
    from plainboard.services.access import Need, rule_for

    paths = [route.path for module in (*API_MODULES, ws, browser, preview) for route in module.router.routes if route.path.startswith("/api/")]
    assert len(paths) > 100 and "/api/projects/{project_id}/tasks" in paths and "/api/projects/{project_id}/ws" in paths
    assert [path for path in paths if rule_for(path) is None] == []
    # The beginning of a path decides, at a "/": a longer rule wins over a shorter one.
    assert rule_for("/api/projects").write == Need.CREATOR and rule_for("/api/projects/{project_id}").write == Need.OWNER
    assert rule_for("/api/projects/{project_id}/tasks/archive").write == Need.MEMBER
    assert rule_for("/api/secrets").read == Need.ADMIN and rule_for("/api/secrets/{secret_id}").read == Need.OWNER
    assert rule_for("/api/auth/login").read == Need.PUBLIC and rule_for("/api/auth/me").read == Need.USER
    assert rule_for("/api/authors") is None
