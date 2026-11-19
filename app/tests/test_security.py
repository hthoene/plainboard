"""The findings of the penetration test of 19 Nov 2026 (PB-01 to PB-08), one test each."""

import io
import zipfile

import httpx
import pytest
from conftest import ADMIN_PASSWORD, end_job, serve_preview
from fastapi import FastAPI
from starlette.websockets import WebSocketDisconnect
from test_users import as_admin, new_user, sign_in

from plainboard.security import SESSION_COOKIE


def _site() -> FastAPI:
    site = FastAPI()

    @site.get("/{path:path}")
    async def page(path: str):
        return {"page": path}

    return site


def _config(monkeypatch, **env) -> None:
    from plainboard import config

    for name, value in env.items():
        monkeypatch.setenv(f"PLAINBOARD_{name.upper()}", value)
    config.get_config.cache_clear()


def test_pb01_only_passive_attachments_are_shown_inline(client, project):
    task = client.post(f"/api/projects/{project['id']}/tasks", json={"title": "Files"}).json()
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 20
    files = [
        ("files", ("page.html", b"<script>alert(1)</script>", "text/html")),
        ("files", ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "image/svg+xml")),
        ("files", ("fake.png", b"<html><script>alert(1)</script></html>", "image/png")),  # the uploader's type is not believed
        ("files", ("shot.png", png, "text/html")),
        ("files", ("notes.txt", b"<script>alert(1)</script>", "text/html")),
        ("files", ("doc.pdf", b"%PDF-1.4 ...", "application/pdf")),
    ]
    ids = {a["filename"]: a["id"] for a in client.post(f"/api/tasks/{task['id']}/attachments", files=files).json()}

    def get(name: str) -> httpx.Response:
        resp = client.get(f"/api/attachments/{ids[name]}")
        assert resp.status_code == 200 and resp.headers["x-content-type-options"] == "nosniff"
        return resp

    for name in ("page.html", "logo.svg", "fake.png"):
        resp = get(name)
        assert resp.headers["content-disposition"].startswith("attachment") and resp.headers["content-security-policy"] == "default-src 'none'; sandbox", name
    assert get("logo.svg").headers["content-type"] == "image/svg+xml"  # still a picture in an <img>
    shot = get("shot.png")
    assert shot.headers["content-type"] == "image/png" and shot.headers["content-disposition"].startswith("inline") and "sandbox" in shot.headers["content-security-policy"]
    assert get("notes.txt").headers["content-type"] == "text/plain; charset=utf-8"
    pdf = get("doc.pdf")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.headers["content-disposition"].startswith("inline")
    assert client.get(f"/api/attachments/{ids['shot.png']}?inline=false").headers["content-disposition"].startswith("attachment")


def test_pb02_previews_run_sandboxed_or_only_on_their_own_origin(client, project, monkeypatch):
    client.app_ref.state.preview_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=_site()))
    serve_preview(client, project["id"], 5173)
    serve_preview(client, project["id"], 5174)
    assert "content-security-policy" not in client.get("/preview/5173/").headers  # as before, unless asked for
    _config(monkeypatch, preview_sandbox="true")
    sandboxed = client.get("/preview/5173/")
    assert sandboxed.json() == {"page": ""} and "allow-same-origin" not in sandboxed.headers["content-security-policy"]
    # Every request of a sandboxed page is cross-origin, so its Referer has to keep the path (the root redirect).
    assert sandboxed.headers["referrer-policy"] == "no-referrer-when-downgrade"

    _config(monkeypatch, preview_origin="http://preview.test")
    # PlainBoard's login opens nothing there, and nothing of PlainBoard is there.
    login = {"cookie": f"{SESSION_COOKIE}={client.cookies.get(SESSION_COOKIE)}"}
    assert client.get("http://preview.test/preview/5173/", headers=login).status_code == 401
    assert client.get("http://preview.test/api/projects", headers=login).status_code == 404
    assert client.get("http://preview.test/", headers=login).status_code == 404
    # On PlainBoard's address: forwarded, with a token, to the preview origin.
    forward = client.get("/preview/5173/shop?x=1", follow_redirects=False)
    assert forward.status_code == 303 and forward.headers["location"].startswith("http://preview.test/api/public/preview/5173/enter?t=")
    entered = client.get(forward.headers["location"], follow_redirects=False)
    assert entered.status_code == 303 and entered.headers["location"] == "/preview/5173/shop?x=1"
    page = client.get("http://preview.test/preview/5173/shop?x=1")
    assert page.json() == {"page": "shop"} and "content-security-policy" not in page.headers
    # A path at the root is one the preview page asked for.
    assert client.get("http://preview.test/@vite/client", follow_redirects=False).headers["location"] == "/preview/5173/@vite/client"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"ws://preview.test/api/projects/{project['id']}/ws"):
            pass
    # The token is for one port, one session, and only good on the preview origin.
    assert client.get("http://preview.test/preview/5174/").status_code == 401
    assert client.get(forward.headers["location"].replace("http://preview.test", ""), follow_redirects=False).status_code == 401
    tampered = forward.headers["location"].replace("/5173/enter", "/5174/enter")
    assert client.get(tampered, follow_redirects=False).status_code == 401
    evil_next = forward.headers["location"].split("&next=")[0] + "&next=//evil.test/"
    assert client.get(evil_next, follow_redirects=False).headers["location"] == "/preview/5173/"
    # Signing out of PlainBoard ends it there too.
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("http://preview.test/preview/5173/").status_code == 401


