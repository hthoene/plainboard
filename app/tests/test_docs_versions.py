"""Versions of doc sites (#126): cut from the working version, frozen by default, still correctable, served per version."""

import json

from conftest import agents_by_slug, idle, wait_for
from test_docs import db, page, site
from test_flow import chat, manager_of, tool_results


def rows_of(client, key: int) -> list[tuple]:
    from plainboard.models import DocPage
    from sqlalchemy import select

    async def read(s):
        return [tuple(r) for r in (await s.execute(select(DocPage.since_seq, DocPage.until_seq, DocPage.body).where(DocPage.page_key == key).order_by(DocPage.since_seq))).all()]

    return db(client, read)


def test_a_cut_version_keeps_its_pages_while_the_working_one_goes_on_and_both_can_be_corrected(client, project):
    pid = project["id"]
    s = site(client, pid, published=True, public=True)
    sid = s["id"]
    base = f"/api/projects/{pid}/docs"
    working = s["releases"][0]
    assert working["working"] and working["slug"] == "next" and working["default"]
    intro = page(client, pid, sid, title="Intro", body="Old intro.")
    guide = page(client, pid, sid, title="Guide", body=f"See [intro](doc-page:{intro['id']}).")
    setup = page(client, pid, sid, title="Setup", parent_id=guide["id"], body="Install it.")

    cut = client.post(f"{base}/sites/{sid}/releases", json={"name": "1.0", "notes": "First release"})
    assert cut.status_code == 201, cut.text
    v1 = cut.json()
    assert v1["slug"] == "1.0" and not v1["working"] and v1["default"] and v1["url"] == "/docs/handbook/"
    assert len(rows_of(client, intro["id"])) == 1  # nothing copied

    # The working version goes on: a change, a new page, a deletion, a new address.
    client.patch(f"{base}/pages/{intro['id']}", json={"body": "New intro."})
    feature = page(client, pid, sid, title="Feature", body="Brand new.")
    assert client.delete(f"{base}/pages/{setup['id']}").status_code == 204
    moved = client.patch(f"{base}/pages/{guide['id']}", json={"slug": "manual"}).json()
    assert moved["path"] == "manual" and moved["id"] == guide["id"]

    top = lambda release=None: [p["title"] for p in client.get(f"{base}/sites/{sid}/pages", params={"release": release} if release else {}).json()["items"]]  # noqa: E731
    assert top() == ["Intro", "Guide", "Feature"] and top(v1["id"]) == ["Intro", "Guide"]
    assert client.get(f"{base}/pages/{intro['id']}").json()["body"] == "New intro."
    old = client.get(f"{base}/pages/{intro['id']}", params={"release": v1["id"]}).json()
    assert old["body"] == "Old intro." and old["release_id"] == v1["id"] and old["id"] == intro["id"]
    assert client.get(f"{base}/pages/{setup['id']}").status_code == 404
    assert client.get(f"{base}/pages/{setup['id']}", params={"release": v1["id"]}).json()["path"] == "guide/setup"
    assert client.get(f"{base}/pages/{feature['id']}", params={"release": v1["id"]}).status_code == 404
    assert len(rows_of(client, intro["id"])) == 2  # copy on write: only what changed

    # Visitors get the latest released version; the working one at /next/ (the frontend shows its banner).
    client.cookies.clear()
    view = client.get("/api/public/docs/handbook").json()
    assert view["version"]["slug"] == "1.0" and [v["slug"] for v in view["versions"]] == ["next", "1.0"] and view["base"] == view["site_base"] == "/docs/handbook"
    assert [i["title"] for i in view["nav"]["items"]] == ["Intro", "Guide"]
    nxt = client.get("/api/public/docs/handbook", params={"release": "next"}).json()
    assert nxt["version"]["working"] and nxt["base"] == "/docs/handbook/next" and len(nxt["nav"]["items"]) == 3
    assert client.get("/api/public/docs/handbook/page", params={"path": "guide/setup"}).json()["markdown"] == "Install it."
    assert client.get("/api/public/docs/handbook/page", params={"path": "guide/setup", "release": "next"}).status_code == 404
    linked = client.get("/api/public/docs/handbook/page", params={"path": "manual", "release": "next"}).json()
    assert 'href="/docs/handbook/next/intro"' in linked["html"]
    assert client.get("/docs/handbook/guide/setup").status_code == 200 and "Install it." in client.get("/docs/handbook/1.0/guide/setup").text
    assert client.get("/docs/handbook/next/manual").status_code == 200 and client.get("/docs/handbook/next/guide/setup").status_code == 404
    hits = client.get("/api/public/docs/handbook/search", params={"q": "intro"}).json()
    assert [h["snippet"] for h in hits if h["id"] == intro["id"]] == ["Old intro."]
    assert any(h["url"] == "/docs/handbook/next/intro" and h["snippet"] == "New intro." for h in client.get("/api/public/docs/handbook/search", params={"q": "intro", "release": "next"}).json())

    # A released version can still be corrected, without the working version noticing.
    from conftest import ADMIN_PASSWORD
    from test_permissions import sign_in

    sign_in(client, "admin", ADMIN_PASSWORD)
    fixed = client.patch(f"{base}/pages/{intro['id']}", params={"release": v1["id"]}, json={"body": "Old intro, corrected."}).json()
    assert fixed["body"] == "Old intro, corrected." and client.get(f"{base}/pages/{intro['id']}").json()["body"] == "New intro."
    assert len(rows_of(client, intro["id"])) == 2
    history = client.get(f"{base}/pages/{intro['id']}/versions").json()
    assert [v["reason"] for v in history][-1] == "created" and len(history) == 3  # every saved state of the page, in all versions

    # Settings of versions: the working one hidden, another default, one deprecated; then deleted ones take their rows.
    v2 = client.post(f"{base}/sites/{sid}/releases", json={"name": "2.0"}).json()
    client.patch(f"{base}/pages/{intro['id']}", json={"body": "Newest intro."})
    assert [r[2] for r in rows_of(client, intro["id"])] == ["Old intro, corrected.", "New intro.", "Newest intro."]
    assert client.patch(f"{base}/releases/{working['id']}", json={"hidden": True}).json()["hidden"]
    client.patch(f"{base}/releases/{v1['id']}", json={"default": True, "deprecated": True})
    listed = {r["name"]: r for r in client.get(f"{base}/sites/{sid}/releases").json()}
    assert listed["1.0"]["default"] and listed["1.0"]["deprecated"] and not listed["2.0"]["default"] and listed["2.0"]["url"] == "/docs/handbook/2.0/"
    assert client.patch(f"{base}/releases/{working['id']}", json={"deprecated": True}).status_code == 400
    assert client.post(f"{base}/sites/{sid}/releases", json={"name": "en"}).status_code == 400  # not a version address: none of the site's languages either
    client.cookies.clear()
    assert client.get("/api/public/docs/handbook", params={"release": "next"}).status_code == 404
    assert client.get("/api/public/docs/handbook").json()["version"]["slug"] == "1.0"
    assert [v["slug"] for v in client.get("/api/public/docs/handbook").json()["versions"]] == ["2.0", "1.0"]
    sign_in(client, "admin", ADMIN_PASSWORD)
    assert client.delete(f"{base}/releases/{v2['id']}").status_code == 204
    assert [r[2] for r in rows_of(client, intro["id"])] == ["Old intro, corrected.", "Newest intro."]
    assert client.delete(f"{base}/releases/{working['id']}").status_code == 400
    assert client.delete(f"{base}/releases/{v1['id']}").status_code == 204
    assert rows_of(client, setup["id"]) == [] and [r[2] for r in rows_of(client, intro["id"])] == ["Newest intro."]
    assert top() == ["Intro", "Guide", "Feature"]


