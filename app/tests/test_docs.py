"""Doc sites (#118): pages with versions, files that stay, agent tools, public serving, own domains, keeping docs current."""

import json
from datetime import timedelta

import httpx
import pytest
from conftest import POSTGRES, agents_by_slug, idle, wait_for
from plainboard_protocol import GitCommit
from test_flow import chat, make_member, manager_of, runner, tool_results
from test_permissions import as_friend, friend, sign_in

from conftest import ADMIN_PASSWORD


def site(client, pid: int, **body) -> dict:
    resp = client.post(f"/api/projects/{pid}/docs/sites", json={"name": "Handbook", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


def page(client, pid: int, site_id: int, **body) -> dict:
    resp = client.post(f"/api/projects/{pid}/docs/sites/{site_id}/pages", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def db(client, fn):
    from plainboard.db import sessionmaker

    async def run():
        async with sessionmaker()() as s:
            return await fn(s)

    return client.portal.call(run)


def test_pages_form_a_tree_keep_every_version_and_restore_one(client, project):
    pid = project["id"]
    s = site(client, pid, spec="For admins. Short pages, English.")
    assert s["slug"] == "handbook" and s["url"] == "/docs/handbook/" and not s["published"]
    start = page(client, pid, s["id"], title="Getting started", body="# Install\n\nRun it.", reason="first page")
    setup = page(client, pid, s["id"], title="Setup", parent_id=start["id"])
    again = page(client, pid, s["id"], title="Setup", parent_id=start["id"])
    assert (setup["path"], again["path"]) == ("getting-started/setup", "getting-started/setup-2")
    first = page(client, pid, s["id"], title="Overview", position=0)
    top = client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/pages").json()
    assert [(p["title"], p["children"]) for p in top["items"]] == [("Overview", 0), ("Getting started", 2)] and top["total"] == 2  # by position
    below = client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/pages", params={"parent": start["id"]}).json()["items"]
    assert [(p["title"], p["depth"]) for p in below] == [("Setup", 1), ("Setup", 1)]

    # A move is no version; a text change is one, with who and why.
    moved = client.patch(f"/api/projects/{pid}/docs/pages/{again['id']}", json={"move": True, "parent_id": None, "position": 0}).json()
    assert moved["path"] == "setup-2" and moved["versions"] == 1  # its slug stays
    assert [p["position"] for p in client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/pages").json()["items"]] == [0, 1, 2]
    deep = client.patch(f"/api/projects/{pid}/docs/pages/{start['id']}", json={"move": True, "parent_id": first["id"]}).json()
    assert deep["path"] == "overview/getting-started" and deep["ancestors"] == [{"id": first["id"], "title": "Overview", "path": "overview"}]
    assert client.get(f"/api/projects/{pid}/docs/pages/{setup['id']}").json()["path"] == "overview/getting-started/setup"  # the pages below it moved along
    assert client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/pages/by-path", params={"path": "overview/getting-started/setup"}).json()["id"] == setup["id"]
    assert [p["id"] for p in client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/pages/find", params={"q": "getting"}).json()][0] == start["id"]
    assert client.patch(f"/api/projects/{pid}/docs/pages/{start['id']}", json={"move": True, "parent_id": setup["id"]}).status_code == 400  # not below itself
    changed = client.patch(f"/api/projects/{pid}/docs/pages/{start['id']}", json={"body": "# Install\n\nRun `pb up`.", "reason": "new command"}).json()
    assert changed["versions"] == 2 and changed["edited_by"] == "Admin"
    versions = client.get(f"/api/projects/{pid}/docs/pages/{start['id']}/versions").json()
    assert [(v["reason"], v["body"]) for v in versions] == [("new command", None), ("first page", None)]
    restored = client.post(f"/api/projects/{pid}/docs/pages/{start['id']}/versions/{versions[1]['id']}/restore").json()
    assert restored["body"] == "# Install\n\nRun it." and restored["versions"] == 3
    assert client.get(f"/api/projects/{pid}/docs/pages/{start['id']}/versions").json()[0]["reason"].startswith("restored the version of")

    assert client.delete(f"/api/projects/{pid}/docs/pages/{start['id']}").status_code == 204  # with the page below it
    assert [p["id"] for p in client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/pages").json()["items"]] == [again["id"], first["id"]]
    assert client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/pages", params={"parent": first["id"]}).json()["total"] == 0
    assert client.post(f"/api/projects/{pid}/docs/sites", json={"name": "Other", "slug": "handbook"}).status_code == 409


def test_viewers_read_members_write_and_only_the_owner_sets_the_domain(client, project):
    pid = project["id"]
    s = site(client, pid)
    olga, ben = friend(client, "olga"), friend(client, "ben")
    assert client.put(f"/api/projects/{pid}/members/{olga['id']}", json={"role": "viewer"}).status_code == 200
    assert client.put(f"/api/projects/{pid}/members/{ben['id']}", json={"role": "member"}).status_code == 200
    as_friend(client, "olga")
    assert client.get(f"/api/projects/{pid}/docs/sites").status_code == 200
    assert client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/pages", json={"title": "X"}).status_code == 403
    as_friend(client, "ben")
    assert client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/pages", json={"title": "X"}).status_code == 201
    assert client.put(f"/api/projects/{pid}/docs/sites/{s['id']}/domain", json={"domain": "docs.example.com"}).status_code == 403
    sign_in(client, "admin", ADMIN_PASSWORD)


def test_doc_files_are_never_deleted_by_the_retention(client, project):
    from plainboard.config import get_config
    from plainboard.models import Attachment, TaskTransition, utcnow
    from plainboard.services import retention
    from sqlalchemy import update

    pid = project["id"]
    s = site(client, pid)
    (picture,) = client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/files", files=[("files", ("arch.png", b"\x89PNG\r\n\x1a\nxx", "image/png"))]).json()
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "Old"}).json()
    (old,) = client.post(f"/api/tasks/{task['id']}/attachments", files=[("files", ("old.png", b"x", "image/png"))]).json()
    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})
    assert client.patch(f"/api/projects/{pid}", json={"attachment_retention_days": 1}).status_code == 200

    async def purge(s):
        await s.execute(update(TaskTransition).where(TaskTransition.project_id == pid).values(at=utcnow() - timedelta(days=30)))
        await s.commit()
        await s.execute(update(Attachment).where(Attachment.id == picture["id"]).values(created_at=utcnow() - timedelta(days=4000)))
        await s.commit()
        done = await retention.purge(s, utcnow())
        return [d.attachment.id for d in done.get(pid, [])], (await s.get(Attachment, picture["id"])).storage_name

    purged, name = db(client, purge)
    assert purged == [old["id"]]
    assert (get_config().uploads_dir / name).is_file()
    assert [f["id"] for f in client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/files").json()] == [picture["id"]]


def test_a_public_site_is_served_without_login_as_html_json_and_search(client, project, monkeypatch):
    pid = project["id"]
    s = site(client, pid, description="How to run the shop")
    (picture,) = client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/files", files=[("files", ("arch.png", b"\x89PNG\r\n\x1a\nxx", "image/png"))]).json()
    home = page(client, pid, s["id"], title="Welcome", body="Start here.")
    api = page(client, pid, s["id"], title="Payments API", parent_id=home["id"], body=f"## Webhooks\n\nRetries with backoff. ![Diagram](doc-file:{picture['id']}) See [home](doc-page:{home['id']}).\n\n<script>alert(1)</script>")
    other = site(client, pid, name="Internal")
    (secret,) = client.post(f"/api/projects/{pid}/docs/sites/{other['id']}/files", files=[("files", ("s.png", b"\x89PNG\r\n\x1a\nyy", "image/png"))]).json()
    client.cookies.clear()  # nobody signed in

    assert client.get("/docs/handbook/").status_code == 404  # a draft is not there
    assert client.get("/api/public/docs/handbook").status_code == 404
    sign_in(client, "admin", ADMIN_PASSWORD)
    client.patch(f"/api/projects/{pid}/docs/sites/{s['id']}", json={"published": True, "public": True})
    client.patch(f"/api/projects/{pid}/docs/sites/{other['id']}", json={"published": True, "public": True})
    client.cookies.clear()

    view = client.get("/api/public/docs/handbook").json()
    assert view["name"] == "Handbook" and view["home"] == "welcome" and view["base"] == "/docs/handbook"
    assert view["nav"] == {"items": [{"id": home["id"], "title": "Welcome", "path": "welcome", "children": 1, "area": None, "group": None}], "total": 1, "offset": 0}
    assert client.get("/api/public/docs/handbook/nav", params={"parent": home["id"]}).json()["items"] == [{"id": api["id"], "title": "Payments API", "path": "welcome/payments-api", "children": 0}]
    got = client.get("/api/public/docs/handbook/page", params={"path": "welcome/payments-api"}).json()
    assert got["toc"] == [{"level": 2, "id": "webhooks", "text": "Webhooks"}] and got["breadcrumbs"] == [{"title": "Welcome", "path": "welcome"}]
    assert f'src="/api/public/docs/handbook/files/{picture["id"]}"' in got["html"] and 'href="/docs/handbook/welcome"' in got["html"]
    assert "<script>" not in got["html"] and got["prev"] == {"title": "Welcome", "path": "welcome"} and got["next"] is None
    assert [len(level["items"]) for level in got["sidebar"]] == [1, 1]
    again = client.get("/api/public/docs/handbook/page", params={"path": "welcome/payments-api"}, headers={"if-none-match": client.get("/api/public/docs/handbook/page", params={"path": "welcome/payments-api"}).headers["etag"]})
    assert again.status_code == 304
    assert client.get("/api/public/docs/handbook/page", params={"path": ""}).json()["title"] == "Welcome"

    html = client.get("/docs/handbook/welcome/payments-api")
    assert html.status_code == 200 and "<title>Payments API – Handbook</title>" in html.text and 'id="webhooks"' in html.text
    assert 'rel="canonical" href="http://testserver/docs/handbook/welcome/payments-api"' in html.text and "window.__PB_DOCS__=" in html.text
    assert client.get("/docs/handbook/nothing-here").status_code == 404
    assert "<loc>http://testserver/docs/handbook/welcome/payments-api</loc>" in client.get("/docs/handbook/sitemap.xml").text
    assert client.get(f"/api/public/docs/handbook/files/{picture['id']}").content.startswith(b"\x89PNG")
    assert client.get(f"/api/public/docs/handbook/files/{secret['id']}").status_code == 404  # another site's file

    hits = client.get("/api/public/docs/handbook/search", params={"q": "webhook retries"}).json()
    assert hits[0]["title"] == "Payments API" and "Retries" in hits[0]["snippet"]
    from plainboard.api import docs_public
    from plainboard.services.docs_public import RateLimit

    monkeypatch.setattr(docs_public, "SEARCHES", RateLimit(1))
    client.get("/api/public/docs/handbook/search", params={"q": "x"})
    assert client.get("/api/public/docs/handbook/search", params={"q": "x"}).status_code == 429
    sign_in(client, "admin", ADMIN_PASSWORD)


def test_a_site_at_its_own_domain_is_served_there_only_when_verified(client, project, monkeypatch):
    from plainboard.services import docs_domains

    pid = project["id"]
    s = site(client, pid, published=True, public=True)
    page(client, pid, s["id"], title="Welcome", body="Hello from the docs.")
    assert client.put(f"/api/projects/{pid}/docs/sites/{s['id']}/domain", json={"domain": "testserver"}).status_code == 400  # PlainBoard's own host
    setup = client.put(f"/api/projects/{pid}/docs/sites/{s['id']}/domain", json={"domain": "https://Docs.Example.com/"}).json()
    assert setup["domain"] == "docs.example.com" and setup["target"] == "testserver" and setup["verified_at"] is None
    host = {"host": "docs.example.com"}
    assert client.get("/.well-known/plainboard-docs", headers=host).text == setup["token"]
    assert client.get("/", headers=host).status_code == 404  # not verified yet

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "docs.example.com"
        return httpx.Response(200, text=setup["token"])

    real = httpx.AsyncClient
    monkeypatch.setattr(docs_domains.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(answer), **kw))
    assert client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/domain/verify").json()["verified"] is True

    client.cookies.clear()
    home = client.get("/", headers=host)
    assert home.status_code == 200 and "Hello from the docs." in home.text and 'href="http://docs.example.com/"' in home.text
    assert client.get("/api/public/docs/handbook", headers=host).json()["base"] == ""
    assert client.get("/api/projects", headers=host).status_code == 404  # nothing else of PlainBoard there
    assert client.get("/api/auth/me", headers=host).status_code == 404
    sign_in(client, "admin", ADMIN_PASSWORD)


def test_cloudflare_creates_the_cname_and_the_token_is_never_shown(client, project, monkeypatch):
    from plainboard.services import docs_domains

    pid = project["id"]
    s = site(client, pid)
    client.put(f"/api/projects/{pid}/docs/sites/{s['id']}/domain", json={"domain": "docs.shop.example.com"})
    seen = []

    def cloudflare(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params), request.headers["authorization"]))
        if request.url.path == "/client/v4/zones":
            found = [{"id": "z1", "name": "example.com"}] if request.url.params["name"] == "example.com" else []
            return httpx.Response(200, json={"success": True, "result": found})
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "result": []})
        body = json.loads(request.content)
        assert body == {"type": "CNAME", "name": "docs.shop.example.com", "content": "board.example.org", "proxied": True, "ttl": 1, "comment": "PlainBoard doc site"}
        return httpx.Response(200, json={"success": True, "result": {"id": "r1"}})

    real = httpx.AsyncClient
    monkeypatch.setattr(docs_domains.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(cloudflare), **kw))
    resp = client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/domain/cloudflare", json={"token": "cf-secret-token", "target": "board.example.org"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"zone": "example.com", "record": "docs.shop.example.com", "target": "board.example.org", "created": True}
    assert [p for _, p, params, _ in seen][:2] == ["/client/v4/zones", "/client/v4/zones"] and seen[-1][0] == "POST" and seen[-1][3] == "Bearer cf-secret-token"
    shown = client.get(f"/api/projects/{pid}/docs/sites/{s['id']}").text + client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/domain").text
    assert "cf-secret-token" not in shown and '"cloudflare_token_set":true' in shown


