"""Handing a project to another owner (services/ownership.py)."""

from conftest import ADMIN_PASSWORD
from test_own_accounts import TOKEN, inbox
from test_permissions import as_friend, friend, sign_in


def owners(members: list[dict]) -> dict[str, tuple[str, bool]]:
    return {m["username"]: (m["role"], m["project_owner"]) for m in members}


def test_the_owner_hands_the_project_to_a_member_and_what_they_may_not_use_chooses_again(client, project):
    bob = friend(client, "bob", max_parallel=2)
    gus = friend(client, "gus")
    carl = friend(client, "carl")
    olga = friend(client)
    as_friend(client)
    own = client.post("/api/accounts", json={**TOKEN, "label": "Olga's", "secret": "tok-olga"}).json()
    pid = client.post("/api/projects", json={"name": "Handed", "account_id": own["id"]}).json()["id"]
    dev = client.post(f"/api/projects/{pid}/agents", json={"name": "Dev", "role": "Developer", "description": "x", "account_id": own["id"]}).json()
    for user, role in ((bob, "member"), (gus, "owner")):
        assert client.put(f"/api/projects/{pid}/members/{user['id']}", json={"role": role}).status_code == 200
    assert owners(client.get(f"/api/projects/{pid}/members").json())["olga"] == ("owner", True)
    assert client.get(f"/api/projects/{pid}/owner/{carl['id']}").status_code == 400  # not a member

    as_friend(client, "gus")  # an owner by role, not the project's owner
    assert client.get(f"/api/projects/{pid}/owner/{bob['id']}").status_code == 403
    assert client.put(f"/api/projects/{pid}/owner/{gus['id']}").status_code == 403

    as_friend(client)
    preview = client.get(f"/api/projects/{pid}/owner/{bob['id']}").json()
    assert preview["lost_accounts"] == ["Olga's"] and preview["unpinned"] == ["the project", "Dev"]
    assert preview["runner_allowed"] and not preview["loses_shared"]
    assert preview["max_parallel_before"] is None and preview["max_parallel_after"] == 2
    resp = client.put(f"/api/projects/{pid}/owner/{bob['id']}")
    assert resp.status_code == 200, resp.text
    assert owners(resp.json()) == {"bob": ("owner", True), "gus": ("owner", False), "olga": ("owner", False)}
    assert client.put(f"/api/projects/{pid}/owner/{olga['id']}").status_code == 403  # no longer hers to hand over

    as_friend(client, "bob")
    assert client.get(f"/api/projects/{pid}").json()["account_id"] is None
    assert next(a for a in client.get(f"/api/projects/{pid}/agents").json() if a["id"] == dev["id"])["account_id"] is None
    assert own["id"] not in client.get(f"/api/projects/{pid}/accounts").json()
    assert [t for t in inbox(client, pid) if "now belongs to" in t] == [
        "This project now belongs to bob, who may not use the subscription chosen for the project, Dev: it now chooses a subscription automatically."
    ]
    assert client.put(f"/api/projects/{pid}/members/{olga['id']}", json={"role": "viewer"}).status_code == 200  # no longer kept as owner


def test_a_runner_the_new_owner_may_not_use_refuses_the_change(client, project):
    external = client.post("/api/runners", json={"name": "Laptop"}).json()["runner"]
    bob = friend(client, "bob", runner_ids=[0])
    friend(client, runner_ids=[external["id"]])
    as_friend(client)
    pid = client.post("/api/projects", json={"name": "Away"}).json()["id"]
    client.put(f"/api/projects/{pid}/members/{bob['id']}", json={"role": "member"})
    preview = client.get(f"/api/projects/{pid}/owner/{bob['id']}").json()
    assert preview["runner"] == "Laptop" and not preview["runner_allowed"]
    for _ in ("owner", "admin"):
        refused = client.put(f"/api/projects/{pid}/owner/{bob['id']}")
        assert refused.status_code == 400 and "Move the project to a runner" in refused.json()["detail"]
        sign_in(client, "admin", ADMIN_PASSWORD)
    assert owners(client.get(f"/api/projects/{pid}/members").json())["olga"] == ("owner", True)


def test_an_admin_gives_a_project_of_nobody_an_owner(client, project):
    olga = friend(client)
    bob = friend(client, "bob")
    as_friend(client)
    pid = client.post("/api/projects", json={"name": "Orphan"}).json()["id"]
    sign_in(client, "admin", ADMIN_PASSWORD)
    assert client.delete(f"/api/users/{olga['id']}").status_code == 204
    assert not any(m["project_owner"] for m in client.get(f"/api/projects/{pid}/members").json())
    as_friend(client, "bob")
    assert client.get(f"/api/projects/{pid}/owner/{bob['id']}").status_code == 404  # not his project
    sign_in(client, "admin", ADMIN_PASSWORD)
    assert client.get(f"/api/projects/{pid}/owner/{bob['id']}").json()["max_parallel_before"] is None
    resp = client.put(f"/api/projects/{pid}/owner/{bob['id']}")  # not a member yet: an admin may
    assert resp.status_code == 200 and owners(resp.json()) == {"bob": ("owner", True)}
    as_friend(client, "bob")
    assert client.get(f"/api/projects/{pid}").status_code == 200
