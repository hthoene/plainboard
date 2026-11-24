"""The theme previews (#128): the app's plan, one shot per page, mode and device as pictures for the model, attached to
a ticket on request; without a browser a clear answer; a website only at public addresses."""

import asyncio
import base64
import contextlib
import json

import httpx
from plainboard_protocol import ToolBridgeSpec, ToolCallResult

from plainboard_runner import doc_shots, mcp_stdio
from plainboard_runner.browser import BrowserError
from plainboard_runner.tool_bridge import LOCAL_TOOLS, ToolBridge


class FakePage:
    def __init__(self, shown: list):
        self.shown = shown

    async def send(self, method, params=None):
        self.shown.append((method, params))
        return {}

    async def show(self, url, width, height, mobile, dark):
        if url.endswith("/broken"):
            raise BrowserError("net::ERR_FAILED")
        self.shown.append((url, width, height, mobile, dark))

    async def shot(self):
        return base64.b64encode(f"jpeg {len(self.shown)}".encode()).decode()

    async def evaluate(self, expression):
        return {"values": {"page": "rgb(255, 255, 255)", "link": ""}, "logos": ["https://shop.example/logo.svg"]}


def bridge_with(answers: dict, uploads: list) -> ToolBridge:
    bridge = ToolBridge(ToolBridgeSpec(base_url="http://app:8080", run_id="1", run_token="t"), workspace=None)  # type: ignore[arg-type]

    async def remote(name, args):
        answers.setdefault("calls", []).append((name, args))
        return answers[name]

    async def upload(task_number, file):
        uploads.append((task_number, file.name, file.read_bytes()))
        return httpx.Response(200, json={"text": "ok"})

    bridge.remote, bridge.upload = remote, upload  # type: ignore[method-assign]
    return bridge


def test_the_preview_shoots_each_page_mode_and_device_and_returns_the_pictures(monkeypatch):
    shown: list = []

    @contextlib.asynccontextmanager
    async def fake(guard=False):
        yield FakePage(shown)

    monkeypatch.setattr(doc_shots, "OPEN", fake)
    plan = {"site": "guide", "paths": ["/docs/guide/", "/docs/guide/broken"], "modes": ["light", "dark"], "devices": ["desktop", "phone"], "cookie": "pb_doc_preview", "token": "tok", "draft": True, "task_number": 7}
    uploads: list = []
    bridge = bridge_with({"preview_doc_site": ToolCallResult(text=json.dumps(plan))}, uploads)
    result = asyncio.run(LOCAL_TOOLS["preview_doc_site"](bridge, {"site": "guide"}))

    assert not result.is_error and len(result.images) == 4 and result.images[0].media_type == "image/jpeg"
    lines = result.text.splitlines()
    assert lines[0] == "Screenshots of guide with the draft theme (not saved), in this order:"
    assert lines[1:5] == ["1. /docs/guide/ light desktop 1440x900", "2. /docs/guide/ light phone 390x844", "3. /docs/guide/ dark desktop 1440x900", "4. /docs/guide/ dark phone 390x844"]
    assert "- /docs/guide/broken light desktop 1440x900: failed, net::ERR_FAILED" in lines
    assert lines[-1] == "Attached to #7: guide-home-light-desktop.jpg, guide-home-light-phone.jpg, guide-home-dark-desktop.jpg, guide-home-dark-phone.jpg."
    assert shown[0] == ("Network.setCookie", {"name": "pb_doc_preview", "value": "tok", "url": "http://app:8080/", "httpOnly": True})
    assert ("http://app:8080/docs/guide/", 390, 844, True, True) in shown and [u[0] for u in uploads] == [7, 7, 7, 7]

    content = asyncio.run(_mcp_content(result))
    assert [c.type for c in content] == ["text", "image", "image", "image", "image"] and content[1].mime_type == "image/jpeg"


async def _mcp_content(result):
    class Bridge:
        tools: list = []

        async def call(self, name, args):
            return result

    server = mcp_stdio.build_server(Bridge())  # type: ignore[arg-type]
    entry = server._request_handlers["tools/call"]
    return (await entry.handler(None, type("Params", (), {"name": "preview_doc_site", "arguments": {}})())).content


def test_without_a_browser_the_preview_says_so_and_the_website_still_answers(monkeypatch):
    @contextlib.asynccontextmanager
    async def missing(guard=False):
        raise BrowserError("No browser in this runner (built with PLAINBOARD_BROWSER=0)")
        yield

    monkeypatch.setattr(doc_shots, "OPEN", missing)
    monkeypatch.setattr(doc_shots, "check_url", lambda url: ("shop.example", 443))
    plan = {"site": "guide", "paths": ["/docs/guide/"], "modes": ["light"], "devices": ["phone"], "cookie": "c", "token": "t"}
    answers = {"preview_doc_site": ToolCallResult(text=json.dumps(plan)), "theme_from_website": ToolCallResult(text="Website https://shop.example: suggestions")}
    bridge = bridge_with(answers, [])
    preview = asyncio.run(LOCAL_TOOLS["preview_doc_site"](bridge, {"site": "guide"}))
    assert preview.is_error and preview.text == "Preview not possible: No browser in this runner (built with PLAINBOARD_BROWSER=0)"
    site = asyncio.run(LOCAL_TOOLS["theme_from_website"](bridge, {"site": "guide", "url": "shop.example"}))
    assert not site.is_error and not site.images
    assert answers["calls"][-1] == ("theme_from_website", {"site": "guide", "url": "https://shop.example", "seen": {"error": "No browser in this runner (built with PLAINBOARD_BROWSER=0)"}})


def test_the_website_is_read_rendered_in_both_modes_and_only_at_a_public_address(monkeypatch):
    shown: list = []
    guarded: list = []

    @contextlib.asynccontextmanager
    async def fake(guard=False):
        guarded.append(guard)
        yield FakePage(shown)

    monkeypatch.setattr(doc_shots, "OPEN", fake)
    answers = {"theme_from_website": ToolCallResult(text="Website https://shop.example/: suggestions")}
    bridge = bridge_with(answers, [])
    refused = asyncio.run(LOCAL_TOOLS["theme_from_website"](bridge, {"site": "guide", "url": "http://127.0.0.1:8090/"}))
    assert refused.is_error and "not a public address" in refused.text and "calls" not in answers and not guarded

    monkeypatch.setattr(doc_shots, "check_url", lambda url: ("shop.example", 443))
    result = asyncio.run(LOCAL_TOOLS["theme_from_website"](bridge, {"site": "guide", "url": "https://shop.example/"}))
    assert guarded == [True] and [s[4] for s in shown] == [False, True] and len(result.images) == 1
    seen = answers["calls"][-1][1]["seen"]
    assert seen == {"light": {"page": "rgb(255, 255, 255)"}, "dark": {"page": "rgb(255, 255, 255)"}, "logos": ["https://shop.example/logo.svg"]}
