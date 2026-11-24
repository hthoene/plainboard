"""A doc site's theme by agents (#128): the tool with the settings' checks and versions, the preview's grant, the
theme from a website (public addresses only)."""

import json

import httpx
import pytest
from plainboard_protocol.netguard import NotPublic, check_url
from test_docs import page, site
from test_flow import chat, manager_of, tool_results

from conftest import idle, wait_for


def call(client, agent_id: int, name: str, **args) -> str:
    chat(client, agent_id, f"TOOL {name} " + json.dumps(args))
    wait_for(lambda: idle(client, client.pid))
    return tool_results(client, agent_id)[-1]


def test_the_theme_tool_checks_like_the_settings_and_keeps_every_version(client, project):
    pid = client.pid = project["id"]
    s = site(client, pid)
    url = f"/api/projects/{pid}/docs/sites/{s['id']}"
    assert client.patch(url, json={"theme": {"radius": 4}}).status_code == 200  # set before by a person: kept as the first version
    manager = manager_of(client, pid)["id"]

    assert call(client, manager, "doc_theme", site="handbook") == 'Theme of handbook: {"radius":4}'
    assert call(client, manager, "doc_theme", site="handbook", action="set", theme={"light": {"accent": "red"}}).startswith("Invalid theme: light.accent")
    assert call(client, manager, "doc_theme", site="handbook", action="set", theme={"colour": "#fff"}).startswith("Unknown theme field(s): colour.")
    assert call(client, manager, "doc_theme", site="handbook", action="set", theme={"font_url": "http://fonts.example.com/a.css"}).startswith("Font stylesheet: an https address")
    saved = call(client, manager, "doc_theme", site="handbook", action="set", theme={"light": {"accent": "#0a7d5a"}, "font_family": "Source Sans 3, sans-serif"}, reason="brand")
    assert saved.startswith("Theme of handbook saved as a new version:") and '"accent":"#0a7d5a"' in saved and '"radius":4' in saved
    assert call(client, manager, "doc_theme", site="handbook", action="set", theme={"radius": None}).startswith("Theme of handbook saved")
    assert client.get(url).json()["theme"]["radius"] is None and client.get(url).json()["theme"]["light"]["accent"] == "#0a7d5a"

    history = call(client, manager, "doc_theme", site="handbook", action="history")
    rows = history.splitlines()[1:]
    assert len(rows) == 3 and "by Manager: changed" in rows[0] and "by Manager: brand" in rows[1] and "in the settings" in rows[2]
    assert call(client, manager, "doc_theme", site="handbook", action="undo").startswith("Theme of handbook saved")
    assert client.get(url).json()["theme"]["radius"] == 4  # back to before the last change
    first = int(rows[2].split()[1])
    call(client, manager, "doc_theme", site="handbook", action="restore", version=first)
    assert client.get(url).json()["theme"]["light"]["accent"] == ""
    assert call(client, manager, "doc_theme", site="handbook", action="reset").startswith("Theme of handbook saved")
    assert client.get(url).json()["theme"]["radius"] is None
    assert call(client, manager, "doc_theme", site="handbook", action="restore", version=999999).endswith("not found.")


def test_the_preview_sees_a_draft_theme_and_an_unpublished_site_only_with_its_grant(client, project):
    pid = client.pid = project["id"]
    s = site(client, pid)  # a draft: not served
    page(client, pid, s["id"], title="Welcome", body="Start here.")
    manager = manager_of(client, pid)["id"]

    too_many = call(client, manager, "preview_doc_site", site="handbook", pages=["", "a", "b"])
    assert too_many == "At most 8 screenshots per call: fewer pages, modes or devices."
    assert call(client, manager, "preview_doc_site", site="handbook", draft={"light": {"accent": "nope"}}).startswith("Invalid theme")
    order = json.loads(call(client, manager, "preview_doc_site", site="handbook", pages=["", "/welcome/"], modes=["dark"], draft={"light": {"accent": "#123456"}}, task_number=7))
    assert order["paths"] == ["/docs/handbook/", "/docs/handbook/welcome"] and order["modes"] == ["dark"] and order["devices"] == ["desktop", "phone"]
    assert order["draft"] and order["task_number"] == 7

    client.cookies.clear()
    assert client.get("/docs/handbook/").status_code == 404
    client.cookies.set(order["cookie"], order["token"])
    shown = client.get("/docs/handbook/")
    assert shown.status_code == 200 and "--accent:#123456" in shown.text and shown.headers["cache-control"] == "no-store"
    client.cookies.set(order["cookie"], "forged")
    assert client.get("/docs/handbook/").status_code == 404


