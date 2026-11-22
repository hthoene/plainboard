"""What a person's projects may use, set per person by an admin (services/permissions.py)."""

from conftest import ADMIN_PASSWORD, idle, runs_of, wait_for


def sign_in(client, username: str, password: str) -> None:
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200


def friend(client, username: str = "olga", **limits) -> dict:
    user = client.post("/api/users", json={"username": username, "password": f"{username}-password", "may_create_projects": True}).json()
    if limits:
        resp = client.patch(f"/api/users/{user['id']}", json=limits)
        assert resp.status_code == 200, resp.text
        user = resp.json()
    return user


def as_friend(client, username: str = "olga") -> None:
    sign_in(client, username, f"{username}-password")


def project_names(client) -> list[str]:
    sign_in(client, "admin", ADMIN_PASSWORD)
    return sorted(p["name"] for p in client.get("/api/projects").json())


def test_a_person_creates_as_many_projects_as_allowed_and_a_refusal_creates_nothing(client, project):
    user = friend(client, max_projects=1)
    assert user["max_projects"] == 1 and user["account_ids"] is None and user["runner_ids"] is None  # the rest: as before
    before = project_names(client)
    as_friend(client)
    assert client.post("/api/projects", json={"name": "First"}).status_code == 201
    refused = client.post("/api/projects", json={"name": "Second"})
    assert refused.status_code == 403 and "as many as you may have" in refused.json()["detail"]
    assert project_names(client) == sorted([*before, "First"])

    client.patch(f"/api/users/{user['id']}", json={"max_projects": None})
    as_friend(client)
    assert client.post("/api/projects", json={"name": "Second"}).status_code == 201
    sign_in(client, "admin", ADMIN_PASSWORD)
    client.patch(f"/api/users/{user['id']}", json={"may_create_projects": False})
    as_friend(client)
    assert client.post("/api/projects", json={"name": "Third"}).status_code == 403
    assert "Third" not in project_names(client)


def test_a_persons_projects_use_only_the_subscriptions_allowed_to_them(client, project):
    claude = next(a for a in client.get("/api/accounts").json() if a["provider"] == "claude_code")
    other = client.post("/api/accounts", json={"provider": "claude_code", "label": "Work", "auth_mode": "oauth_token", "secret": "tok-work"}).json()
    assert client.patch("/api/users/999", json={"account_ids": []}).status_code == 404
    assert client.patch("/api/users/1", json={"account_ids": [12345]}).status_code == 404  # an unknown subscription
    friend(client, account_ids=[other["id"]])
    as_friend(client)
    assert client.post("/api/projects", json={"name": "Pinned", "account_id": claude["id"]}).status_code == 400
    assert "Pinned" not in project_names(client)
    as_friend(client)
    pid = client.post("/api/projects", json={"name": "Mine"}).json()["id"]
    assert client.get(f"/api/projects/{pid}/accounts").json() == [other["id"]]
    assert client.patch(f"/api/projects/{pid}", json={"account_id": claude["id"]}).status_code == 400
    dev = client.post(f"/api/projects/{pid}/agents", json={"name": "Dev", "role": "Developer", "description": "x", "account_id": claude["id"]})
    assert dev.status_code == 400
    dev = client.post(f"/api/projects/{pid}/agents", json={"name": "Dev", "role": "Developer", "description": "x"}).json()
    assert client.post(f"/api/agents/{dev['id']}/messages", json={"text": "SAY hi"}).status_code == 202
    wait_for(lambda: idle(client, pid))
    assert client.app_ref.state.runner.specs[-1].credentials.claude_oauth_token == "tok-work"

    # An admin's project: every subscription, and the friend's limit is no business of the admin's projects.
    sign_in(client, "admin", ADMIN_PASSWORD)
    assert sorted(client.get(f"/api/projects/{project['id']}/accounts").json()) == sorted(a["id"] for a in client.get("/api/accounts").json())