def test_pb03_a_preview_opens_for_its_project_and_a_running_server_only(client, project):
    from plainboard.db import sessionmaker
    from plainboard.services import preview as preview_service

    client.app_ref.state.preview_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=_site()))
    pid = project["id"]
    other = client.post("/api/projects", json={"name": "Other"}).json()
    job = serve_preview(client, pid, 5173)
    new_user(client, "max")
    assert client.get("/preview/5173/").status_code == 200  # an admin
    assert client.get("/preview/6000/").status_code == 404  # no server of an agent runs there

    sign_in(client, "max", "max-password")
    assert client.get("/preview/5173/").status_code == 401  # signed in, but no role in that project
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/preview/5173/ws"):
            pass
    as_admin(client)
    max_id = next(u["id"] for u in client.get("/api/users").json() if u["username"] == "max")
    assert client.put(f"/api/projects/{pid}/members/{max_id}", json={"role": "viewer"}).status_code == 200
    sign_in(client, "max", "max-password")
    assert client.get("/preview/5173/").status_code == 200

    # The port is given up and another project's server takes it: a role in the first opens nothing,
    # and neither does the first project's share.
    end_job(client, job)
    assert client.get("/preview/5173/").status_code == 404
    as_admin(client)
    serve_preview(client, other["id"], 5173)
    sign_in(client, "max", "max-password")

    async def share() -> None:
        async with sessionmaker()() as session:
            await preview_service.share(session, pid, 5173, "public")

    client.portal.call(share)
    assert client.get("/preview/5173/").status_code == 401
    client.cookies.clear()
    assert client.get("/preview/5173/").status_code == 401


def test_pb04_the_login_cookie_is_secure_over_https_and_hsts_is_sent_there(client, monkeypatch):
    login = {"username": "admin", "password": ADMIN_PASSWORD}
    over_https = client.post("/api/auth/login", json=login, headers={"x-forwarded-proto": "https"})
    assert "secure" in over_https.headers["set-cookie"].lower() and over_https.headers["strict-transport-security"] == "max-age=31536000"
    over_http = client.post("/api/auth/login", json=login)
    assert "secure" not in over_http.headers["set-cookie"].lower() and "strict-transport-security" not in over_http.headers

    _config(monkeypatch, secure_cookies="true", hsts="false")
    forced = client.post("/api/auth/login", json=login)
    assert "secure" in forced.headers["set-cookie"].lower()
    assert "strict-transport-security" not in client.get("/api/health", headers={"x-forwarded-proto": "https"}).headers


def test_pb05_signing_out_ends_the_session_also_for_a_copy_of_its_cookie(client):
    from itsdangerous import URLSafeTimedSerializer

    from plainboard import security

    def use(cookie: str) -> None:
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, cookie)

    copied = client.cookies.get(SESSION_COOKIE)
    other_browser = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}).cookies.get(SESSION_COOKIE)
    use(copied)
    assert client.post("/api/auth/logout").status_code == 204
    use(copied)  # the copy, after signing out
    assert client.get("/api/auth/status").json()["user"] is None and client.get("/api/projects").status_code == 401
    use(other_browser)
    assert client.get("/api/projects").status_code == 200  # only that browser signed out
    use(URLSafeTimedSerializer(security._key, salt="plainboard-session").dumps({"u": 1, "v": 0}))  # from before sessions were kept
    assert client.get("/api/projects").status_code == 401