def test_a_page_deleted_in_the_working_version_lives_on_in_the_released_one_under_its_id(client, project):
    pid = project["id"]
    s = site(client, pid)
    base = f"/api/projects/{pid}/docs"
    p = page(client, pid, s["id"], title="Old feature", body="v1")
    child = page(client, pid, s["id"], title="Detail", parent_id=p["id"], body="d1")
    v1 = client.post(f"{base}/sites/{s['id']}/releases", json={"name": "1.0"}).json()
    client.patch(f"{base}/pages/{p['id']}", json={"body": "v2"})
    assert client.delete(f"{base}/pages/{p['id']}").status_code == 204  # the working row goes, the key passes to 1.0's
    assert client.get(f"{base}/pages/{p['id']}").status_code == 404
    kept = client.get(f"{base}/pages/{p['id']}", params={"release": v1["id"]}).json()
    assert kept["body"] == "v1" and kept["id"] == p["id"] and kept["children"] == 1
    assert client.get(f"{base}/pages/{child['id']}", params={"release": v1["id"]}).json()["ancestors"][0]["id"] == p["id"]
    assert len(rows_of(client, p["id"])) == 1
    # A page added to the released version only.
    extra = client.post(f"{base}/sites/{s['id']}/pages", params={"release": v1["id"]}, json={"title": "Erratum", "body": "Fix."}).json()
    assert extra["release_id"] == v1["id"] and client.get(f"{base}/pages/{extra['id']}").status_code == 404


