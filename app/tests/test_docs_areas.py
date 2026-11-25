"""Areas and groups of a doc site's navigation (#159): order the top-level pages, versioned like pages."""

import json

from conftest import agents_by_slug, idle, wait_for
from test_docs import page, site
from test_docs_api import bearer, make_key
from test_flow import chat, manager_of, tool_results


def test_areas_and_groups_order_the_served_navigation_and_belong_to_their_version(client, project):
    pid = project["id"]
    s = site(client, pid, published=True, public=True)
    sid = s["id"]
    base = f"/api/projects/{pid}/docs"
    intro = page(client, pid, sid, title="Intro")
    install = page(client, pid, sid, title="Install")
    cli = page(client, pid, sid, title="CLI")
    view = client.get("/api/public/docs/handbook").json()
    assert view["areas"] == [] and view["groups"] == [] and view["nav"]["items"][0]["area"] is None  # as before: nothing else changes

    guides = client.post(f"{base}/sites/{sid}/areas", json={"title": "Guides", "description": "How to", "icon": "rocket"}).json()
    ref = client.post(f"{base}/sites/{sid}/areas", json={"title": "Reference", "icon": "code"}).json()
    assert client.post(f"{base}/sites/{sid}/areas", json={"title": "X", "icon": "nope"}).status_code == 400
    start = client.post(f"{base}/sites/{sid}/groups", json={"title": "Start", "area_id": guides["id"]}).json()
    tools = client.post(f"{base}/sites/{sid}/groups", json={"title": "Tools", "area_id": ref["id"], "position": 0}).json()
    assert [g["title"] for g in client.get(f"{base}/sites/{sid}/outline").json()["groups"]] == ["Tools", "Start"]

    # A group puts its pages into its area; only pages at the top have them.
    placed = client.patch(f"{base}/pages/{install['id']}", json={"group_id": start["id"]}).json()
    assert placed["group_id"] == start["id"] and placed["area_id"] == guides["id"]
    assert client.patch(f"{base}/pages/{cli['id']}", json={"area_id": guides["id"], "group_id": tools["id"]}).status_code == 400
    client.patch(f"{base}/pages/{cli['id']}", json={"group_id": tools["id"]})
    below = page(client, pid, sid, title="Flags", parent_id=cli["id"])
    assert client.patch(f"{base}/pages/{below['id']}", json={"area_id": ref["id"]}).status_code == 400
    moved = client.patch(f"{base}/pages/{install['id']}", json={"move": True, "parent_id": intro["id"]}).json()
    assert moved["area_id"] is None and moved["group_id"] is None
    client.patch(f"{base}/pages/{install['id']}", json={"move": True, "parent_id": None, "group_id": start["id"]})
    client.patch(f"{base}/sites/{sid}/areas/{ref['id']}", json={"position": 0, "texts": {"de": {"title": "Referenz"}}})

    view = client.get("/api/public/docs/handbook").json()
    assert [(a["title"], a["icon"]) for a in view["areas"]] == [("Reference", "code"), ("Guides", "rocket")]
    assert view["groups"] == [{"id": tools["id"], "area": ref["id"], "title": "Tools"}, {"id": start["id"], "area": guides["id"], "title": "Start"}]
    assert {i["title"]: (i["area"], i["group"]) for i in view["nav"]["items"]} == {
        "Intro": (None, None), "CLI": (ref["id"], tools["id"]), "Install": (guides["id"], start["id"]),
    }  # fmt: skip
    sidebar = client.get("/api/public/docs/handbook/page", params={"path": "cli/flags"}).json()["sidebar"][0]["items"]
    assert {i["title"]: i["area"] for i in sidebar}["CLI"] == ref["id"]

    # The doc API names them too (#131).
    secret = make_key(client, pid, sid)["key"]
    info = client.get("/api/v1/docs/handbook", headers=bearer(secret)).json()
    assert [a["title"] for a in info["areas"]] == ["Reference", "Guides"] and info["areas"][0]["texts"] == {"de": {"title": "Referenz"}}
    items = {i["title"]: i for i in client.get("/api/v1/docs/handbook/pages", headers=bearer(secret)).json()["items"]}
    assert items["CLI"]["area_id"] == ref["id"] and items["CLI"]["group_id"] == tools["id"] and items["Flags"]["area_id"] is None

    # A cut version keeps them as they were, while the working version changes them.
    v1 = client.post(f"{base}/sites/{sid}/releases", json={"name": "1.0"}).json()
    client.patch(f"{base}/sites/{sid}/areas/{guides['id']}", json={"title": "Tutorials"})
    assert client.delete(f"{base}/sites/{sid}/groups/{tools['id']}").status_code == 204
    assert client.get(f"{base}/pages/{cli['id']}").json()["group_id"] is None  # its pages stay, without it
    assert client.get(f"{base}/pages/{cli['id']}", params={"release": v1["id"]}).json()["group_id"] == tools["id"]
    working = client.get(f"{base}/sites/{sid}/outline").json()
    old = client.get(f"{base}/sites/{sid}/outline", params={"release": v1["id"]}).json()
    assert [a["title"] for a in working["areas"]] == ["Reference", "Tutorials"] and [g["title"] for g in working["groups"]] == ["Start"]
    assert [a["title"] for a in old["areas"]] == ["Reference", "Guides"] and [g["title"] for g in old["groups"]] == ["Tools", "Start"]
    assert {a["id"] for a in working["areas"]} == {a["id"] for a in old["areas"]}  # the same keys in every version
    served = client.get("/api/public/docs/handbook").json()  # visitors get 1.0
    assert [a["title"] for a in served["areas"]] == ["Reference", "Guides"]
    # Corrected in the released version only.
    client.patch(f"{base}/sites/{sid}/areas/{guides['id']}", params={"release": v1["id"]}, json={"description": "Step by step"})
    assert client.get(f"{base}/sites/{sid}/outline").json()["areas"][1]["description"] == "How to"
    # Deleting the version takes what only it had.
    assert client.delete(f"{base}/releases/{v1['id']}").status_code == 204
    assert client.get(f"{base}/sites/{sid}/outline").json() == working


