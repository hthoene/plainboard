"""Tool registry: the single place where agent tools are defined.

A tool = name + description + pydantic args model + async handler + roles. Tool definitions are
sent to the runner with every run (provider-agnostic JSON schema); calls come back through the
internal API and are executed here with the same services the REST API uses.

Add a tool: create a function in one of the modules of this package and decorate it with
``@tool(...)``. Nothing else is required.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from plainboard_protocol import ToolCallResult, ToolDef
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Agent, Project
from ..services.errors import ServiceError

if TYPE_CHECKING:
    from ..orchestration.run_manager import RunContext, RunManager

log = logging.getLogger(__name__)

MANAGER = "manager"
WORKER = "worker"
ALL = (MANAGER, WORKER)


@dataclass
class ToolContext:
    session: AsyncSession
    project: Project
    agent: Agent
    run: RunContext
    runs: RunManager


class ToolError(Exception):
    pass


class NoArgs(BaseModel):
    pass


Handler = Callable[[ToolContext, Any], Awaitable[str]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Handler | None
    roles: frozenset[str]
    local: bool = False

    def definition(self) -> ToolDef:
        return ToolDef(name=self.name, description=self.description, input_schema=clean_schema(self.args_model.model_json_schema()), local=self.local)


_TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, args: type[BaseModel] = NoArgs, roles: tuple[str, ...] = ALL, local: bool = False):
    def register(fn: Handler | None) -> Handler | None:
        if name in _TOOLS:
            raise RuntimeError(f"Duplicate tool {name}")
        _TOOLS[name] = Tool(name, description, args, fn, frozenset(roles), local)
        return fn

    return register


def local_tool(name: str, description: str, args: type[BaseModel], roles: tuple[str, ...] = ALL) -> None:
    """Declare a tool that the runner executes itself (needs the agent's filesystem)."""
    tool(name, description, args, roles, local=True)(None)


def clean_schema(node: Any, mapping: bool = False) -> Any:
    """Make pydantic schemas compact - they are sent with every request.

    Drops auto-generated titles and ``default: null`` and turns ``anyOf: [X, null]`` (optional
    fields) into plain ``X``; optionality is already expressed by ``required``.
    """
    if isinstance(node, dict):
        if not mapping:
            variants = node.get("anyOf")
            if isinstance(variants, list) and len(variants) == 2 and {"type": "null"} in variants:
                (inner,) = [v for v in variants if v != {"type": "null"}]
                node = {**{k: v for k, v in node.items() if k != "anyOf"}, **inner}
            if node.get("default", 0) is None:
                node = {k: v for k, v in node.items() if k != "default"}
        out = {}
        for key, value in node.items():
            if mapping:
                out[key] = clean_schema(value)
            elif key == "title" and isinstance(value, str):
                continue
            else:
                out[key] = clean_schema(value, mapping=key in ("properties", "$defs"))
        return out
    if isinstance(node, list):
        return [clean_schema(item) for item in node]
    return node


def role_of(agent: Agent) -> str:
    return MANAGER if agent.is_manager else WORKER


def tools_for(agent: Agent) -> list[Tool]:
    role = role_of(agent)
    return [t for t in _TOOLS.values() if role in t.roles]


def tool_defs_for(agent: Agent) -> list[ToolDef]:
    return [t.definition() for t in tools_for(agent)]


def all_tools() -> list[Tool]:
    return list(_TOOLS.values())


async def execute(name: str, ctx: ToolContext, raw_args: dict[str, Any] | None) -> ToolCallResult:
    tool_ = _TOOLS.get(name)
    if tool_ is None or tool_.handler is None:
        return ToolCallResult(text=f"Unknown tool: {name}", is_error=True)
    if role_of(ctx.agent) not in tool_.roles:
        return ToolCallResult(text=f"Tool {name} is not available to you", is_error=True)
    try:
        args = tool_.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return ToolCallResult(text=f"Invalid arguments: {problems}", is_error=True)
    try:
        return ToolCallResult(text=await tool_.handler(ctx, args))
    except (ServiceError, ToolError) as exc:
        return ToolCallResult(text=str(exc), is_error=True)
    except Exception as exc:
        log.exception("Tool %s crashed", name)
        return ToolCallResult(text=f"Internal error in {name}: {exc}", is_error=True)


def _load_modules() -> None:
    from . import automation, board, integrations, knowledge, project, repos, team  # noqa: F401


_load_modules()