def test_each_site_has_its_own_hidden_doc_agent_that_alone_writes_it(client, project):
    pid = project["id"]
    manager = manager_of(client, pid)
    dev = make_member(client, pid, "Dev")
    chat(client, manager["id"], "TOOL manage_doc_site " + json.dumps({"name": "Guide", "spec": "Audience: shop owners. Tone: friendly.", "agent_model": "claude-sonnet-x", "agent_effort": "low", "domain_help": True}))
    wait_for(lambda: idle(client, pid))
    created = tool_results(client, manager["id"])[-1]
    assert created.startswith("Created doc site guide (id ") and "Its doc agent: docs-guide (claude-sonnet-x, effort low)." in created and "Cloudflare API token" in created
    guide = client.get(f"/api/projects/{pid}/docs/sites").json()[0]
    writer = agents_by_slug(client, pid)["docs-guide"]
    assert guide["agent"]["id"] == writer["id"] and writer["doc_site_id"] == guide["id"] and "Audience: shop owners." in writer["instructions"]
    other = site(client, pid, name="Other")

    # Hidden from the team: list_agents, the team in every prompt.
    chat(client, manager["id"], "TOOL list_agents {}")
    wait_for(lambda: idle(client, pid))
    assert "docs-guide" not in tool_results(client, manager["id"])[-1] and "docs-guide" not in runner(client).specs[-1].system_prompt

    # Everyone reads, only the doc agent writes, and only its own site.
    chat(client, dev["id"], "TOOL write_doc_page " + json.dumps({"site": "guide", "title": "Orders", "reason": "first page"}))
    wait_for(lambda: idle(client, pid))
    assert tool_results(client, dev["id"])[-1] == "Tool write_doc_page is not available to you"
    dev_tools = {t.name for t in runner(client).specs[-1].bridge.tools}
    assert {"list_docs", "read_doc_page"} <= dev_tools and not dev_tools & {"write_doc_page", "attach_doc_file", "manage_doc_site", "order_docs"}
    chat(client, writer["id"], "\n".join([
        "TOOL write_doc_page " + json.dumps({"site": "guide", "title": "Orders", "body": "Orders arrive here.", "reason": "first page", "paths": ["app/orders"]}),
        "TOOL write_doc_page " + json.dumps({"site": "other", "title": "X", "reason": "not mine"}),
    ]))  # fmt: skip
    wait_for(lambda: idle(client, pid))
    written, refused = tool_results(client, writer["id"])[-2:]
    assert written.startswith("Created doc page ") and refused == "Only the doc agent of other writes it."
    assert {"write_doc_page", "attach_doc_file", "list_docs"} <= {t.name for t in runner(client).specs[-1].bridge.tools}
    page_id = int(written.split()[3])
    chat(client, dev["id"], "\n".join(["TOOL list_docs {\"site\": \"guide\"}", "TOOL read_doc_page " + json.dumps({"page": page_id})]))
    wait_for(lambda: idle(client, pid))
    listed, read = tool_results(client, dev["id"])[-2:]
    assert f'- {page_id} /orders "Orders"' in listed and "paths: app/orders" in listed and "Doc spec" not in listed and "Orders arrive here." in read

    # The manager configures it and orders work; the agent goes with its site.
    chat(client, manager["id"], "\n".join([
        "TOOL manage_doc_site " + json.dumps({"site": "guide", "spec": "Audience: developers.", "agent_model": "claude-opus-x"}),
        "TOOL order_docs " + json.dumps({"site": "guide", "title": "First version", "instructions": "Write the first version of the guide."}),
    ]))  # fmt: skip
    wait_for(lambda: idle(client, pid))
    saved, ordered = tool_results(client, manager["id"])[-2:]
    assert "claude-opus-x" in saved and ordered.startswith("Assignment #") and "doc agent of guide (docs-guide)" in ordered
    writer = agents_by_slug(client, pid)["docs-guide"]
    assert writer["model"] == "claude-opus-x" and "Audience: developers." in writer["instructions"]
    assert [a["agent_id"] for a in client.get(f"/api/projects/{pid}/assignments").json() if a["title"] == "First version"] == [writer["id"]]
    assert client.delete(f"/api/projects/{pid}/docs/sites/{guide['id']}").status_code == 204
    assert "docs-guide" not in agents_by_slug(client, pid) and "docs-other" in agents_by_slug(client, pid)
    assert other["agent"]["slug"] == "docs-other"