def test_only_public_addresses_are_fetched(monkeypatch):
    for url in ("http://localhost/", "http://127.0.0.1:8080/", "http://10.1.2.3/", "http://169.254.169.254/latest", "http://[::1]/", "ftp://example.com/", "http://user:pw@example.com/", "http://app.internal/"):
        with pytest.raises(NotPublic):
            check_url(url)
    monkeypatch.setattr("socket.getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", ("192.168.1.5" if host == "intranet.example" else "93.184.216.34", 80))])
    assert check_url("https://shop.example/") == ("shop.example", 443)
    with pytest.raises(NotPublic):
        check_url("https://intranet.example/")


HTML = b"""<html><head><title>Shop</title><meta name="theme-color" content="#0a7d5a">
<link rel="stylesheet" href="/site.css"><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">
<link rel="icon" href="/icon.svg"><style>:root{--brand-primary:#0a7d5a;--text:#1b1b1b}</style></head>
<body><header><img src="/img/logo.png" alt="Shop logo"></header><a href="https://evil.example/">x</a></body></html>"""


def test_a_website_gives_colours_fonts_and_logos_kept_as_doc_files(client, project, monkeypatch):
    pid = client.pid = project["id"]
    s = site(client, pid)
    from plainboard.services import docs_website

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "redirect.example":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        if path == "/":
            return httpx.Response(200, content=HTML, headers={"content-type": "text/html; charset=utf-8"})
        if path == "/site.css":
            return httpx.Response(200, content=b"body{font-family:'Inter', sans-serif;color:#1b1b1b} a{color:#0a7d5a} .x{color:#0a7d5a}", headers={"content-type": "text/css"})
        if path == "/img/logo.png":
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nlogo", headers={"content-type": "image/png"})
        if path == "/icon.svg":
            return httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"})
        return httpx.Response(404)

    def checked(url: str):
        host = httpx.URL(url).host
        if host not in ("shop.example", "redirect.example"):
            raise NotPublic(f"{host} is not a public address")
        return host, 443

    monkeypatch.setattr(docs_website, "check_url", checked)
    monkeypatch.setattr(docs_website, "client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(answer), follow_redirects=False))
    manager = manager_of(client, pid)["id"]

    out = call(client, manager, "theme_from_website", site="handbook", url="shop.example", seen={"light": {"page": "rgb(255, 255, 255)", "link": "rgb(10, 125, 90)"}})
    assert out.startswith('Website https://shop.example "Shop": suggestions for the theme of handbook'), out
    assert "theme-color: #0a7d5a" in out and "--brand-primary: #0a7d5a" in out and "Rendered, light: page rgb(255, 255, 255), link rgb(10, 125, 90)" in out
    assert "#0a7d5a (3x)" in out and "'Inter', sans-serif (1x)" in out and "font_url): https://fonts.googleapis.com/css2?family=Inter" in out
    files = client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/files").json()
    assert [f["filename"] for f in files] == ["logo.png", "icon.svg"] and f"- file {files[0]['id']} logo.png (img alt='Shop logo', 1 KB)" in out
    assert "evil" not in out
    assert call(client, manager, "theme_from_website", site="handbook", url="https://redirect.example/") == "http://127.0.0.1/admin: 127.0.0.1 is not a public address. Only public http(s) websites."
    assert "Only public http(s) websites." in call(client, manager, "theme_from_website", site="handbook", url="http://localhost:8080/")