def test_the_manager_cuts_versions_and_the_doc_agent_corrects_a_released_one(client, project):
    pid = project["id"]
    manager = manager_of(client, pid)
    chat(client, manager["id"], "TOOL manage_doc_site " + json.dumps({"name": "Guide", "spec": "For users."}))
    wait_for(lambda: idle(client, pid))
    writer = agents_by_slug(client, pid)["docs-guide"]
    assert "released version" in writer["instructions"]
    chat(client, writer["id"], "TOOL write_doc_page " + json.dumps({"site": "guide", "title": "Start", "body": "Run it.", "reason": "first"}))
    wait_for(lambda: idle(client, pid))
    key = int(tool_results(client, writer["id"])[-1].split()[3])
    chat(client, manager["id"], "TOOL manage_doc_version " + json.dumps({"site": "guide", "version": "1.0", "cut": True, "notes": "Launch"}))
    wait_for(lambda: idle(client, pid))
    done = tool_results(client, manager["id"])[-1]
    assert done.startswith("Cut version 1.0 of guide") and "1.0 (default; /docs/guide/)" in done and "next (working; /docs/guide/next/)" in done
    chat(client, writer["id"], "\n".join([
        "TOOL write_doc_page " + json.dumps({"page": key, "body": "Run it with --fast.", "reason": "new option"}),
        "TOOL write_doc_page " + json.dumps({"page": key, "body": "Run it (as root).", "reason": "an error in 1.0", "version": "1.0"}),
        "TOOL read_doc_page " + json.dumps({"page": key, "version": "1.0"}),
        "TOOL list_docs {}",
    ]))  # fmt: skip
    wait_for(lambda: idle(client, pid))
    new, fixed, read, listed = tool_results(client, writer["id"])[-4:]
    assert "saved as a new version" in new and "(version 1.0) saved as a new version" in fixed
    assert "Run it (as root)." in read and "site guide (version 1.0)" in read
    assert "versions: working + 1.0" in listed
    assert client.get(f"/api/projects/{pid}/docs/pages/{key}").json()["body"] == "Run it with --fast."
    chat(client, manager["id"], "TOOL manage_doc_version " + json.dumps({"site": "guide", "version": "1.0", "deprecated": True}))
    wait_for(lambda: idle(client, pid))
    assert "1.0 (deprecated; /docs/guide/1.0/)" in tool_results(client, manager["id"])[-1]


def test_many_versions_of_a_large_site_cost_rows_by_changes_not_by_versions(client, project):
    """2 000 pages, 25 versions, a few pages changed between two cuts: rows grow by the changes, a cut copies
    nothing, and a page of the oldest version is served with as few queries as one of the working version."""
    import time

    from plainboard.models import DocPage, utcnow
    from plainboard.services.docs import source_hash
    from sqlalchemy import event, func, insert, select

    pid = project["id"]
    s = site(client, pid, published=True, public=True)
    base = f"/api/projects/{pid}/docs"

    async def fill(session):
        now, rows = utcnow(), []
        for n in range(1, 2001):
            parent = None if n <= 40 else 200000 + (n % 40) + 1
            slug = f"p{n}"
            path = slug if parent is None else f"p{parent - 200000}/{slug}"
            title, body = f"Page {n}", f"Text of page {n}. " + "lorem ipsum " * 30
            rows.append({
                "id": 200000 + n, "page_key": 200000 + n, "project_id": pid, "site_id": s["id"], "parent_id": parent, "position": n, "title": title, "slug": slug,
                "path": path, "depth": 0 if parent is None else 1, "body": body, "source_hash": source_hash(title, body), "paths": [], "edited_by": "x",
                "edited_at": now, "drift": 0, "flag_reason": "", "created_at": now, "updated_at": now, "since_seq": 1,
            })  # fmt: skip
        for start in range(0, len(rows), 500):
            await session.execute(insert(DocPage), rows[start : start + 500])
        await session.commit()

    db(client, fill)
    count = lambda: db(client, lambda s: s.scalar(select(func.count()).select_from(DocPage).where(DocPage.site_id == s_id)))  # noqa: E731
    s_id = s["id"]
    cuts = []
    for v in range(25):
        started = time.monotonic()
        assert client.post(f"{base}/sites/{s_id}/releases", json={"name": f"1.{v}"}).status_code == 201
        cuts.append(time.monotonic() - started)
        for k in range(4):  # four pages change in each version
            key = 200000 + 41 + (v * 4 + k) % 1900
            assert client.patch(f"{base}/pages/{key}", json={"body": f"Changed in 1.{v + 1}"}).status_code == 200
    assert count() == 2000 + 25 * 4  # one row per change, none per version
    assert max(cuts) < 1.0, cuts

    from plainboard.db import sessionmaker

    engine = sessionmaker()().bind

    def served(path: str, release: str | None) -> tuple[int, dict]:
        statements = []
        listen = lambda *a: statements.append(1)  # noqa: E731
        event.listen(engine.sync_engine, "before_cursor_execute", listen)
        try:
            body = client.get("/api/public/docs/handbook/page", params={"path": path, **({"release": release} if release else {})}).json()
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", listen)
        return len(statements), body

    oldest_q, oldest = served("p2/p41", "1.0")
    newest_q, newest = served("p2/p41", "next")
    assert oldest["markdown"].startswith("Text of page 41") and newest["markdown"] == "Changed in 1.1"
    assert oldest_q <= newest_q + 2 and oldest_q < 40, (oldest_q, newest_q)
    assert len(oldest["sidebar"][0]["items"]) == 40 and oldest["sidebar"][1]["total"] == 49
    assert client.get("/api/public/docs/handbook/page", params={"path": "p2/p41", "release": "1.1"}).json()["markdown"] == "Changed in 1.1"
    assert client.get("/api/public/docs/handbook/page", params={"path": "p2/p41"}).json()["markdown"] == "Changed in 1.1"  # the default: 1.24