def test_the_doc_agent_gets_once_per_burst_the_pages_a_landing_may_have_made_outdated(client, project):
    from plainboard.models import Assignment, DocPage, InboxItem, Landing, LandingStatus, utcnow
    from sqlalchemy import select

    pid = project["id"]
    client.patch(f"/api/projects/{pid}", json={"auto_wake_manager": False})
    repo = client.post(f"/api/projects/{pid}/repositories", json={"name": "api", "url": "https://github.com/acme/api.git"}).json()
    s = site(client, pid)
    writer = s["agent"]["id"]
    pay = page(client, pid, s["id"], title="Payments", body="How payments work.", paths=["app/pay", "docs/*.md"])
    other = page(client, pid, s["id"], title="Orders", body="Orders.", paths=["app/orders"])
    fake = runner(client)
    fake.commits = [GitCommit(sha="a" * 40, message="Start", files=["README.md"])]
    watcher = client.app_ref.state.docs_watcher
    now = utcnow()
    client.portal.call(watcher.tick, now)  # starts watching from here
    assert fake.git_log_specs[-1].since is None and fake.git_log_specs[-1].limit == 1

    async def land(s, at, branch):
        s.add(Landing(project_id=pid, repository_id=repo["id"], branch=branch, title="Retry webhooks", status=LandingStatus.MERGED, merged_at=at))
        await s.commit()

    db(client, lambda s: land(s, now + timedelta(minutes=1), "be/retries"))
    fake.commits = [GitCommit(sha="c" * 40, message="Retry webhooks", files=["app/pay/webhooks.py", "app/pay/x.py"]), GitCommit(sha="b" * 40, message="Doc", files=["docs/pay.md"]), *fake.commits]
    client.portal.call(watcher.tick, now + timedelta(minutes=5))  # still landing: nothing yet
    db(client, lambda s: land(s, now + timedelta(minutes=6), "be/more"))
    client.portal.call(watcher.tick, now + timedelta(minutes=20))
    wait_for(lambda: idle(client, pid))

    async def orders(s):
        return [a.instructions for a in await s.scalars(select(Assignment).where(Assignment.agent_id == writer).order_by(Assignment.id))]

    manager = agents_by_slug(client, pid)["manager"]

    async def manager_inbox(s):
        return [i.text for i in await s.scalars(select(InboxItem).where(InboxItem.agent_id == manager["id"]))]

    texts = db(client, orders)
    assert len(texts) == 1, texts
    assert "What landed:\n- api Retry webhooks\n- api Retry webhooks\n- commits in api: Retry webhooks; Doc" in texts[0] and "list_docs query" in texts[0]
    assert "api: be/retries; be/more" in texts[0] and f'doc page {pay["id"]} "Payments" (/payments): its linked code changed: app/pay/webhooks.py, app/pay/x.py, docs/pay.md (2 commits)' in texts[0]
    assert "Orders" not in texts[0] and fake.git_log_specs[-1].since == "a" * 40
    assert not db(client, manager_inbox)  # the manager is not involved, not even by the report
    flagged = client.get(f"/api/projects/{pid}/docs/pages/{pay['id']}").json()
    assert flagged["flagged_at"] and flagged["drift"] == 2 and client.get(f"/api/projects/{pid}/docs/sites/{s['id']}").json()["stale_pages"] == 1
    client.portal.call(watcher.tick, now + timedelta(minutes=40))
    assert len(db(client, orders)) == 1  # nothing new landed

    cleared = client.post(f"/api/projects/{pid}/docs/pages/{pay['id']}/confirm").json()
    assert cleared["flagged_at"] is None and cleared["drift"] == 0

    # Daily: direct pushes count into the drift; a page whose code changed a lot is named, once a week.
    async def drift(s):
        (await s.get(DocPage, other["id"])).drift = 12
        await s.commit()

    db(client, drift)
    client.portal.call(watcher.tick, now + timedelta(days=2))
    wait_for(lambda: idle(client, pid))
    stale = [t for t in db(client, orders) if t.startswith("Pages of your doc site that are probably stale")]
    assert len(stale) == 1 and f'doc page {other["id"]} "Orders"' in stale[0] and "12 commits changed its linked code" in stale[0]
    client.portal.call(watcher.tick, now + timedelta(days=3, hours=1))
    assert len([t for t in db(client, orders) if t.startswith("Pages of your doc site that are probably stale")]) == 1


def test_a_project_moves_with_its_doc_sites_files_and_links(client, project):
    from test_transfer import bring_in, export

    pid = project["id"]
    s = site(client, pid, spec="Be brief.", published=True, public=True, languages=["de"])
    client.put(f"/api/projects/{pid}/docs/sites/{s['id']}/domain", json={"domain": "docs.example.com"})
    (picture,) = client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/files", files=[("files", ("a.png", b"\x89PNG\r\n\x1a\nzz", "image/png"))]).json()
    top = page(client, pid, s["id"], title="Top", body="top")
    below = page(client, pid, s["id"], title="Below", parent_id=top["id"], body=f"![a](doc-file:{picture['id']}) [up](doc-page:{top['id']})")
    resp = bring_in(client, export(client, pid))
    assert resp.status_code == 201, resp.text
    new_pid = resp.json()["project"]["id"] if "project" in resp.json() else resp.json()["id"]
    (copy,) = client.get(f"/api/projects/{new_pid}/docs/sites").json()
    assert copy["slug"] == "handbook-2" and copy["spec"] == "Be brief." and copy["domain"] is None  # this installation serves the original
    pages = client.get(f"/api/projects/{new_pid}/docs/sites/{copy['id']}/pages").json()["items"]
    pages += client.get(f"/api/projects/{new_pid}/docs/sites/{copy['id']}/pages", params={"parent": pages[0]["id"]}).json()["items"]
    assert [p["path"] for p in pages] == ["top", "top/below"] and copy["languages"] == ["de"]
    (new_file,) = client.get(f"/api/projects/{new_pid}/docs/sites/{copy['id']}/files").json()
    body = client.get(f"/api/projects/{new_pid}/docs/pages/{pages[1]['id']}").json()["body"]
    assert body == f"![a](doc-file:{new_file['id']}) [up](doc-page:{pages[0]['id']})" and new_file["id"] != picture["id"]
    assert below["id"] != pages[1]["id"]


