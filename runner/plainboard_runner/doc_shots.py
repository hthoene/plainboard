"""Screenshots for doc site themes (#128): the local half of preview_doc_site and theme_from_website.

A headless Chromium of its own per call (a fresh profile, gone afterwards), driven over the DevTools protocol like
the browser window (browser.py). preview_doc_site asks the app which pages to open and gets a short-lived grant (a
cookie, for drafts and unpublished sites), then shoots each page per mode (prefers-color-scheme) and device and returns
the pictures to the model. theme_from_website opens a public website (every request of the page checked: public
addresses only), reads what it renders (colours, fonts, logos) and shoots it once; the app reads its HTML and CSS
and stores the logos. Without Chromium in the runner (built with PLAINBOARD_BROWSER=0) both still answer: the
preview says so, the website's suggestions come without the rendered part.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import logging
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from plainboard_protocol import ToolCallResult, ToolImage
from plainboard_protocol.netguard import NotPublic, check_url, public_ip, resolve
from websockets.asyncio.client import connect

from . import browser
from .tool_bridge import ToolBridge, local_tool

log = logging.getLogger(__name__)

DEVICES = {"desktop": (1440, 900, False), "phone": (390, 844, True)}
PAGE_SECONDS = 20  # one page until it is loaded
CALL_SECONDS = 150  # everything of one call
SETTLE_SECONDS = 0.4  # after load and fonts: transitions, the first paint of the script
QUALITY = 80


class Headless:
    """One Chromium with one page for a single call."""

    def __init__(self) -> None:
        self.cdp: browser.Cdp | None = None
        self.page = ""
        self._process: asyncio.subprocess.Process | None = None
        self._profile = Path(tempfile.mkdtemp(prefix="pb-shots-"))
        self._loaded = asyncio.Event()
        self._pump: asyncio.Task[None] | None = None
        self._guard = False
        self._hosts: dict[str, bool] = {}

    async def start(self, guard: bool = False) -> None:
        executable = browser.binary()
        if executable is None:
            raise browser.BrowserError("No browser in this runner (built with PLAINBOARD_BROWSER=0)")
        self._process = await asyncio.create_subprocess_exec(
            executable, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
            "--hide-scrollbars", "--remote-debugging-port=0", f"--user-data-dir={self._profile}", "about:blank",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        endpoint = await self._endpoint()
        self.cdp = browser.Cdp(await connect(endpoint, max_size=None, open_timeout=10, ping_interval=None))
        target = (await self.cdp.send("Target.createTarget", {"url": "about:blank"}))["targetId"]
        self.page = (await self.cdp.send("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
        self._pump = asyncio.create_task(self._events(), name="doc-shots")
        with contextlib.suppress(browser.BrowserError):
            await self.cdp.send("Browser.setDownloadBehavior", {"behavior": "deny"})
        await self.send("Page.enable")
        await self.send("Network.enable")
        await self.send("Network.setBlockedURLs", {"urls": browser.blocked_urls()})
        if guard:
            self._guard = True
            await self.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})

    async def _endpoint(self) -> str:
        port_file = self._profile / "DevToolsActivePort"
        deadline = time.monotonic() + browser.START_SECONDS
        while time.monotonic() < deadline:
            if self._process and self._process.returncode is not None:
                raise browser.BrowserError("The browser did not start")
            with contextlib.suppress(OSError):
                lines = port_file.read_text().split()
                if len(lines) >= 2:
                    return f"ws://127.0.0.1:{lines[0]}{lines[1]}"
            await asyncio.sleep(0.1)
        raise browser.BrowserError("The browser did not start in time")

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self.cdp is not None
        return await self.cdp.send(method, params, self.page)

    async def _public(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme in ("data", "blob"):
            return True
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        host = parts.hostname.lower()
        if host not in self._hosts:
            try:
                ipaddress.ip_address(host)
                self._hosts[host] = public_ip(host)
            except ValueError:
                try:
                    local = host == "localhost" or host.endswith(".localhost")
                    self._hosts[host] = not local and bool(await asyncio.to_thread(resolve, host, parts.port or (443 if parts.scheme == "https" else 80)))
                except (NotPublic, ValueError):
                    self._hosts[host] = False
        return self._hosts[host]

    async def _paused(self, params: dict[str, Any]) -> None:
        request_id = params.get("requestId")
        try:
            if await self._public(params.get("request", {}).get("url", "")):
                await self.cdp.post("Fetch.continueRequest", {"requestId": request_id}, self.page)  # type: ignore[union-attr]
            else:
                await self.cdp.post("Fetch.failRequest", {"requestId": request_id, "errorReason": "AccessDenied"}, self.page)  # type: ignore[union-attr]
        except browser.BrowserError:
            pass

    async def _events(self) -> None:
        assert self.cdp is not None
        while (event := await self.cdp.events.get()) is not None:
            method, params, _session = event
            if method == "Page.loadEventFired":
                self._loaded.set()
            elif method == "Fetch.requestPaused" and self._guard:
                asyncio.create_task(self._paused(params))
            elif method == "Page.javascriptDialogOpening":
                with contextlib.suppress(browser.BrowserError):
                    await self.cdp.post("Page.handleJavaScriptDialog", {"accept": False}, self.page)

    async def show(self, url: str, width: int, height: int, mobile: bool, dark: bool) -> None:
        await self.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": mobile})
        await self.send("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": "dark" if dark else "light"}]})
        self._loaded.clear()
        result = await self.send("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise browser.BrowserError(f"{url}: {result['errorText']}")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._loaded.wait(), PAGE_SECONDS)
        with contextlib.suppress(browser.BrowserError, TimeoutError):
            await asyncio.wait_for(self.send("Runtime.evaluate", {"expression": "document.fonts.ready.then(() => true)", "awaitPromise": True}), 5)
        await asyncio.sleep(SETTLE_SECONDS)

    async def shot(self) -> str:
        return (await self.send("Page.captureScreenshot", {"format": "jpeg", "quality": QUALITY}))["data"]

    async def evaluate(self, expression: str) -> Any:
        result = await self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return result.get("result", {}).get("value")

    async def stop(self) -> None:
        if self._pump:
            self._pump.cancel()
        if self.cdp:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.cdp.send("Browser.close"), timeout=3)
            await self.cdp.close()
        if self._process and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._process.wait(), timeout=5)
        shutil.rmtree(self._profile, ignore_errors=True)


@contextlib.asynccontextmanager
async def headless(guard: bool = False) -> AsyncIterator[Headless]:
    one = Headless()
    try:
        await one.start(guard)
        yield one
    finally:
        await one.stop()


OPEN = headless  # what the tools open: tests put a fake browser here


async def _attach(bridge: ToolBridge, task_number: int, shots: list[tuple[str, str]]) -> str:
    done = []
    with tempfile.TemporaryDirectory(prefix="pb-shots-") as folder:
        for name, data in shots:
            file = Path(folder) / name
            file.write_bytes(base64.b64decode(data))
            resp = await bridge.upload(task_number, file)
            if resp.status_code >= 400:
                return f"Attaching to #{task_number} failed: {resp.text[:200]}"
            done.append(name)
    return f"Attached to #{task_number}: {', '.join(done)}."


@local_tool("preview_doc_site")
async def _preview_doc_site(bridge: ToolBridge, args: dict[str, Any]) -> ToolCallResult:
    plan = await bridge.remote("preview_doc_site", args)
    if plan.is_error:
        return plan
    order = json.loads(plan.text)
    base = bridge.spec.base_url.rstrip("/")
    shots: list[tuple[str, str]] = []  # (file name, base64 JPEG)
    lines: list[str] = []
    try:
        async with asyncio.timeout(CALL_SECONDS), OPEN() as page:
            await page.send("Network.setCookie", {"name": order["cookie"], "value": order["token"], "url": base + "/", "httpOnly": True})
            for path in order["paths"]:
                for mode in order["modes"]:
                    for device in order["devices"]:
                        width, height, mobile = DEVICES[device]
                        label = f"{path} {mode} {device} {width}x{height}"
                        try:
                            await page.show(base + path, width, height, mobile, mode == "dark")
                            name = path.removeprefix(f"/docs/{order['site']}").strip("/").replace("/", "-")[:60] or "home"
                            shots.append((f"{order['site']}-{name}-{mode}-{device}.jpg", await page.shot()))
                            lines.append(f"{len(shots)}. {label}")
                        except browser.BrowserError as exc:
                            lines.append(f"- {label}: failed, {exc}")
    except browser.BrowserError as exc:
        return ToolCallResult(text=f"Preview not possible: {exc}", is_error=True)
    except TimeoutError:
        lines.append(f"(stopped after {CALL_SECONDS} s)")
    if not shots:
        return ToolCallResult(text="No screenshot made:\n" + "\n".join(lines), is_error=True)
    head = f"Screenshots of {order['site']}" + (" with the draft theme (not saved)" if order.get("draft") else "") + ", in this order:"
    if order.get("task_number"):
        lines.append(await _attach(bridge, int(order["task_number"]), shots))
    return ToolCallResult(text="\n".join([head, *lines]), images=[ToolImage(media_type="image/jpeg", data=data) for _, data in shots])


# What the website renders: the colours of its page, text, links, buttons and header, its fonts and corners, the
# logo's address. Plain values only; nothing of the page is changed.
READ_BRAND = r"""(() => {
  const cs = (el) => el ? getComputedStyle(el) : null;
  const color = (v) => v && v !== "rgba(0, 0, 0, 0)" && v !== "transparent" ? v : "";
  const body = document.body, root = document.documentElement;
  const header = document.querySelector("header, [role=banner], nav");
  const link = document.querySelector("main a[href], article a[href], p a[href]") || document.querySelector("a[href]");
  const button = [...document.querySelectorAll("a[class*=btn], a[class*=button], button, [class*=cta]")].find((b) => color(cs(b).backgroundColor) && b.offsetWidth > 40);
  const h1 = document.querySelector("h1");
  const out = {
    page: color(cs(body).backgroundColor) || color(cs(root).backgroundColor), text: color(cs(body).color),
    link: link ? color(cs(link).color) : "", button: button ? color(cs(button).backgroundColor) : "", button_text: button ? color(cs(button).color) : "",
    button_radius: button ? cs(button).borderRadius : "", header: header ? color(cs(header).backgroundColor) : "",
    font: cs(body).fontFamily, heading_font: h1 ? cs(h1).fontFamily : "", heading_color: h1 ? color(cs(h1).color) : "",
  };
  const logos = [];
  for (const img of document.querySelectorAll("header img, nav img, a[href='/'] img, img[alt*=logo i], img[class*=logo i], img[src*=logo i]")) {
    if (img.currentSrc && !img.currentSrc.startsWith("data:") && !logos.includes(img.currentSrc)) logos.push(img.currentSrc);
  }
  return {values: out, logos: logos.slice(0, 5)};
})()"""


@local_tool("theme_from_website")
async def _theme_from_website(bridge: ToolBridge, args: dict[str, Any]) -> ToolCallResult:
    url = str(args.get("url", "")).strip()
    if url and "://" not in url:
        url = "https://" + url
    try:
        await asyncio.to_thread(check_url, url)
    except NotPublic as exc:
        return ToolCallResult(text=f"{url}: {exc}. Only public http(s) websites.", is_error=True)
    seen: dict[str, Any] = {}
    picture = ""
    try:
        async with asyncio.timeout(60), OPEN(guard=True) as page:
            width, height, _ = DEVICES["desktop"]
            for mode in ("light", "dark"):
                await page.show(url, width, height, False, mode == "dark")
                found = await page.evaluate(READ_BRAND) or {}
                seen[mode] = {k: str(v)[:120] for k, v in (found.get("values") or {}).items() if v}
                seen["logos"] = list(dict.fromkeys([*seen.get("logos", []), *(str(x)[:500] for x in found.get("logos") or [])]))[:10]
                if mode == "light":
                    picture = await page.shot()
    except (browser.BrowserError, TimeoutError) as exc:
        seen = {"error": str(exc) or "timed out"}
    result = await bridge.remote("theme_from_website", {**args, "url": url, "seen": seen})
    if picture and not result.is_error:
        result.text += "\nThe website as rendered (light, 1440x900) is attached."
        result.images.append(ToolImage(media_type="image/jpeg", data=picture))
    return result
