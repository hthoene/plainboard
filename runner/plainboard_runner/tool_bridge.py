"""Generic bridge between provider tool calls and the PlainBoard app.

Tool definitions (name, description, JSON schema) come from the app with each run. Remote
tools are proxied to the app's internal API; local tools run here because they need the
agent's filesystem. Providers only see :meth:`ToolBridge.call` - they stay tool-agnostic.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from plainboard_protocol import TOOL_PENDING_HEADER, ToolBridgeSpec, ToolCallResult, ToolDef

from . import snapshots

log = logging.getLogger(__name__)

LocalTool = Callable[["ToolBridge", dict[str, Any]], Awaitable[ToolCallResult]]
LOCAL_TOOLS: dict[str, LocalTool] = {}


def local_tool(name: str) -> Callable[[LocalTool], LocalTool]:
    def register(fn: LocalTool) -> LocalTool:
        LOCAL_TOOLS[name] = fn
        return fn

    return register


class ToolBridge:
    def __init__(self, spec: ToolBridgeSpec, workspace: Path, timeout: float = 900):
        self.spec = spec
        self.workspace = workspace
        self._defs = {t.name: t for t in spec.tools}
        # ask_agent may block while another agent works, hence the generous timeout.
        self._client = httpx.AsyncClient(
            base_url=spec.base_url,
            headers={"Authorization": f"Bearer {spec.run_token}"},
            timeout=httpx.Timeout(timeout, connect=10),
        )

    @property
    def tools(self) -> list[ToolDef]:
        return list(self._defs.values())

    async def call(self, name: str, args: dict[str, Any]) -> ToolCallResult:
        tool = self._defs.get(name)
        if tool is None:
            return ToolCallResult(text=f"Unknown tool: {name}", is_error=True)
        try:
            if tool.local:
                handler = LOCAL_TOOLS.get(name)
                if handler is None:
                    return ToolCallResult(text=f"Local tool not available: {name}", is_error=True)
                return await handler(self, args)
            return await self.remote(name, args)
        except Exception as exc:  # report to the model instead of crashing the run
            log.exception("Tool %s failed", name)
            return ToolCallResult(text=f"Tool error: {exc}", is_error=True)

    async def remote(self, name: str, args: dict[str, Any]) -> ToolCallResult:
        """The tool's call, executed by the app."""
        base = f"/api/internal/runs/{self.spec.run_id}/tools"
        resp = await self._client.post(f"{base}/{name}", json=args, headers={TOOL_PENDING_HEADER: "1"})
        while True:
            if resp.status_code >= 400:
                return ToolCallResult(text=f"Tool failed ({resp.status_code}): {resp.text[:500]}", is_error=True)
            result = ToolCallResult.model_validate(resp.json())
            if not result.pending:
                return result
            # Still at work (a quick check, a question to another agent): each request stays short, so
            # a proxy between here and the app (Cloudflare ends one after 100 s) never cuts it off.
            resp = await self._client.post(f"{base}/pending/{result.pending}")

    async def upload(self, task_number: int, file: Path) -> httpx.Response:
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        with file.open("rb") as fh:
            return await self._client.post(
                f"/api/internal/runs/{self.spec.run_id}/attachments",
                data={"task_number": str(task_number)},
                files={"file": (file.name, fh, mime)},
            )

    async def skill_archive(self, skill_id: int) -> bytes:
        """The zip of one of the turn's skills (runner skills.py)."""
        resp = await self._client.get(f"/api/internal/runs/{self.spec.run_id}/skills/{skill_id}", timeout=120)
        resp.raise_for_status()
        return resp.content

    async def aclose(self) -> None:
        await self._client.aclose()


@local_tool("attach_file")
async def _attach_file(bridge: ToolBridge, args: dict[str, Any]) -> ToolCallResult:
    raw = Path(str(args.get("path", "")))
    file = raw if raw.is_absolute() else bridge.workspace / raw
    if not file.is_file():
        return ToolCallResult(text=f"File not found: {file}", is_error=True)
    resp = await bridge.upload(int(args["task_number"]), file)
    if resp.status_code >= 400:
        return ToolCallResult(text=f"Upload failed: {resp.text[:300]}", is_error=True)
    return ToolCallResult.model_validate(resp.json())


@local_tool("attach_doc_file")
async def _attach_doc_file(bridge: ToolBridge, args: dict[str, Any]) -> ToolCallResult:
    raw = Path(str(args.get("path", "")))
    file = raw if raw.is_absolute() else bridge.workspace / raw
    if not file.is_file():
        return ToolCallResult(text=f"File not found: {file}", is_error=True)
    mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    with file.open("rb") as fh:
        resp = await bridge._client.post(f"/api/internal/runs/{bridge.spec.run_id}/doc-files", data={"site": str(args.get("site", ""))}, files={"file": (file.name, fh, mime)})
    if resp.status_code >= 400:
        return ToolCallResult(text=f"Upload failed: {resp.text[:300]}", is_error=True)
    return ToolCallResult.model_validate(resp.json())


@local_tool("send_files")
async def _send_files(bridge: ToolBridge, args: dict[str, Any]) -> ToolCallResult:
    paths = [p if p.is_absolute() else bridge.workspace / p for p in (Path(str(raw)) for raw in args.get("paths") or [])]
    if missing := [str(p) for p in paths if not p.is_file()]:
        return ToolCallResult(text=f"File not found: {', '.join(missing)}", is_error=True)
    ids = ",".join(str(int(i)) for i in args.get("attachment_ids") or [])
    files = [("files", (p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream")) for p in paths]
    resp = await bridge._client.post(f"/api/internal/runs/{bridge.spec.run_id}/chat-files", data={"attachment_ids": ids}, files=files or None)
    if resp.status_code >= 400:
        return ToolCallResult(text=f"Sending failed: {resp.text[:300]}", is_error=True)
    return ToolCallResult.model_validate(resp.json())


SKILL_FILE_BYTES = 5 * 1024 * 1024  # the app's limit for a skill's zip (services/skills.py)


@local_tool("install_skill")
async def _install_skill(bridge: ToolBridge, args: dict[str, Any]) -> ToolCallResult:
    """A skill from a file: its content goes to the app, which checks and keeps it; a git source goes as it is."""
    args = {k: v for k, v in args.items() if k != "content_b64"}
    if args.get("file"):
        raw = Path(str(args["file"]))
        file = raw if raw.is_absolute() else bridge.workspace / raw
        if not file.is_file():
            return ToolCallResult(text=f"File not found: {file}", is_error=True)
        if file.stat().st_size > SKILL_FILE_BYTES:
            return ToolCallResult(text=f"A skill may be at most {SKILL_FILE_BYTES // (1024 * 1024)} MB", is_error=True)
        args["content_b64"] = base64.b64encode(file.read_bytes()).decode()
    return await bridge.remote("install_skill", args)


@local_tool("workspace_snapshots")
async def _workspace_snapshots(bridge: ToolBridge, args: dict[str, Any]) -> ToolCallResult:
    repo = str(args.get("repo") or "")
    if not args.get("restore"):
        return ToolCallResult(text=await asyncio.to_thread(snapshots.describe, bridge.workspace, repo or None))
    try:
        text = await asyncio.to_thread(snapshots.restore, bridge.workspace, repo, str(args["restore"]), args.get("mode") == "stash", args.get("branch") or None)
    except snapshots.SnapshotError as exc:
        return ToolCallResult(text=str(exc), is_error=True)
    return ToolCallResult(text=text)


from . import code_read, doc_shots  # noqa: E402,F401  (register the code tools of a doc site's assistant, the theme previews)