@pytest.mark.skipif(not POSTGRES, reason="semantic search needs PostgreSQL with pgvector")
def test_doc_pages_are_found_by_agents_people_and_the_public_search(client, project, monkeypatch):
    from test_search import FakeEmbedder, index, search

    from plainboard.services.search import embedding

    monkeypatch.setattr(embedding, "_current", {embedding.DEFAULT: FakeEmbedder()})
    pid = project["id"]
    s = site(client, pid, published=True, public=True)
    found = page(client, pid, s["id"], title="Webhooks", body="Stripe retries webhooks with exponential backoff for six hours.")
    index(client)
    result = search(client, manager_of(client, pid)["id"], pid, query="stripe webhook backoff", kinds=["doc"])
    assert f"doc page {found['id']} doc" in result and "Webhooks" in result
    palette = client.get(f"/api/projects/{pid}/search", params={"q": "stripe webhook backoff"}).json()
    assert [h.get("doc_page_id") for h in palette["hits"] if h["kind"] == "doc"] == [found["id"]], palette
    assert client.get("/api/public/docs/handbook/search", params={"q": "exponential backoff"}).json()[0]["title"] == "Webhooks"
    assert client.delete(f"/api/projects/{pid}/docs/pages/{found['id']}").status_code == 204
    assert "Nothing found." in search(client, manager_of(client, pid)["id"], pid, query="stripe webhook backoff", kinds=["doc"])


def test_a_private_site_is_only_for_people_of_its_project_also_at_its_domain(client, project, monkeypatch):
    from plainboard.models import DocSite, utcnow
    from plainboard.services import docs_domains

    pid = project["id"]
    s = site(client, pid, published=True)  # published, private
    page(client, pid, s["id"], title="Internals", body="Secret architecture.")
    client.put(f"/api/projects/{pid}/docs/sites/{s['id']}/domain", json={"domain": "intern.example.com"})

    async def verified(session):
        (await session.get(DocSite, s["id"])).domain_verified_at = utcnow()
        await session.commit()

    db(client, verified)
    docs_domains.forget()
    assert client.get("/api/public/docs/handbook/page", params={"path": ""}).json()["title"] == "Internals"  # the admin
    assert client.get("/docs/handbook/").headers["cache-control"] == "private, no-store"
    olga = friend(client, "olga")
    friend(client, "stranger")
    client.put(f"/api/projects/{pid}/members/{olga['id']}", json={"role": "viewer"})
    client.cookies.clear()
    assert client.get("/api/public/docs/handbook").status_code == 401
    shown = client.get("/docs/handbook/internals")
    assert shown.status_code == 401 and "Secret architecture" not in shown.text and "/api/auth/login" in shown.text
    assert client.get("/api/public/docs/handbook/search", params={"q": "secret"}).status_code == 401
    as_friend(client, "stranger")
    assert client.get("/api/public/docs/handbook").status_code == 404
    as_friend(client, "olga")
    assert "Secret architecture" in client.get("/docs/handbook/internals").text

    client.cookies.clear()  # at its own domain: signs in there, with a session cookie of that domain
    host = {"host": "intern.example.com"}
    assert client.get("/internals", headers=host).status_code == 401
    assert client.post("/api/auth/login", json={"username": "olga", "password": "olga-password"}, headers=host).status_code == 200
    assert "Secret architecture" in client.get("/internals", headers=host).text
    assert client.get("/api/projects", headers=host).status_code == 404
    sign_in(client, "admin", ADMIN_PASSWORD)


def test_markdown_keeps_its_shape_through_a_translation_api():
    from plainboard.services import docs_translate as t

    pieces = t.blocks("# Title\n\nSee `pb up` and [docs](https://x.io/a).\n\n```sh\nrm -rf /\n```\n\n| a | b |\n- one\n- two")
    assert pieces[4] == (False, "```sh\nrm -rf /\n```")
    text, kept = t.protect(pieces[2][1], lambda i: f"<x>{i}</x>")
    assert "pb up" not in text and "https" not in text and kept == ["`pb up`", "](https://x.io/a)"]
    assert t.restore(text.replace("See", "Siehe").replace("and", "und"), kept, t._DEEPL_TAG) == "Siehe `pb up` und [docs](https://x.io/a)."
    text, kept = t.protect("- one\n- two & <b>", lambda i: f'<span translate="no">{i}</span>')
    assert t.restore(text, kept, t._GOOGLE_TAG) == "- one\n- two & <b>"


def test_pages_are_translated_kept_in_sync_and_served_per_language(client, project, monkeypatch):
    from plainboard.services import docs_translate

    pid = project["id"]
    s = site(client, pid, published=True, public=True, languages=["de", "fr"])
    assert s["language"] == "en" and s["languages"] == ["de", "fr"]
    home = page(client, pid, s["id"], title="Welcome", body="Hello world.\n\n```\ncode stays\n```")
    sub = page(client, pid, s["id"], title="Payments", parent_id=home["id"], body="Payments are retried.")
    asked = []

    def deepl(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append(body)
        assert request.headers["authorization"] == "DeepL-Auth-Key key-1:fx" and request.url.host == "api-free.deepl.com"
        words = {"Welcome": "Willkommen", "Hello world.": "Hallo Welt.", "Payments": "Zahlungen", "Payments are retried.": "Zahlungen werden wiederholt.", "Payments are retried twice.": "Zahlungen werden zweimal wiederholt."}
        return httpx.Response(200, json={"translations": [{"text": words.get(x, x)} for x in body["text"]]})

    monkeypatch.setattr(client.app_ref.state.docs_watcher, "translate_soon", lambda: None)  # the test looks itself
    real = httpx.AsyncClient
    monkeypatch.setattr(docs_translate.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(deepl), **kw))
    assert client.patch(f"/api/projects/{pid}/docs/sites/{s['id']}", json={"translator": "deepl", "languages": ["de"]}).status_code == 200
    out = client.put(f"/api/projects/{pid}/docs/sites/{s['id']}/translator-keys", json={"deepl": "key-1:fx"}).json()
    assert out["deepl_key_set"] is True and "key-1" not in json.dumps(out)
    watcher = client.app_ref.state.docs_watcher
    from plainboard.models import Project, utcnow

    async def look(session):
        return await watcher.translate(session, await session.get(Project, pid), utcnow())

    assert db(client, look) == 2 and db(client, look) == 0  # nothing left to do
    assert all(b["target_lang"] == "DE" and b["tag_handling"] == "xml" for b in asked) and not any("code stays" in x for b in asked for x in b["text"])
    states = client.get(f"/api/projects/{pid}/docs/pages/{sub['id']}/translations").json()
    assert [(x["lang"], x["state"], x["title"], x["translated_by"]) for x in states] == [("de", "current", "Zahlungen", "deepl")]

    got = client.get("/api/public/docs/handbook/page", params={"path": "welcome/payments", "lang": "de"}).json()
    assert got["title"] == "Zahlungen" and "Zahlungen werden wiederholt." in got["html"] and got["breadcrumbs"] == [{"title": "Willkommen", "path": "welcome"}]
    html = client.get("/docs/handbook/de/welcome/payments").text
    assert '<html lang="de">' in html and "Zahlungen werden wiederholt." in html and 'hreflang="en" href="http://testserver/docs/handbook/welcome/payments"' in html
    assert 'href="/docs/handbook/de/welcome"' in html  # the navigation stays in its language
    assert client.get("/api/public/docs/handbook/search", params={"q": "wiederholt", "lang": "de"}).json()[0]["url"] == "/docs/handbook/de/welcome/payments"
    assert client.get("/api/public/docs/handbook/search", params={"q": "wiederholt"}).json() == []
    assert client.get("/api/public/docs/handbook/page", params={"path": "", "lang": "xx"}).status_code == 404

    # The page changes: its translation is outdated, still shown, and made again in the next batch.
    client.patch(f"/api/projects/{pid}/docs/pages/{sub['id']}", json={"body": "Payments are retried twice.", "reason": "twice"})
    assert client.get(f"/api/projects/{pid}/docs/pages/{sub['id']}/translations").json()[0]["state"] == "outdated"
    assert db(client, look) == 1
    assert "zweimal" in client.get("/api/public/docs/handbook/page", params={"path": "welcome/payments", "lang": "de"}).json()["html"]
    # By hand: current until the page changes again.
    put = client.put(f"/api/projects/{pid}/docs/pages/{home['id']}/translations/de", json={"title": "Hallo", "body": "Von Hand."}).json()
    assert put["state"] == "current" and put["translated_by"] == "Admin"