def test_the_doc_agent_orders_its_site_with_areas_and_groups(client, project):
    pid = project["id"]
    manager = manager_of(client, pid)
    chat(client, manager["id"], "TOOL manage_doc_site " + json.dumps({"name": "Guide", "spec": "For users."}))
    wait_for(lambda: idle(client, pid))
    writer = agents_by_slug(client, pid)["docs-guide"]
    assert "doc_outline" in writer["instructions"]
    chat(client, writer["id"], "\n".join([
        "TOOL doc_outline " + json.dumps({"kind": "area", "title": "Reference", "description": "Every command", "icon": "terminal"}),
        "TOOL write_doc_page " + json.dumps({"site": "guide", "title": "Start", "body": "Run it.", "reason": "first"}),
    ]))  # fmt: skip
    wait_for(lambda: idle(client, pid))
    made, written = tool_results(client, writer["id"])[-2:]
    area = int(made.split()[2])
    key = int(written.split()[3])
    chat(client, writer["id"], "TOOL doc_outline " + json.dumps({"kind": "group", "title": "Commands", "area": area}))
    wait_for(lambda: idle(client, pid))
    group_made = tool_results(client, writer["id"])[-1]
    assert group_made.startswith("Created group")
    group = int(group_made.split()[2])
    chat(client, writer["id"], "\n".join([
        "TOOL write_doc_page " + json.dumps({"page": key, "group": group, "reason": "order"}),
        "TOOL list_docs " + json.dumps({"site": "guide"}),
        "TOOL doc_outline " + json.dumps({"kind": "area", "id": area, "delete": True}),
        "TOOL list_docs " + json.dumps({"site": "guide"}),
    ]))  # fmt: skip
    wait_for(lambda: idle(client, pid))
    placed, listed, deleted, after = tool_results(client, writer["id"])[-4:]
    assert "updated (text unchanged)" in placed
    assert f'- area {area} "Reference" (terminal): Every command' in listed and f'- group {group} "Commands" in area {area}' in listed
    assert f'"Start" [area {area} group {group}]' in listed
    assert deleted.startswith(f"Deleted area {area}") and f'- group {group} "Commands"\n' in after and f'"Start" [group {group}]' in after


def test_a_project_moves_with_the_areas_and_groups_of_its_doc_sites(client, project):
    from test_transfer import bring_in, export

    pid = project["id"]
    s = site(client, pid)
    base = f"/api/projects/{pid}/docs"
    area = client.post(f"{base}/sites/{s['id']}/areas", json={"title": "Guides"}).json()
    group = client.post(f"{base}/sites/{s['id']}/groups", json={"title": "Start", "area_id": area["id"]}).json()
    page(client, pid, s["id"], title="Intro", group_id=group["id"])
    resp = bring_in(client, export(client, pid))
    assert resp.status_code == 201, resp.text
    new_pid = resp.json()["project"]["id"] if "project" in resp.json() else resp.json()["id"]
    (copy,) = client.get(f"/api/projects/{new_pid}/docs/sites").json()
    outline = client.get(f"/api/projects/{new_pid}/docs/sites/{copy['id']}/outline").json()
    (new_area,), (new_group,) = outline["areas"], outline["groups"]
    assert new_area["title"] == "Guides" and new_area["id"] != area["id"] and new_group["area_id"] == new_area["id"]
    (intro,) = client.get(f"/api/projects/{new_pid}/docs/sites/{copy['id']}/pages").json()["items"]
    assert (intro["area_id"], intro["group_id"]) == (new_area["id"], new_group["id"])


def test_migration_adds_areas_and_groups_and_goes_back(tmp_path, monkeypatch):
    from alembic import command
    from plainboard import config, migrate
    from sqlalchemy import create_engine, inspect

    monkeypatch.setenv("PLAINBOARD_DATA_DIR", str(tmp_path / "data"))
    config.get_config.cache_clear()
    (tmp_path / "data").mkdir()
    command.upgrade(migrate.alembic_config(), "0099")
    engine = create_engine(config.get_config().sync_database_url)
    names = lambda: (set(inspect(engine).get_table_names()), {c["name"] for c in inspect(engine).get_columns("doc_pages")})  # noqa: E731
    tables, columns = names()
    assert {"doc_areas", "doc_groups"} <= tables and {"area_id", "group_id"} <= columns
    command.downgrade(migrate.alembic_config(), "0098")
    tables, columns = names()
    assert "doc_areas" not in tables and "area_id" not in columns
    command.upgrade(migrate.alembic_config(), "head")
    engine.dispose()
    config.get_config.cache_clear()