def test_pb06_sockets_open_only_for_pages_of_plainboard(client, project, monkeypatch):
    path = f"/api/projects/{project['id']}/ws"
    with client.websocket_connect(path, headers={"origin": "http://testserver"}):
        pass
    with client.websocket_connect(path):  # no browser: no Origin
        pass
    for origin in ("https://evil.example", "null"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path, headers={"origin": origin}):
                pass
    _config(monkeypatch, allowed_origins="https://board.example")
    with client.websocket_connect(path, headers={"origin": "https://board.example"}):
        pass
    # The preview's socket too, once access is given by a cookie.
    serve_preview(client, project["id"], 5173)
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/preview/5173/ws", headers={"origin": "https://evil.example"}):
            pass
    assert closed.value.code == 4403


def test_pb07_a_page_of_another_site_changes_nothing(client, project, monkeypatch):
    pid = project["id"]
    evil = {"origin": "https://attacker.example"}
    assert client.post("/api/auth/logout", headers=evil).status_code == 403
    assert client.post(f"/api/projects/{pid}/tasks", json={"title": "x"}, headers={"referer": "https://attacker.example/form"}).status_code == 403
    assert client.post(f"/api/projects/{pid}/tasks", json={"title": "x"}, headers={"origin": "null"}).status_code == 403
    assert client.get(f"/api/projects/{pid}/tasks", headers=evil).status_code == 200  # reading changes nothing
    assert client.post(f"/api/projects/{pid}/tasks", json={"title": "mine"}, headers={"origin": "http://testserver"}).status_code == 201
    assert client.post(f"/api/projects/{pid}/tasks", json={"title": "script"}).status_code == 201  # no browser
    # Behind a reverse proxy that names the public address.
    proxied = {"origin": "https://plainboard.example", "x-forwarded-host": "plainboard.example", "x-forwarded-proto": "https"}
    assert client.post(f"/api/projects/{pid}/tasks", json={"title": "proxied"}, headers=proxied).status_code == 201
    _config(monkeypatch, allowed_origins="https://attacker.example")
    assert client.post(f"/api/projects/{pid}/tasks", json={"title": "allowed"}, headers=evil).status_code == 201


def test_pb08_uploads_and_project_files_are_bounded_while_they_are_read(client, project, monkeypatch, tmp_path):
    from plainboard.services import transfer
    from plainboard.services.errors import ServiceError

    _config(monkeypatch, max_upload_mb="1")
    task = client.post(f"/api/projects/{project['id']}/tasks", json={"title": "Files"}).json()
    uploads = tmp_path / "uploads"
    before = set(uploads.iterdir()) if uploads.exists() else set()
    big = client.post(f"/api/tasks/{task['id']}/attachments", files=[("files", ("big.bin", b"\0" * (1024 * 1024 + 1), "application/octet-stream"))])
    assert big.status_code == 413 and set(uploads.iterdir()) == before  # nothing half-written stays
    whole = client.post(f"/api/tasks/{task['id']}/attachments", content=b"\0" * (12 * 1024 * 1024), headers={"content-type": "multipart/form-data; boundary=x"})
    assert whole.status_code == 413  # refused before the body is read

    def archive(entries: dict[str, bytes]) -> io.BytesIO:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        buffer.seek(0)
        return buffer

    monkeypatch.setattr(transfer, "MAX_ENTRIES", 3)
    with pytest.raises(ServiceError) as many:
        transfer._open(archive({f"files/{i}": b"x" for i in range(4)} | {"project.json": b"{}"}))
    assert many.value.status_code == 413
    monkeypatch.setattr(transfer, "MAX_TOTAL_MB", 1)
    with pytest.raises(ServiceError):  # a few KB that unpack to 2 MB
        transfer._open(archive({"project.json": b"{}", "files/zeros": b"\0" * (2 * 1024 * 1024)}))
    monkeypatch.setattr(transfer, "MAX_TOTAL_MB", 64)
    bomb = zipfile.ZipFile(archive({"files/zeros": b"\0" * (2 * 1024 * 1024)}))
    with pytest.raises(ServiceError):
        transfer._store(bomb, tmp_path / "in", "zeros")
    assert list((tmp_path / "in").iterdir()) == []