def test_an_llm_translates_on_the_projects_subscription(client, project, monkeypatch):
    from plainboard_protocol import CompletionResult

    from plainboard.models import Project, Run, utcnow
    from sqlalchemy import select

    pid = project["id"]
    monkeypatch.setattr(client.app_ref.state.docs_watcher, "translate_soon", lambda: None)  # the test looks itself
    s = site(client, pid, published=True, public=True, languages=["fr"])
    client.patch(f"/api/projects/{pid}/docs/sites/{s['id']}", json={"translator": "llm", "translate_model": "claude-haiku-x"})
    page(client, pid, s["id"], title="Welcome", body="Hello.")
    fake = runner(client)
    specs = []

    async def complete(spec):
        specs.append(spec)
        return CompletionResult(text="Bienvenue" if "title" in spec.prompt else "Bonjour.", input_tokens=10, output_tokens=5, cost_usd=0.001)

    fake.complete = complete

    async def look(session):
        return await client.app_ref.state.docs_watcher.translate(session, await session.get(Project, pid), utcnow())

    assert db(client, look) == 1
    assert specs[0].model == "claude-haiku-x" and "into fr" in specs[0].system_prompt
    assert client.get("/api/public/docs/handbook/page", params={"path": "", "lang": "fr"}).json()["markdown"] == "Bonjour."
    runs = db(client, lambda session: session.scalars(select(Run.agent_id).where(Run.project_id == pid, Run.trigger == "docs")))
    assert list(runs) == [s["agent"]["id"]] * 2  # title and text, each counted, on the doc agent


def test_an_llm_translation_must_keep_the_markdown_skeleton(client, project, monkeypatch):
    """#155: a paragraph that comes back as a heading is asked again; twice wrong fails and the original is shown."""
    from plainboard_protocol import CompletionResult

    from plainboard.models import DocTranslation, Project, utcnow
    from plainboard.services import docs_translate as t
    from sqlalchemy import select, update

    body = "Vanillite generates new worlds.\n\n## Seeds\n\n- one\n- two\n\n![map](doc-file:3#dark) [more](doc-page:4)\n\n```\nx\n```"
    assert t.differs(body, body.replace("Vanillite generates", "Vanillite erzeugt")) is None
    assert t.differs(body, "# " + body) == "block 1 is h1 instead of text"
    assert t.differs(body, body.replace("## Seeds", "### Seeds")) is not None
    assert t.differs(body, body.replace("#dark", "")) is not None and t.differs(body, body.replace("[more](doc-page:4)", "more")) is not None

    pid = project["id"]
    monkeypatch.setattr(client.app_ref.state.docs_watcher, "translate_soon", lambda: None)  # the test looks itself
    s = site(client, pid, published=True, public=True, languages=["de"])
    client.patch(f"/api/projects/{pid}/docs/sites/{s['id']}", json={"translator": "llm"})
    p = page(client, pid, s["id"], title="World", body="Worlds are generated.\n\n## Seeds\n\nA seed.")
    answers = ["Welt", "# Welten werden erzeugt.\n\n## Seeds\n\nEin Seed.", "Welten werden erzeugt.\n\n## Seeds\n\nEin Seed."]
    fake = runner(client)

    async def complete(spec):
        return CompletionResult(text=answers.pop(0), input_tokens=10, output_tokens=5, cost_usd=0.001)

    fake.complete = complete

    async def look(session):
        return await client.app_ref.state.docs_watcher.translate(session, await session.get(Project, pid), utcnow())

    assert db(client, look) == 1 and not answers  # asked again after the heading
    assert client.get("/api/public/docs/handbook/page", params={"path": "", "lang": "de"}).json()["markdown"].startswith("Welten werden erzeugt.")

    client.patch(f"/api/projects/{pid}/docs/pages/{p['id']}", json={"body": "Worlds are new.", "reason": "shorter"})
    answers += ["Welt", "# Welten sind neu.", "# Welten sind neu."]
    assert db(client, look) == 0 and not answers
    assert client.get(f"/api/projects/{pid}/docs/pages/{p['id']}/translations").json()[0]["state"] == "failed"

    # One made before the check, current but with a heading of a paragraph: hidden and made again, nothing deleted.
    async def broken(session):
        from plainboard.models import DocPage

        page_ = await session.get(DocPage, p["id"])
        await session.execute(update(DocTranslation).where(DocTranslation.page_id == p["id"]).values(body="# Welten sind neu.", source_hash=page_.source_hash, error="", translated_at=utcnow(), translated_by="llm default"))
        await session.commit()

    db(client, broken)
    client.app_ref.state.docs_watcher._rechecked.clear()  # as after a restart
    answers += ["Welt", "Welten sind neu."]
    assert db(client, look) == 1 and not answers
    got = db(client, lambda session: session.scalar(select(DocTranslation).where(DocTranslation.page_id == p["id"])))
    assert got.body == "Welten sind neu." and t.state(type("P", (), {"source_hash": got.source_hash})(), got) == "current"


def test_a_site_of_thousands_of_pages_is_served_and_listed_piece_by_piece(client, project):
    """3 000 pages: 30 sections of 50 pages, one chain 40 deep, the rest at the top. Nothing reads them all."""
    import time

    from plainboard.models import DocPage, utcnow
    from plainboard.services.docs import source_hash
    from sqlalchemy import event, insert

    pid = project["id"]
    s = site(client, pid, published=True, public=True)

    async def fill(session):
        now, rows = utcnow(), []

        def row(id_, parent, pos, slug, path, depth):
            title = f"Page {slug}"
            body = f"Text of {slug}. " + ("needle " if id_ == 2500 else "") + "lorem ipsum " * 20
            return {"id": 100000 + id_, "page_key": 100000 + id_, "project_id": pid, "site_id": s["id"], "parent_id": parent and 100000 + parent, "position": pos, "title": title, "slug": slug,
                    "path": path, "depth": depth, "body": body, "source_hash": source_hash(title, body), "paths": [], "edited_by": "x", "edited_at": now,
                    "drift": 0, "flag_reason": "", "created_at": now, "updated_at": now}  # fmt: skip

        n = 0
        for sec in range(30):
            n += 1
            sec_id = n
            rows.append(row(sec_id, None, sec, f"s{sec}", f"s{sec}", 0))
            for k in range(50):
                n += 1
                rows.append(row(n, sec_id, k, f"p{k}", f"s{sec}/p{k}", 1))
        parent, path = None, ""
        for d in range(40):
            n += 1
            path = f"{path}/d{d}" if path else f"d{d}"
            rows.append(row(n, parent, 30 if parent is None else 0, f"d{d}", path, d))
            parent = n
        while n < 3000:
            n += 1
            rows.append(row(n, None, n, f"t{n}", f"t{n}", 0))
        for start in range(0, len(rows), 500):
            await session.execute(insert(DocPage), rows[start : start + 500])
        await session.commit()
        return n

    assert db(client, fill) == 3000
    from plainboard.db import sessionmaker

    engine = sessionmaker()().bind
    statements = []
    listen = lambda *a: statements.append(1)  # noqa: E731
    event.listen(engine.sync_engine, "before_cursor_execute", listen)
    try:
        started = time.monotonic()
        deep = client.get("/api/public/docs/handbook/page", params={"path": "/".join(f"d{i}" for i in range(40))}).json()
        took = time.monotonic() - started
        used = len(statements)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", listen)
    assert deep["title"] == "Page d39" and len(deep["breadcrumbs"]) == 39 and deep["prev"]["title"] == "Page d38" and deep["next"]["title"].startswith("Page t")
    assert len(deep["sidebar"]) == 40 and len(deep["sidebar"][0]["items"]) == 200 and "Page d0" in [i["title"] for i in deep["sidebar"][0]["items"]]
    assert all(len(level["items"]) == 1 for level in deep["sidebar"][1:])
    assert used < 80 and took < 5, (used, took)  # by the depth, not by the size
    top = client.get("/api/public/docs/handbook").json()["nav"]
    assert top["total"] == 30 + 1 + (3000 - 30 - 30 * 50 - 40) and len(top["items"]) == 200
    more = client.get("/api/public/docs/handbook/nav", params={"offset": 200, "limit": 200}).json()
    assert more["offset"] == 200 and len(more["items"]) == 200
    assert client.get("/api/public/docs/handbook/search", params={"q": "needle"}).json()[0]["title"].startswith("Page")
    first = client.get("/api/projects/{}/docs/sites/{}/pages".format(pid, s["id"]), params={"parent": 100001}).json()
    assert first["total"] == 50 and len(first["items"]) == 50
    html = client.get("/docs/handbook/s3/p7")
    assert html.status_code == 200 and len(html.text) < 200_000

    manager = manager_of(client, pid)
    chat(client, manager["id"], "TOOL list_docs " + json.dumps({"site": "handbook"}) + "\nTOOL list_docs " + json.dumps({"site": "handbook", "query": "p7 s3"}))
    wait_for(lambda: idle(client, pid))
    listed, found = tool_results(client, manager["id"])[-2:]
    assert listed.count("\n- ") == 50 and "(more: offset=50)" in listed and len(listed) < 12000
    assert '/s3/p7 "Page p7"' in found


