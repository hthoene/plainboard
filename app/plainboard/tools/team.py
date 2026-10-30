"""Team tools: agents, assignments (work orders) and inter-agent communication."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..models import Agent, Assignment, Run, Task
from ..schemas import AgentCreate, AgentUpdate, ToolsetName
from ..services import agents as agent_service
from ..services import board as board_service
from ..services import repositories as repo_service
from ..services.errors import NotFound
from ..orchestration.context import render_message
from ..services.util import truncate
from .registry import ALL, MANAGER, ToolContext, ToolError, tool
from .repos import apply_repo_access


class AgentRef(BaseModel):
    agent: str = Field(description="Agent slug, name or id")


class CreateAgentArgs(BaseModel):
    name: str
    role: str = Field(description="Short job title, e.g. 'Backend Developer'")
    description: str = Field(description="Area of responsibility; keep it narrow")
    instructions: str = Field(default="", description="Extra standing instructions")
    model: str | None = Field(default=None, description="Model id; default from settings")
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    toolset: ToolsetName = Field(default="full", description="full = coding agent; board = coordination and small tasks, smaller prompt")
    context_limit_tokens: int | None = Field(default=None, ge=5000, description="Compress context above this size")
    repositories: list[str] | None = Field(default=None, description="Repository names it may use; ['*'] = all (default)")


class UpdateAgentArgs(BaseModel):
    agent: str
    name: str | None = None
    role: str | None = None
    description: str | None = None
    instructions: str | None = None
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    toolset: ToolsetName | None = None
    context_limit_tokens: int | None = Field(default=None, ge=5000)
    summary_model: str | None = None
    enabled: bool | None = None
    repositories: list[str] | None = Field(default=None, description="Repository names it may use; ['*'] = all")


class DelegateArgs(BaseModel):
    agent: str = Field(description="Assignee slug, name or id")
    title: str = Field(description="Short title of the work order")
    instructions: str = Field(description="Self-contained instructions: goal, context, constraints, expected result")
    ticket_number: int | None = Field(default=None, description="Related ticket, if any")
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = Field(
        default=None, description="Reasoning effort for this assignment only; omit to use the agent's setting"
    )


class ListAssignmentsArgs(BaseModel):
    ticket_number: int | None = None
    agent: str | None = None
    status: Literal["queued", "running", "done", "failed", "cancelled"] | None = None
    limit: int = Field(default=20, ge=1, le=100)


class AssignmentRef(BaseModel):
    assignment_id: int


class AskArgs(BaseModel):
    agent: str
    question: str


class MessageArgs(BaseModel):
    agent: str
    message: str


class StopAgentArgs(BaseModel):
    agent: str
    reason: str = Field(default="", description="Why, in a few words; they see it")
    now: bool = Field(default=False, description="Abort at once, without a chance to save anything")
    pause: bool = Field(default=True, description="They take no new work until update_agent enabled=true")


class ReadChatArgs(BaseModel):
    agent: str
    limit: int = Field(default=20, ge=1, le=100)
    before_id: int | None = Field(default=None, description="Older messages: the id of the oldest one you have")
    include_tools: bool = False


class ListRunsArgs(BaseModel):
    agent: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


@tool("list_agents", "List the team: slug, role, model, status and area.")
async def list_agents(ctx: ToolContext, _args) -> str:
    agents = await agent_service.list_for_project(ctx.session, ctx.project.id)
    outs = await agent_service.to_out(ctx.session, agents)
    repos = await repo_service.list_for_project(ctx.session, ctx.project.id)
    names = {a.id: ", ".join(r.name for r in repo_service.accessible(a, repos)) for a in agents}
    return "\n".join(
        f"{a.slug} | {a.name} | {a.role} | {a.model} | {a.status}{'' if a.enabled else ' (disabled)'} | {truncate(a.description, 120)}"
        + (f" | repos: {names[a.id] or 'none'}" if repos and not a.is_manager else "")
        for a in outs
    )


@tool("get_agent", "Full configuration of one agent.", AgentRef, roles=(MANAGER,))
async def get_agent(ctx: ToolContext, args: AgentRef) -> str:
    agent = await agent_service.resolve(ctx.session, ctx.project.id, args.agent)
    (out,) = await agent_service.to_out(ctx.session, [agent])
    return out.model_dump_json(exclude={"project_id", "color"}, indent=1)


@tool("create_agent", "Add a specialised agent to the team.", CreateAgentArgs, roles=(MANAGER,))
async def create_agent(ctx: ToolContext, args: CreateAgentArgs) -> str:
    agent = await agent_service.create(ctx.session, ctx.project.id, AgentCreate(**args.model_dump(exclude={"repositories"})))
    access = await apply_repo_access(ctx, agent, args.repositories) if args.repositories is not None else "all repositories"
    return f"Created agent {agent.slug} ({agent.model}; {access})."


@tool("update_agent", "Change an agent's configuration (model, role, instructions, enabled ...).", UpdateAgentArgs, roles=(MANAGER,))
async def update_agent(ctx: ToolContext, args: UpdateAgentArgs) -> str:
    agent = await agent_service.resolve(ctx.session, ctx.project.id, args.agent)
    agent = await agent_service.update(ctx.session, agent, AgentUpdate(**args.model_dump(exclude_unset=True, exclude={"agent", "repositories"})))
    if args.repositories is not None:
        await apply_repo_access(ctx, agent, args.repositories)
    if args.enabled is False and agent.id != ctx.agent.id:
        await ctx.runs.wind_down(ctx.session, agent)
    return f"Updated agent {agent.slug}."


@tool("delete_agent", "Remove an agent from the team.", AgentRef, roles=(MANAGER,))
async def delete_agent(ctx: ToolContext, args: AgentRef) -> str:
    agent = await agent_service.resolve(ctx.session, ctx.project.id, args.agent)
    await agent_service.delete(ctx.session, agent)
    return f"Deleted agent {args.agent}."


@tool(
    "delegate",
    "Give an agent a clear work order (runs asynchronously). The report comes back to you as an update.",
    DelegateArgs,
    roles=(MANAGER,),
)
async def delegate(ctx: ToolContext, args: DelegateArgs) -> str:
    target = await agent_service.resolve(ctx.session, ctx.project.id, args.agent)
    task = await board_service.get_task_by_number(ctx.session, ctx.project.id, args.ticket_number) if args.ticket_number else None
    assignment = await ctx.runs.delegate(ctx.session, ctx.agent, target, args.title, args.instructions, task, effort=args.effort)
    effort = f" (effort {args.effort})" if args.effort else ""
    return f"Assignment #{assignment.id} queued for {target.slug}{effort}."


@tool("list_assignments", "List work orders, optionally by ticket, agent or status.", ListAssignmentsArgs)
async def list_assignments(ctx: ToolContext, args: ListAssignmentsArgs) -> str:
    q = select(Assignment).where(Assignment.project_id == ctx.project.id)
    if args.ticket_number:
        task = await board_service.get_task_by_number(ctx.session, ctx.project.id, args.ticket_number)
        q = q.where(Assignment.task_id == task.id)
    if args.agent:
        q = q.where(Assignment.agent_id == (await agent_service.resolve(ctx.session, ctx.project.id, args.agent)).id)
    if args.status:
        q = q.where(Assignment.status == args.status)
    rows = (await ctx.session.scalars(q.order_by(Assignment.id.desc()).limit(args.limit))).all()
    names = {a.id: a.slug for a in await agent_service.list_for_project(ctx.session, ctx.project.id)}
    tickets = {t.id: t.number for t in (await ctx.session.scalars(select(Task).where(Task.project_id == ctx.project.id))).all()}
    lines = []
    for a in rows:
        ticket = f" ticket #{tickets[a.task_id]}" if a.task_id in tickets else ""
        result = f" → {truncate(a.result, 300)}" if a.result else ""
        effort = f" (effort {a.effort})" if a.effort else ""
        lines.append(f"#{a.id} [{a.status}] {names.get(a.agent_id, a.agent_id)}{ticket}{effort}: {a.title}{result}")
    return "\n".join(lines) or "No assignments."


@tool("cancel_assignment", "Cancel a queued or running work order.", AssignmentRef, roles=(MANAGER,))
async def cancel_assignment(ctx: ToolContext, args: AssignmentRef) -> str:
    assignment = await ctx.session.get(Assignment, args.assignment_id)
    if assignment is None or assignment.project_id != ctx.project.id:
        raise NotFound(f"Assignment #{args.assignment_id} not found")
    if assignment.run_id and await ctx.runs.cancel_run(assignment.run_id):
        return f"Cancelling assignment #{assignment.id}."
    return f"Assignment #{assignment.id} is not active ({assignment.status})."


@tool("ask_agent", "Ask a teammate a question and wait for the answer (they answer from their own context).", AskArgs, roles=ALL)
async def ask_agent(ctx: ToolContext, args: AskArgs) -> str:
    target = await agent_service.resolve(ctx.session, ctx.project.id, args.agent)
    await ctx.session.commit()  # don't hold a DB transaction while waiting
    answer = await ctx.runs.ask(ctx.run, ctx.agent, target, args.question)
    return f"{target.name}: {answer}"


@tool("message_agent", "Send a teammate a message without waiting. It only reaches them after their current turn; their reply arrives as an update.", MessageArgs, roles=ALL)
async def message_agent(ctx: ToolContext, args: MessageArgs) -> str:
    target = await agent_service.resolve(ctx.session, ctx.project.id, args.agent)
    await ctx.runs.message(ctx.session, ctx.agent, target, args.message)
    return f"Message sent to {target.slug}."


@tool("stop_agent", "Stop or pause a teammate. They finish to a safe point, save their work and report to you. Queued work is dropped.", StopAgentArgs, roles=(MANAGER,))
async def stop_agent(ctx: ToolContext, args: StopAgentArgs) -> str:
    target = await agent_service.resolve(ctx.session, ctx.project.id, args.agent)
    if target.id == ctx.agent.id:
        raise ToolError("You cannot stop yourself")
    if args.pause:
        await agent_service.update(ctx.session, target, AgentUpdate(enabled=False))
    paused = " Paused until enabled again." if args.pause else ""
    if args.now:
        stopped = await ctx.runs.stop_agent(ctx.session, target)
        return f"Aborted {stopped} run(s) and job(s) of {target.slug}.{paused}"
    if await ctx.runs.wind_down(ctx.session, target, args.reason):
        return f"{target.slug} was told to wrap up after its current step; its report comes as an update.{paused}"
    return f"{target.slug} is not working.{paused}"


@tool("read_chat", "Read a teammate's conversation, newest messages last.", ReadChatArgs, roles=ALL)
async def read_chat(ctx: ToolContext, args: ReadChatArgs) -> str:
    target = await agent_service.resolve(ctx.session, ctx.project.id, args.agent)
    conv = await agent_service.conversation_for(ctx.session, target.id)
    kinds = ("text", "tool") if args.include_tools else ("text",)
    rows = await agent_service.messages(ctx.session, conv, before_id=args.before_id, limit=args.limit, kinds=kinds)
    return chr(10).join(f"#{m.id} {truncate(render_message(m), 1500)}" for m in reversed(rows)) or "No messages."


@tool("list_runs", "Recent or active agent runs (who is working on what).", ListRunsArgs, roles=(MANAGER,))
async def list_runs(ctx: ToolContext, args: ListRunsArgs) -> str:
    q = select(Run).where(Run.project_id == ctx.project.id)
    if args.agent:
        q = q.where(Run.agent_id == (await agent_service.resolve(ctx.session, ctx.project.id, args.agent)).id)
    rows = (await ctx.session.scalars(q.order_by(Run.id.desc()).limit(args.limit))).all()
    names = {a.id: a.slug for a in (await ctx.session.scalars(select(Agent).where(Agent.project_id == ctx.project.id))).all()}
    return "\n".join(
        f"run {r.id} [{r.status}] {names.get(r.agent_id, r.agent_id)} ({r.trigger}): {truncate(r.input_text, 80)}" + (f" ! {truncate(r.error, 120)}" if r.error else "")
        for r in rows
    ) or "No runs."
