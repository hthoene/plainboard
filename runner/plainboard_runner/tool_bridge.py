"""Generic bridge between provider tool calls and the PlainBoard app.

Tool definitions (name, description, JSON schema) come from the app with each run. Remote
tools are proxied to the app's internal API; local tools run here because they need the
agent's filesystem. Providers only see :meth:`ToolBridge.call` - they stay tool-agnostic.
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from plainboard_protocol import ToolBridgeSpec, ToolCallResult, ToolDef

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
            resp = await self._client.post(f"/api/internal/runs/{self.spec.run_id}/tools/{name}", json=args)
            if resp.status_code >= 400:
                return ToolCallResult(text=f"Tool failed ({resp.status_code}): {resp.text[:500]}", is_error=True)
            return ToolCallResult.model_validate(resp.json())
        except Exception as exc:  # report to the model instead of crashing the run
            log.exception("Tool %s failed", name)
            return ToolCallResult(text=f"Tool error: {exc}", is_error=True)

    async def upload(self, task_number: int, file: Path) -> httpx.Response:
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        with file.open("rb") as fh:
            return await self._client.post(
                f"/api/internal/runs/{self.spec.run_id}/attachments",
                data={"task_number": str(task_number)},
                files={"file": (file.name, fh, mime)},
            )

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
