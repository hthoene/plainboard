from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import ACTIVE_RUN_STATUSES, Agent, Conversation, Job, JobStatus, McpServer, Repository, Run, RunStatus
from ..schemas import AgentCreate, AgentOut, AgentUpdate
from . import settings as settings_service
from .errors import NotFound, ServiceError
from .util import slugify

NULLABLE_FIELDS = {"effort", "max_turns"}
PALETTE = ["#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#14b8a6", "#84cc16", "#f97316"]


async def _unique_slug(session: AsyncSession, project_id: int, name: str, exclude_id: int | None = None) -> str:
    base = slugify(name, "agent")
    slug, n = base, 2
    while True:
        q = select(Agent.id).where(Agent.project_id == project_id, Agent.slug == slug)
        if exclude_id:
            q = q.where(Agent.id != exclude_id)
        if (await session.scalar(q)) is None:
            return slug
        slug, n = f"{base}-{n}", n + 1


async def _set_mcp_servers(session: AsyncSession, agent: Agent, ids: list[int]) -> None:
    servers = (await session.scalars(select(McpServer).where(McpServer.id.in_(ids)))).all() if ids else []
    for server in servers:
        if server.project_id not in (None, agent.project_id):
            raise ServiceError(f"MCP server {server.name} belongs to another project")
    agent.mcp_servers = list(servers)


async def _set_repositories(session: AsyncSession, agent: Agent, ids: list[int]) -> None:
    repos = (await session.scalars(select(Repository).where(Repository.id.in_(ids)))).all() if ids else []
    if any(r.project_id != agent.project_id for r in repos):
        raise ServiceError("Repository belongs to another project")
    agent.repositories = list(repos)


async def create(session: AsyncSession, project_id: int, data: AgentCreate, *, is_manager: bool = False) -> Agent:
    s = await settings_service.get_all(session)
    count = await session.scalar(select(func.count(Agent.id)).where(Agent.project_id == project_id)) or 0
    agent = Agent(
        project_id=project_id,
        name=data.name,
        slug=await _unique_slug(session, project_id, data.name),
        role=data.role,
        is_manager=is_manager,
        description=data.description,
        instructions=data.instructions,
        provider=data.provider or s["default_provider"],
        model=data.model or (s["default_manager_model"] if is_manager else s["default_worker_model"]),
        effort=data.effort if data.effort is not None else s["default_effort"],
        toolset=data.toolset,
        context_limit_tokens=data.context_limit_tokens or int(s["default_context_limit"]),
        keep_recent_messages=data.keep_recent_messages if data.keep_recent_messages is not None else int(s["default_keep_recent"]),
        summary_model=data.summary_model or s["default_summary_model"],
        max_turns=data.max_turns,
        color=data.color or PALETTE[count % len(PALETTE)],
        all_repos=data.all_repos,
    )
    agent.mcp_servers = []
    agent.repositories = []
    if data.mcp_server_ids:
        await _set_mcp_servers(session, agent, data.mcp_server_ids)
    if data.repository_ids:
        await _set_repositories(session, agent, data.repository_ids)
    session.add(agent)
    await session.flush()
    session.add(Conversation(agent_id=agent.id))
    await session.commit()
    await publish(session, agent)
    return agent


async def update(session: AsyncSession, agent: Agent, data: AgentUpdate) -> Agent:
    fields = data.model_dump(exclude_unset=True)
    mcp_ids = fields.pop("mcp_server_ids", None)
    repo_ids = fields.pop("repository_ids", None)
    if "name" in fields and fields["name"] != agent.name:
        agent.slug = await _unique_slug(session, agent.project_id, fields["name"], exclude_id=agent.id)
    if agent.is_manager and fields.get("enabled") is False:
        raise ServiceError("The manager cannot be disabled")
    for key, value in fields.items():
        if key == "effort" and value == "":
            value = None
        if value is None and key not in NULLABLE_FIELDS:
            continue  # "not provided" for required columns
        setattr(agent, key, value)
    if mcp_ids is not None:
        await _set_mcp_servers(session, agent, mcp_ids)
    if repo_ids is not None:
        await _set_repositories(session, agent, repo_ids)
    await session.commit()
    await publish(session, agent)
    return agent