def test_a_persons_projects_run_only_on_the_runners_allowed_to_them(client, project):
    external = client.post("/api/runners", json={"name": "Laptop"}).json()["runner"]
    user = friend(client, runner_ids=[external["id"]])
    as_friend(client)
    pid = client.post("/api/projects", json={"name": "Away"}).json()["id"]
    choices = client.get(f"/api/projects/{pid}/runner").json()
    assert choices["runner_id"] == external["id"] and [r["id"] for r in choices["runners"]] == [external["id"]]  # it starts on the one allowed
    assert client.put(f"/api/projects/{pid}/runner", json={"runner_id": None}).status_code == 403

    sign_in(client, "admin", ADMIN_PASSWORD)
    client.patch(f"/api/users/{user['id']}", json={"runner_ids": [0]})
    as_friend(client)
    assert client.put(f"/api/projects/{pid}/runner", json={"runner_id": None}).json()["runner_id"] is None
    assert [r["id"] for r in client.get(f"/api/projects/{pid}/runner").json()["runners"]] == [None]
    assert client.put(f"/api/projects/{pid}/runner", json={"runner_id": external["id"]}).status_code == 403

    sign_in(client, "admin", ADMIN_PASSWORD)
    client.patch(f"/api/users/{user['id']}", json={"runner_ids": []})
    as_friend(client)
    assert client.post("/api/projects", json={"name": "Nowhere"}).status_code == 403
    assert "Nowhere" not in project_names(client)


def test_a_new_project_starts_on_the_runner_chosen_if_allowed_and_a_refusal_creates_nothing(client, project):
    laptop = client.post("/api/runners", json={"name": "Laptop"}).json()["runner"]
    other = client.post("/api/runners", json={"name": "Other"}).json()["runner"]
    assert [r["id"] for r in client.get("/api/runners/choices").json()] == [None, laptop["id"], other["id"]]  # an admin: all
    friend(client, runner_ids=[0, laptop["id"]])
    as_friend(client)
    choices = client.get("/api/runners/choices").json()
    assert [(r["id"], r["connected"]) for r in choices] == [(None, True), (laptop["id"], False)]
    made = client.post("/api/projects", json={"name": "On laptop", "runner_id": laptop["id"]}).json()
    assert client.get(f"/api/projects/{made['id']}/runner").json()["runner_id"] == laptop["id"]
    made = client.post("/api/projects", json={"name": "Here", "runner_id": None}).json()
    assert client.get(f"/api/projects/{made['id']}/runner").json()["runner_id"] is None
    assert client.post("/api/projects", json={"name": "Forbidden", "runner_id": other["id"]}).status_code == 403
    assert client.post("/api/projects", json={"name": "Unknown", "runner_id": 9999}).status_code == 404
    assert {"Forbidden", "Unknown"}.isdisjoint(project_names(client))


def test_a_persons_limit_of_agents_at_work_holds_across_their_projects(client, project):
    from conftest import FakeRunner

    friend(client, max_parallel=1)
    as_friend(client)
    one, two = (client.post("/api/projects", json={"name": name}).json()["id"] for name in ("One", "Two"))
    assert client.get(f"/api/projects/{one}").json()["parallel_agents"] is None
    devs = [client.post(f"/api/projects/{pid}/agents", json={"name": "Dev", "role": "Developer", "description": "x"}).json() for pid in (one, two)]
    working = lambda: [r["agent_id"] for pid in (one, two) for r in runs_of(client, pid) if r["status"] == "running"]
    for dev in devs:
        assert client.post(f"/api/agents/{dev['id']}/messages", json={"text": "Go on\nHOLD"}).status_code == 202  # HOLD on a line of its own: the name goes in front of the first
    wait_for(lambda: len(working()) == 1)
    assert len(working()) == 1  # each project would have a slot of its own; the person has one
    runner: FakeRunner = client.app_ref.state.runner
    runner.released = True
    wait_for(lambda: idle(client, one) and idle(client, two))
    assert all(any(r["agent_id"] == d["id"] and r["status"] == "succeeded" for pid in (one, two) for r in runs_of(client, pid)) for d in devs)


