"""MCP server management for the manager (external tools for the team)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..schemas import McpServerCreate, McpServerUpdate
from ..services import agents as agent_service
from ..services import mcp as mcp_service
from .registry import MANAGER, ToolContext, tool


class CreateMcpArgs(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    transport: Literal["stdio", "http", "sse"]
    command: str | None = Field(default=None, description="stdio: executable, e.g. 'npx'")
    args: list[str] = Field(default_factory=list, description="stdio: arguments")
    url: str | None = Field(default=None, description="http/sse: server URL")
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    agents: list[str] | None = Field(default=None, description="Restrict to these agents (default: all agents)")


class UpdateMcpArgs(BaseModel):
    server: str = Field(description="Server name or id")
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    enabled: bool | None = None
    agents: list[str] | None = Field(default=None, description="Restrict to these agents; [] = all agents")


class McpRef(BaseModel):
    server: str


async def _assign(ctx: ToolContext, server_id: int, refs: list[str]) -> None:
    targets = {(await agent_service.resolve(ctx.session, ctx.project.id, r)).id for r in refs}
    server = await mcp_service.get(ctx.session, server_id, ctx.project.id)
    for agent in await agent_service.list_for_project(ctx.session, ctx.project.id):
        has = any(s.id == server_id for s in agent.mcp_servers)
        if agent.id in targets and not has:
            agent.mcp_servers.append(server)
        elif agent.id not in targets and has:
            agent.mcp_servers = [s for s in agent.mcp_servers if s.id != server_id]
    await ctx.session.commit()


@tool("list_mcp_servers", "List configured MCP servers (external tools).", roles=(MANAGER,))
async def list_mcp_servers(ctx: ToolContext, _args) -> str:
    servers = await mcp_service.to_out(ctx.session, await mcp_service.list_for_project(ctx.session, ctx.project.id))
    return "\n".join(
        f"{s.id}: {s.name} [{s.transport}] {s.command or s.url} {' '.join(s.args)} | {'all agents' if s.all_agents else f'agents {s.agent_ids}'}{'' if s.enabled else ' (disabled)'}"
        for s in servers
    ) or "No MCP servers."


@tool("create_mcp_server", "Add an MCP server; its tools become available to the selected agents on their next run.", CreateMcpArgs, roles=(MANAGER,))
async def create_mcp_server(ctx: ToolContext, args: CreateMcpArgs) -> str:
    data = McpServerCreate(**args.model_dump(exclude={"agents"}), all_agents=not args.agents)
    server = await mcp_service.create(ctx.session, ctx.project.id, data)
    if args.agents:
        await _assign(ctx, server.id, args.agents)
    return f"MCP server {server.name} created."


@tool("update_mcp_server", "Change an MCP server or which agents use it.", UpdateMcpArgs, roles=(MANAGER,))
async def update_mcp_server(ctx: ToolContext, args: UpdateMcpArgs) -> str:
    server = await mcp_service.resolve(ctx.session, ctx.project.id, args.server)
    fields = args.model_dump(exclude_unset=True, exclude={"server", "agents"})
    if args.agents is not None:
        fields["all_agents"] = not args.agents
    await mcp_service.update(ctx.session, server, McpServerUpdate(**fields), ctx.project.id)
    if args.agents:
        await _assign(ctx, server.id, args.agents)
    return f"MCP server {server.name} updated."


@tool("delete_mcp_server", "Remove an MCP server.", McpRef, roles=(MANAGER,))
async def delete_mcp_server(ctx: ToolContext, args: McpRef) -> str:
    server = await mcp_service.resolve(ctx.session, ctx.project.id, args.server)
    await mcp_service.delete(ctx.session, server, ctx.project.id)
    return f"Deleted MCP server {args.server}."