async def delete(session: AsyncSession, agent: Agent) -> None:
    if agent.is_manager:
        raise ServiceError("The manager agent cannot be deleted")
    project_id, agent_id = agent.project_id, agent.id
    await session.delete(agent)
    await session.commit()
    bus.publish(project_id, "agent.deleted", {"id": agent_id})


async def get(session: AsyncSession, agent_id: int) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise NotFound("Agent not found")
    return agent


async def list_for_project(session: AsyncSession, project_id: int) -> list[Agent]:
    q = select(Agent).where(Agent.project_id == project_id).order_by(Agent.is_manager.desc(), Agent.id)
    return list((await session.scalars(q)).all())


async def get_manager(session: AsyncSession, project_id: int) -> Agent:
    agent = await session.scalar(select(Agent).where(Agent.project_id == project_id, Agent.is_manager.is_(True)))
    if agent is None:
        raise NotFound("Project has no manager")
    return agent


async def resolve(session: AsyncSession, project_id: int, ref: str | int) -> Agent:
    """Find an agent by id, slug or (case-insensitive) name - convenient for LLM tool calls."""
    agents = await list_for_project(session, project_id)
    text = str(ref).strip().lstrip("@").lower()
    for agent in agents:
        if str(agent.id) == text or agent.slug == text or agent.name.lower() == text:
            return agent
    names = ", ".join(a.slug for a in agents)
    raise NotFound(f"No agent '{ref}'. Available: {names}")


async def conversation_for(session: AsyncSession, agent_id: int) -> Conversation:
    conv = await session.scalar(select(Conversation).where(Conversation.agent_id == agent_id))
    if conv is None:
        conv = Conversation(agent_id=agent_id)
        session.add(conv)
        await session.commit()
    return conv


async def to_out(session: AsyncSession, agents: list[Agent]) -> list[AgentOut]:
    if not agents:
        return []
    ids = [a.id for a in agents]
    rows = (
        await session.execute(
            select(Run.agent_id, Run.status).where(Run.agent_id.in_(ids), Run.status.in_(ACTIVE_RUN_STATUSES)).distinct()
        )
    ).all()
    status: dict[int, str] = {}
    for agent_id, run_status in rows:
        if run_status == RunStatus.RUNNING or agent_id not in status:
            status[agent_id] = "running" if run_status == RunStatus.RUNNING else "queued"
    jobs = dict((await session.execute(select(Job.agent_id, func.count(Job.id)).where(Job.agent_id.in_(ids), Job.status == JobStatus.RUNNING).group_by(Job.agent_id))).all())
    convs = {c.agent_id: c for c in (await session.scalars(select(Conversation).where(Conversation.agent_id.in_(ids)))).all()}
    out = []
    for agent in agents:
        conv = convs.get(agent.id)
        item = AgentOut.model_validate(agent)
        item.mcp_server_ids = [s.id for s in agent.mcp_servers]
        item.repository_ids = [r.id for r in agent.repositories]
        item.status = status.get(agent.id, "idle")  # type: ignore[assignment]
        item.running_jobs = jobs.get(agent.id, 0)
        if conv:
            item.context_tokens = conv.context_tokens
            item.base_context_tokens = conv.session_base_tokens
            item.total_cost_usd = conv.total_cost_usd
            item.total_input_tokens = conv.total_input_tokens
            item.total_output_tokens = conv.total_output_tokens
        out.append(item)
    return out


async def publish(session: AsyncSession, agent: Agent) -> None:
    (out,) = await to_out(session, [agent])
    bus.publish(agent.project_id, "agent.upserted", out)


async def publish_by_id(session: AsyncSession, agent_id: int) -> None:
    agent = await session.get(Agent, agent_id)
    if agent is not None:
        await session.refresh(agent)
        await publish(session, agent)