def test_a_non_admin_owner_shares_their_project_and_its_creator_stays_its_owner(client, project):
    user = friend(client)
    guest = client.post("/api/users", json={"username": "gus", "password": "gus-password"}).json()
    as_friend(client)
    pid = client.post("/api/projects", json={"name": "Shared"}).json()["id"]
    assert client.get("/api/users/directory").status_code == 200
    assert client.put(f"/api/projects/{pid}/members/{guest['id']}", json={"role": "owner"}).status_code == 200
    as_friend(client, "gus")
    assert client.put(f"/api/projects/{pid}/members/{user['id']}", json={"role": "viewer"}).status_code == 400
    assert client.delete(f"/api/projects/{pid}/members/{user['id']}").status_code == 400
    assert client.put(f"/api/projects/{pid}/members/{guest['id']}", json={"role": "member"}).status_code == 200


def test_a_persons_projects_get_the_installations_secrets_and_github_token_only_when_allowed(client, project):
    assert client.put("/api/settings", json={"values": {"github_token": "ghp_installation"}}).status_code == 200
    assert client.post("/api/secrets", json={"name": "CRATES_API_KEY", "value": "global-crates"}).status_code == 201
    mcp = {"name": "shared-docs", "transport": "http", "url": "https://mcp.example", "headers": {"Authorization": "Bearer global"}, "global_scope": True}
    assert client.post(f"/api/projects/{project['id']}/mcp-servers", json=mcp).status_code == 201
    user = friend(client)
    assert user["may_use_shared"] is False
    as_friend(client)
    pid = client.post("/api/projects", json={"name": "Mine"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/secrets", json={"name": "OWN_KEY", "value": "own-value"}).status_code == 201
    assert [s["name"] for s in client.get(f"/api/projects/{pid}/secrets").json()] == ["OWN_KEY"]
    assert client.get(f"/api/projects/{pid}/mcp-servers").json() == []
    assert client.post(f"/api/projects/{pid}/mcp-servers", json={**mcp, "name": "mine"}).status_code == 403
    check = client.post(f"/api/projects/{pid}/check-github").json()
    assert not check["ok"] and "add a GitHub token in the project settings" in check["detail"]
    assert not client.get(f"/api/projects/{pid}/github/repos").json()["ok"]
    dev = client.post(f"/api/projects/{pid}/agents", json={"name": "Dev", "role": "Developer", "description": "x"}).json()
    runner = client.app_ref.state.runner

    def turn() -> object:
        assert client.post(f"/api/agents/{dev['id']}/messages", json={"text": "SAY hi"}).status_code == 202
        wait_for(lambda: idle(client, pid))
        return runner.specs[-1]

    spec = turn()
    assert spec.credentials.secrets == {"OWN_KEY": "own-value"} and spec.credentials.github_token is None
    assert spec.mcp_servers == []

    sign_in(client, "admin", ADMIN_PASSWORD)
    assert client.patch(f"/api/users/{user['id']}", json={"may_use_shared": True}).json()["may_use_shared"] is True
    as_friend(client)
    spec = turn()
    assert spec.credentials.secrets == {"OWN_KEY": "own-value", "CRATES_API_KEY": "global-crates"}
    assert spec.credentials.github_token == "ghp_installation" and [m.name for m in spec.mcp_servers] == ["shared-docs"]

    # Its own token wins, also without the switch.
    sign_in(client, "admin", ADMIN_PASSWORD)
    client.patch(f"/api/users/{user['id']}", json={"may_use_shared": False})
    as_friend(client)
    assert client.patch(f"/api/projects/{pid}", json={"github_token": "ghp_own"}).status_code == 200
    spec = turn()
    assert spec.credentials.github_token == "ghp_own" and "CRATES_API_KEY" not in spec.credentials.secrets
    assert client.post(f"/api/projects/{pid}/check-github").json()["ok"]

    # An admin's project: as before.
    sign_in(client, "admin", ADMIN_PASSWORD)
    admin_dev = client.post(f"/api/projects/{project['id']}/agents", json={"name": "Dev", "role": "Developer", "description": "x"}).json()
    assert client.post(f"/api/agents/{admin_dev['id']}/messages", json={"text": "SAY hi"}).status_code == 202
    wait_for(lambda: idle(client, project["id"]))
    spec = runner.specs[-1]
    assert spec.credentials.secrets["CRATES_API_KEY"] == "global-crates" and spec.credentials.github_token == "ghp_installation"