def test_a_site_makes_at_most_its_limit_of_vector_searches_a_minute_then_searches_by_words(client, project, monkeypatch):
    from plainboard.services import docs_public as public
    from plainboard.services import search as search_service
    from plainboard.services.search import store

    queries = []

    class Model:
        state, name = "ready", "fake"

        async def query(self, text):
            queries.append(text)
            return [0.0]

    async def enabled(session, project):
        return True

    async def embedder(session):
        return Model()

    async def nearest(*args):
        return []

    monkeypatch.setattr(search_service, "enabled", enabled)
    monkeypatch.setattr(search_service, "embedder", embedder)
    monkeypatch.setattr(store, "nearest_of_kind", nearest)
    monkeypatch.setattr(public, "VECTOR_SEARCHES", public.RateLimit(public.VECTOR_SEARCHES_PER_MINUTE))
    pid = project["id"]
    s = site(client, pid, published=True, public=True)
    page(client, pid, s["id"], title="Webhooks", body="Stripe retries webhooks.")
    for _ in range(public.VECTOR_SEARCHES_PER_MINUTE):
        resp = client.get("/api/public/docs/handbook/search", params={"q": "webhooks"})
        assert "x-search-words-only" not in resp.headers and resp.json()[0]["title"] == "Webhooks"
    resp = client.get("/api/public/docs/handbook/search", params={"q": "webhooks"})
    assert resp.headers["x-search-words-only"] == "1" and resp.json()[0]["title"] == "Webhooks"  # words still find it
    assert len(queries) == public.VECTOR_SEARCHES_PER_MINUTE
    for key, seen in public.VECTOR_SEARCHES._seen.items():  # a minute later the budget is back
        public.VECTOR_SEARCHES._seen[key] = type(seen)(t - 61 for t in seen)
    resp = client.get("/api/public/docs/handbook/search", params={"q": "webhooks"})
    assert "x-search-words-only" not in resp.headers and len(queries) == public.VECTOR_SEARCHES_PER_MINUTE + 1


def test_the_doc_agent_searches_its_own_site_by_meaning_and_words_outside_the_visitors_budget(client, project, monkeypatch):
    from types import SimpleNamespace

    from plainboard.services import docs_public as public
    from plainboard.services import search as search_service
    from plainboard.services.search import store

    pid = project["id"]
    s = site(client, pid)  # a draft: not served, still searched
    other = site(client, pid, name="Other")
    hooks = page(client, pid, s["id"], title="Webhooks", body="Stripe retries webhooks. Set `RETRY_LIMIT` to change it.")
    payouts = page(client, pid, s["id"], title="Payouts", body="Money reaches your bank account every Friday.")
    page(client, pid, s["id"], title="Orders", body="Orders arrive here.")
    foreign = page(client, pid, other["id"], title="Retry limit", body="RETRY_LIMIT in the other site.")
    queries = []

    class Model:
        state, name = "ready", "fake"

        async def query(self, text):
            queries.append(text)
            return [0.0]

    async def enabled(session, project):
        return True

    async def embedder(session):
        return Model()

    async def nearest(*args):  # by meaning: the payouts page, and one of the other site
        return [SimpleNamespace(source_id=str(payouts["id"])), SimpleNamespace(source_id=str(foreign["id"]))]

    monkeypatch.setattr(search_service, "enabled", enabled)
    monkeypatch.setattr(search_service, "embedder", embedder)
    monkeypatch.setattr(store, "nearest_of_kind", nearest)
    monkeypatch.setattr(public, "VECTOR_SEARCHES", public.RateLimit(public.VECTOR_SEARCHES_PER_MINUTE))
    for _ in range(public.VECTOR_SEARCHES_PER_MINUTE):
        assert public.VECTOR_SEARCHES.allow(str(s["id"]))  # the visitors used up this minute's budget

    chat(client, s["agent"]["id"], "TOOL list_docs " + json.dumps({"site": "handbook", "query": "retry_limit when is money paid"}))
    wait_for(lambda: idle(client, pid))
    found = tool_results(client, s["agent"]["id"])[-1]
    assert found.startswith("Pages of handbook found for"), found
    assert f'- {hooks["id"]} /webhooks "Webhooks"' in found and "Set RETRY_LIMIT to change it." in found  # by words, with a snippet
    assert f'- {payouts["id"]} /payouts "Payouts"' in found  # by meaning only
    assert "Orders" not in found and "other site" not in found and str(foreign["id"]) + " /" not in found  # its own site only
    assert queries == ["retry_limit when is money paid"]  # searched by meaning although the budget was used up
    assert not public.VECTOR_SEARCHES.allow(str(s["id"]))  # and took nothing of it


def test_linked_code_paths_follow_renames_and_dead_ones_go_to_the_doc_agent(client, project):
    from plainboard.models import Assignment, Landing, LandingStatus, utcnow
    from sqlalchemy import select

    pid = project["id"]
    client.patch(f"/api/projects/{pid}", json={"auto_wake_manager": False})
    repo = client.post(f"/api/projects/{pid}/repositories", json={"name": "api", "url": "https://github.com/acme/api.git"}).json()
    s = site(client, pid)
    writer = s["agent"]["id"]
    pay = page(client, pid, s["id"], title="Payments", body="How payments work.", paths=["app/pay/", "README.md", "docs/*.md", "app/keep"])
    old = page(client, pid, s["id"], title="Legacy", body="Old things.", paths=["lib/old.py"])
    fake = runner(client)
    fake.commits = [GitCommit(sha="a" * 40, message="Start", files=["README.md"])]
    watcher = client.app_ref.state.docs_watcher
    now = utcnow()
    client.portal.call(watcher.tick, now)

    async def land(s):
        s.add(Landing(project_id=pid, repository_id=repo["id"], branch="be/move", title="Move payments", status=LandingStatus.MERGED, merged_at=now + timedelta(minutes=1)))
        await s.commit()

    db(client, land)
    renames = {"app/pay/a.py": "app/payments/a.py", "app/pay/b.py": "app/payments/b.py", "README.md": "docs/README.md", "app/keep/x.py": "app/other/x.py"}
    fake.commits = [GitCommit(sha="c" * 40, message="Move payments", files=[f for pair in renames.items() for f in pair], renames=renames), *fake.commits]
    fake.dead_patterns = {"app/pay"}  # app/pay is empty now, app/keep still has files
    client.portal.call(watcher.tick, now + timedelta(minutes=20))
    wait_for(lambda: idle(client, pid))
    moved = client.get(f"/api/projects/{pid}/docs/pages/{pay['id']}").json()
    assert moved["paths"] == ["app/payments", "docs/README.md", "docs/*.md", "app/keep"]
    newest = client.get(f"/api/projects/{pid}/docs/pages/{pay['id']}/versions").json()[0]
    assert newest["author"] == "PlainBoard" and newest["reason"] == "Linked code paths follow a rename: app/pay/ -> app/payments, README.md -> docs/README.md"
    assert client.get(f"/api/projects/{pid}/docs/pages/{old['id']}").json()["paths"] == ["lib/old.py"]

    async def orders(s):
        return [a.instructions for a in await s.scalars(select(Assignment).where(Assignment.agent_id == writer).order_by(Assignment.id))]

    # Daily: paths that match nothing in any repository are named, with the pages, once a week.
    fake.dead_patterns = {"lib/old.py", "docs/*.md"}
    client.portal.call(watcher.tick, now + timedelta(days=2))
    wait_for(lambda: idle(client, pid))
    stale = [t for t in db(client, orders) if t.startswith("Pages of your doc site that are probably stale")]
    assert len(stale) == 1, db(client, orders)
    assert f'doc page {old["id"]} "Legacy" (/legacy): its code paths match no file any more: lib/old.py' in stale[0]
    assert f'doc page {pay["id"]} "Payments" (/payments): its code paths match no file any more: docs/*.md' in stale[0] and "write_doc_page paths" in stale[0]
    assert {p for spec in fake.present_specs for p in spec.patterns} >= {"lib/old.py", "app/payments", "docs/*.md"}
    client.portal.call(watcher.tick, now + timedelta(days=3, hours=1))
    assert len([t for t in db(client, orders) if t.startswith("Pages of your doc site that are probably stale")]) == 1


def test_a_picture_for_one_mode_shows_only_in_that_mode():
    from plainboard.services.docs_public import render

    body = "![a](doc-file:1#light) ![b](doc-file:2#dark) ![c](x.png#gh-dark-mode-only) ![d](doc-file:3) [e](doc-file:4#dark)"
    out = render(body, base="/docs/h", files_base="/f", page_paths={}).html
    assert '<img src="/f/1" alt="a" data-mode="light" />' in out and '<img src="/f/2" alt="b" data-mode="dark" />' in out
    assert '<img src="x.png" alt="c" data-mode="dark" />' in out and '<img src="/f/3" alt="d" />' in out
    assert 'href="doc-file:4#dark"' in out  # only pictures have a mode


def test_the_doc_agent_links_pages_by_id_and_no_link_goes_nowhere(client, project):
    from plainboard.models import Assignment
    from plainboard.services.docs_public import render
    from sqlalchemy import select

    pid = project["id"]
    client.patch(f"/api/projects/{pid}", json={"auto_wake_manager": False})
    s = site(client, pid)
    writer = s["agent"]["id"]
    assert "\"See also\" line" in agents_by_slug(client, pid)["docs-handbook"]["instructions"]
    guide = page(client, pid, s["id"], title="Guide", body="Start.")
    setup = page(client, pid, s["id"], title="Setup", body="Install.", parent_id=guide["id"])
    billing = page(client, pid, s["id"], title="Billing", body="Fees.")

    # Path links to its own pages are written as doc-page:<id>; links to no page refuse the write.
    body = (
        "See [setup](setup), [fees](/docs/handbook/billing#fees), [home](../guide) and [again](doc-page:%d).\n\n"
        "[ext](https://example.com/x) [top](#top) `[code](nowhere)`\n\n[ref]: setup.md\n\nA [reference][ref]." % billing["id"]
    )
    chat(client, writer, "\n".join([
        "TOOL write_doc_page " + json.dumps({"site": "handbook", "title": "FAQ", "parent": guide["id"], "body": body, "reason": "faq"}),
        "TOOL write_doc_page " + json.dumps({"site": "handbook", "title": "Bad", "body": "[a](doc-page:99999) [b](nowhere/at-all) [c](doc-page:%d)" % setup["id"], "reason": "bad"}),
    ]))  # fmt: skip
    wait_for(lambda: idle(client, pid))
    created, refused = tool_results(client, writer)[-2:]
    assert created.endswith("4 path link(s) written as doc-page:ID."), created
    faq = int(created.split()[3])
    saved = client.get(f"/api/projects/{pid}/docs/pages/{faq}").json()["body"]
    sid, bid, gid = setup["id"], billing["id"], guide["id"]
    assert f"[setup](doc-page:{sid}), [fees](doc-page:{bid}#fees), [home](doc-page:{gid}) and [again](doc-page:{bid})" in saved
    assert f"[ref]: doc-page:{sid}" in saved and "(https://example.com/x)" in saved and "`[code](nowhere)`" in saved
    assert refused == "Not saved: these links name no page of handbook: doc-page:99999, nowhere/at-all. Link pages as [text](doc-page:ID) (ids from list_docs), write a missing page first, or leave the link out."
    assert [p["title"] for p in client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/pages").json()["items"]].count("Bad") == 0

    # A deleted page: the pages that link to it are marked and go to the doc agent with the next daily look.
    assert client.delete(f"/api/projects/{pid}/docs/pages/{bid}").status_code == 204
    marked = client.get(f"/api/projects/{pid}/docs/pages/{faq}").json()
    assert marked["flagged_at"] and marked["flag_reason"] == f'links to a deleted page: "Billing" (doc-page:{bid}); change or remove the link'
    assert not client.get(f"/api/projects/{pid}/docs/pages/{sid}").json()["flagged_at"]
    client.portal.call(client.app_ref.state.docs_watcher.tick)
    wait_for(lambda: idle(client, pid))

    async def orders(s):
        return [a.instructions for a in await s.scalars(select(Assignment).where(Assignment.agent_id == writer))]

    stale = [t for t in db(client, orders) if t.startswith("Pages of your doc site that are probably stale")]
    assert len(stale) == 1 and f'doc page {faq} "FAQ" (/guide/faq): links to a deleted page: "Billing"' in stale[0] and "remove the link" in stale[0]

    # Served, a link to a page that is not there is its text, no dead link.
    out = render(saved, base="/docs/handbook", files_base="/f", page_paths={sid: "guide/setup", gid: "guide"}).html
    assert '<a href="/docs/handbook/guide/setup">setup</a>' in out and ", fees, " in out and "and again." in out and 'href="#"' not in out


def test_the_doc_agent_proposes_a_restructuring_once_the_site_grew_and_the_user_decides(client, project):
    from plainboard.models import Assignment, Decision, DocProposal, Task, utcnow
    from plainboard.services import docs as docs_service
    from sqlalchemy import select

    pid = project["id"]
    client.patch(f"/api/projects/{pid}", json={"auto_wake_manager": False})
    s = site(client, pid, spec="For admins.")
    assert s["restructure_check"] is True
    writer = s["agent"]["id"]
    assert "propose_doc_restructure" in agents_by_slug(client, pid)["docs-handbook"]["instructions"]
    pages = [page(client, pid, s["id"], title=f"Topic {i}", body="Text.") for i in range(14)]
    watcher = client.app_ref.state.docs_watcher
    now = utcnow()

    async def orders(s):
        return [a for a in await s.scalars(select(Assignment).where(Assignment.agent_id == writer).order_by(Assignment.id))]

    def checks():
        return [a for a in db(client, orders) if a.title == "Check the structure of your doc site"]

    client.portal.call(watcher.tick, now)
    assert not checks()  # 14 pages: too small to check
    pages.append(page(client, pid, s["id"], title="Topic 14", body="Text."))
    client.portal.call(watcher.tick, now + timedelta(days=1, minutes=1))
    wait_for(lambda: idle(client, pid))
    (check,) = checks()
    assert "it has 15 pages" in check.instructions and "Most of the time it fits" in check.instructions and "Never restructure yourself" in check.instructions
    for i in range(4):  # 19 pages: not a quarter and 5 more, few edits
        page(client, pid, s["id"], title=f"More {i}", body="Text.")
    client.portal.call(watcher.tick, now + timedelta(days=9))
    assert len(checks()) == 1

    # A proposal is a ticket with a decision for the user; one at a time.
    first, second = pages[0]["id"], pages[1]["id"]
    proposal = {"title": "Group the topics", "reason": "Readers find topics faster in two sections.", "before": "- Topic 0\n- Topic 1", "after": "- Basics\n  - Topic 0\n  - Topic 1", "pages": [first, second], "options": ["Umbauen", "So lassen"]}
    chat(client, writer, "\n".join(["TOOL propose_doc_restructure " + json.dumps(proposal), "TOOL propose_doc_restructure " + json.dumps({**proposal, "title": "Other"})]))
    wait_for(lambda: idle(client, pid))
    made, refused = tool_results(client, writer)[-2:]
    assert made.startswith("Proposed on ticket #") and refused.startswith("A proposal is already waiting for the user's decision (ticket #")
    number = int(made.split("#")[1].split(":")[0])

    async def decision_of(s, number):
        task = await s.scalar(select(Task).where(Task.project_id == pid, Task.number == number))
        decision = await s.scalar(select(Decision).where(Decision.task_id == task.id))
        return task.id, decision.id, decision.question, decision.details, [o["label"] for o in decision.options]

    task_id, decision_id, question, details, labels = db(client, lambda s: decision_of(s, number))
    assert question == "Group the topics (Handbook)" and labels == ["Umbauen", "So lassen"]
    assert "Readers find topics faster" in details and "**Outline proposed**" in details and f"doc-page:{first}" in details

    # Declined: the ticket is done and the same proposal, in other dress, is refused.
    assert client.post(f"/api/decisions/{decision_id}/answer", json={"option_index": 1}).status_code == 200
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "done"
    chat(client, writer, "\n".join([
        "TOOL propose_doc_restructure " + json.dumps({**proposal, "after": "* basics\n    * topic 0\n    * TOPIC 1", "pages": []}),
        "TOOL propose_doc_restructure " + json.dumps({**proposal, "after": "- Other order", "pages": [second, first]}),
        "TOOL write_doc_page " + json.dumps({"page": first, "delete": True, "reason": "merged"}),
    ]))  # fmt: skip
    wait_for(lambda: idle(client, pid))
    assert all(r.startswith(f"The user declined this restructuring on ticket #{number}") for r in tool_results(client, writer)[-3:-1])
    assert tool_results(client, writer)[-1].startswith("Not deleted: pages go only in a restructuring the user approved")

    # Approved: PlainBoard orders the doc agent the restructuring on the ticket; there it may delete pages.
    chat(client, writer, "TOOL propose_doc_restructure " + json.dumps({**proposal, "title": "Merge topic 1", "after": "- Topic 0 (with topic 1)", "pages": [second]}))
    wait_for(lambda: idle(client, pid))
    number = int(tool_results(client, writer)[-1].split("#")[1].split(":")[0])
    task_id, decision_id, *_ = db(client, lambda s: decision_of(s, number))
    note = "Yes.\nTOOL write_doc_page " + json.dumps({"page": second, "delete": True, "reason": "merged into topic 0"})
    assert client.post(f"/api/decisions/{decision_id}/answer", json={"option_index": 0, "text": note}).status_code == 200
    wait_for(lambda: idle(client, pid))
    (order,) = [a for a in db(client, orders) if a.task_id == task_id]
    assert order.title == "Restructure the doc site Handbook" and f"as you proposed on ticket #{number}" in order.instructions and "doc-page: links stay valid" in order.instructions
    assert client.get(f"/api/projects/{pid}/docs/pages/{second}").status_code == 404
    statuses = db(client, lambda s: s.scalars(select(DocProposal.status).order_by(DocProposal.id)))
    assert list(statuses) == ["declined", "approved"]

    # A moved page keeps its slug, so its old address still finds it.
    client.patch(f"/api/projects/{pid}/docs/pages/{first}", json={"move": True, "parent_id": pages[2]["id"]})
    found = db(client, lambda x: docs_service.moved(x, s["id"], "topic-0"))
    assert found.page_key == first

    # The switch: off, no checks however much the site grows.
    assert client.patch(f"/api/projects/{pid}/docs/sites/{s['id']}", json={"restructure_check": False}).json()["restructure_check"] is False
    for i in range(10):
        page(client, pid, s["id"], title=f"Late {i}", body="Text.")
    client.portal.call(watcher.tick, now + timedelta(days=30))
    assert len(checks()) == 1


def test_a_page_written_by_people_only_is_read_but_never_changed_or_handed_to_the_doc_agent(client, project):
    from plainboard.models import Assignment, DocPage, Landing, LandingStatus, utcnow
    from sqlalchemy import select

    pid = project["id"]
    client.patch(f"/api/projects/{pid}", json={"auto_wake_manager": False})
    repo = client.post(f"/api/projects/{pid}/repositories", json={"name": "api", "url": "https://github.com/acme/api.git"}).json()
    s = site(client, pid)
    writer = s["agent"]["id"]
    guide = page(client, pid, s["id"], title="Guide", body="All about it.")
    mine = page(client, pid, s["id"], title="Payments", body="My own words.", parent_id=guide["id"], paths=["app/pay"])
    other = page(client, pid, s["id"], title="Orders", body="Orders.", paths=["app/orders"])
    marked = client.patch(f"/api/projects/{pid}/docs/pages/{mine['id']}", json={"human_only": True}).json()
    assert marked["human_only"] is True and marked["body"] == "My own words." and marked["versions"] == 1

    # The doc agent reads and links it, but changes, moves and deletes nothing of it, nor plans to.
    chat(client, writer, "\n".join([
        "TOOL list_docs " + json.dumps({"site": "handbook", "parent": guide["id"]}),
        "TOOL read_doc_page " + json.dumps({"page": mine["id"]}),
        "TOOL write_doc_page " + json.dumps({"page": mine["id"], "body": "Better words.", "reason": "clearer"}),
        "TOOL write_doc_page " + json.dumps({"page": mine["id"], "move": True, "parent": 0, "reason": "to the top"}),
        "TOOL write_doc_page " + json.dumps({"page": guide["id"], "delete": True, "reason": "merged"}),
        "TOOL propose_doc_restructure " + json.dumps({"title": "Merge", "reason": "Readers find it faster in one page.", "before": "- Guide\n  - Payments", "after": "- Guide", "pages": [guide["id"], mine["id"]]}),
        "TOOL write_doc_page " + json.dumps({"site": "handbook", "title": "Refunds", "body": f"See [payments](doc-page:{mine['id']}).", "reason": "new page"}),
    ]))  # fmt: skip
    wait_for(lambda: idle(client, pid))
    listed, read, edit, move, delete, proposed, linked = tool_results(client, writer)[-7:]
    assert f'- {mine["id"]} /guide/payments "Payments" [written by people only: read and link only]' in listed
    assert "written by people only: read and link only, never change it" in read and "My own words." in read
    assert edit == move == f"Not changed: doc page {mine['id']} is written by people only. Read and link it, but leave it as it is."
    assert delete.startswith(f"Not deleted: pages below it are written by people only ({mine['id']})")
    assert f"Pages {mine['id']} are written by people only: plan no change to them" in proposed
    assert linked.startswith("Created doc page ")
    kept = client.get(f"/api/projects/{pid}/docs/pages/{mine['id']}").json()
    assert kept["body"] == "My own words." and kept["parent_id"] == guide["id"] and kept["human_only"] is True

    # The watcher hands no work on it to the doc agent: not after a landing, not as a stale page.
    fake = runner(client)
    fake.commits = [GitCommit(sha="a" * 40, message="Start", files=["README.md"])]
    watcher = client.app_ref.state.docs_watcher
    now = utcnow()
    client.portal.call(watcher.tick, now)

    async def land(s):
        s.add(Landing(project_id=pid, repository_id=repo["id"], branch="be/pay", title="Pay and order", status=LandingStatus.MERGED, merged_at=now + timedelta(minutes=1)))
        await s.commit()

    db(client, land)
    fake.commits = [GitCommit(sha="c" * 40, message="Pay and order", files=["app/pay/x.py", "app/orders/y.py"]), *fake.commits]
    client.portal.call(watcher.tick, now + timedelta(minutes=20))
    wait_for(lambda: idle(client, pid))

    async def orders(s):
        return [a.instructions for a in await s.scalars(select(Assignment).where(Assignment.agent_id == writer, Assignment.title != "").order_by(Assignment.id))]

    texts = [t for t in db(client, orders) if t.startswith("Pages of your doc site")]
    assert len(texts) == 1 and f'doc page {other["id"]} "Orders"' in texts[0] and "Payments" not in texts[0]
    assert client.get(f"/api/projects/{pid}/docs/pages/{mine['id']}").json()["flagged_at"] is None

    async def drift(s):
        for key in (mine["id"], other["id"]):
            (await s.get(DocPage, key)).drift = 12
        await s.commit()

    db(client, drift)
    client.portal.call(watcher.tick, now + timedelta(days=2))
    wait_for(lambda: idle(client, pid))
    stale = [t for t in db(client, orders) if t.startswith("Pages of your doc site that are probably stale")]
    assert len(stale) == 1 and f'doc page {other["id"]} "Orders"' in stale[0] and "Payments" not in stale[0]
