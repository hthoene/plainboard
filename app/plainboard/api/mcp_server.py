"""PlainBoard as an MCP server for a person's own AI client (Claude Desktop, Claude Code, Cursor, ...).

``/mcp`` speaks MCP over Streamable HTTP (the official SDK, stateless, JSON answers: no request stays
open, behind Cloudflare's 100 s limit). It takes a personal token (services/personal_tokens.py) as
Bearer and never the login cookie, so a page of another site can't use it with the browser's login.

Every tool acts as the token's person with exactly their rights: what changes something goes through
the REST endpoint the UI uses, in this process (same rules in services/access.py, same side effects);
what reads asks ``access.check`` for the route the UI reads it from, then the services. Agent tools that
format the same thing compactly are reused. Each call is in the audit (``models.McpCall``).
"""

import base64
import functools
import inspect
import mimetypes
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Annotated, Any, Literal

import anyio
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field
from sqlalchemy import select
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from ..db import sessionmaker
from ..models import Attachment, Decision, DecisionStatus, Project, Run, RunStatus, Task
from ..schemas import CustomerTextIn
from ..services import access
from ..services import agents as agent_service
from ..services import board as board_service
from ..services import landings as landing_service
from ..services import personal_tokens as token_service
from ..services import search as search_service
from ..services import users as user_service
from ..services.access import Principal
from ..services.errors import ServiceError
from ..services.util import truncate
from ..tools import board as board_tools
from ..tools import team as team_tools
from ..tools.registry import ToolError as AgentToolError
from .guard import is_https

PATH = "/mcp"
WAIT_MAX = 50  # seconds a tool may wait: far below Cloudflare's 100 s
TEXT_INLINE = 64 * 1024  # a text attachment up to this size comes back as text
UPLOAD_MAX = 10 * 1024 * 1024
INSTRUCTIONS = (
    "PlainBoard: a Kanban board with a team of AI agents per project. You act as the person whose token this is, with their rights. "
    "Start with list_projects; tickets are named by project and number (#12). The manager plans and delegates: talk to it with chat, "
    "then wait_for_reply. Tickets, comments and decisions are read by the agents."
)


@dataclass(frozen=True)
class Caller:
    app: Any  # the FastAPI app, for its own endpoints
    principal: Principal
    secret: str
    base_url: str  # PlainBoard's address as the client sees it


_caller: ContextVar[Caller | None] = ContextVar("mcp_caller", default=None)
_project_of_call: ContextVar[int | None] = ContextVar("mcp_project", default=None)


def caller() -> Caller:
    found = _caller.get()
    if found is None:
        raise ToolError("Not authenticated")
    return found


# --- what the tools share -------------------------------------------------------------------


async def api(method: str, path: str, *, json: Any = None, params: dict | None = None, files: Any = None) -> Any:
    """One of PlainBoard's own endpoints, as the caller: its rule, its checks, its side effects."""
    c = caller()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url="http://plainboard.internal", timeout=WAIT_MAX) as client:
        response = await client.request(method, path, json=json, params=params, files=files, headers={"Authorization": f"Bearer {c.secret}"})
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        if isinstance(detail, list):  # a validation error: which fields
            detail = "; ".join(f"{'.'.join(str(x) for x in d.get('loc', [])[1:])}: {d.get('msg')}" for d in detail)
        raise ToolError(str(detail or f"HTTP {response.status_code}"))
    return response.json() if response.content else None


async def check(session, method: str, route: str, **params: Any) -> None:  # noqa: ANN001
    """Whether the caller may do this through the route of the REST API (services/access.RULES)."""
    try:
        await access.check(session, caller().principal, method, route, params)
    except ServiceError as exc:
        raise ToolError(str(exc)) from None


async def project_of(session, ref: str | int) -> Project:  # noqa: ANN001
    """A project of the caller by id, slug or name."""
    user = caller().principal.user
    assert user is not None
    mine = None if user.is_admin else set(await user_service.roles(session, user))
    text = str(ref).strip().lower()
    for project in await session.scalars(select(Project).order_by(Project.id)):
        if (mine is None or project.id in mine) and text in (str(project.id), project.slug.lower(), project.name.lower()):
            _project_of_call.set(project.id)
            await check(session, "GET", "/api/projects/{project_id}", project_id=project.id)
            return project
    raise ToolError(f"No project '{ref}' of yours: see list_projects")


async def ticket_of(session, project: Project, number: int) -> Task:  # noqa: ANN001
    task = await board_service.get_task_by_number(session, project.id, number)
    await check(session, "GET", "/api/tasks/{task_id}", task_id=task.id)
    return task


def agent_ctx(session, project: Project) -> SimpleNamespace:  # noqa: ANN001
    """What the agent tools that only read need of their context."""
    return SimpleNamespace(session=session, project=project, runs=caller().app.state.runs)


def attachment_url(attachment: Attachment) -> str:
    return f"{caller().base_url}/api/attachments/{attachment.id}?inline=false"


ProjectRef = Annotated[str, Field(description="Project id, slug or name")]
Number = Annotated[int, Field(description="Ticket number")]
_TOOLS: list[tuple[Callable, str, bool]] = []


def tool(description: str, read: bool = True):  # noqa: ANN201
    """A tool of the server: short descriptions, they cost the client's tokens. Errors of the services and the
    API come back as the tool's error; every call goes into the audit."""

    def register(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def run(*args: Any, **kwargs: Any) -> Any:
            c = caller()
            _project_of_call.set(None)
            error = ""
            try:
                return await fn(*args, **kwargs)
            except (ServiceError, AgentToolError) as exc:
                error = str(exc)
                raise ToolError(error) from None
            except ToolError as exc:
                error = str(exc)
                raise
            except Exception as exc:
                error = type(exc).__name__
                raise
            finally:
                assert c.principal.user is not None and c.principal.token_id is not None
                async with sessionmaker()() as session:
                    await token_service.audit(session, c.principal.user.id, c.principal.token_id, fn.__name__, _project_of_call.get(), error)

        run.__signature__ = inspect.signature(fn)  # the client sees the tool's own arguments
        _TOOLS.append((run, description, read))
        return fn

    return register


# --- projects and the board -----------------------------------------------------------------


@tool("Your projects: id, slug, name, your role.")
async def list_projects() -> str:
    rows = await api("GET", "/api/projects")
    return "\n".join(f"{p['id']} {p['slug']} | {p['name']} | {p.get('role') or 'admin'}" + (" (paused)" if p.get("paused_at") else "") for p in rows) or "No projects."


@tool("Ticket counts per status plus all open tickets.")
async def board_summary(project: ProjectRef) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
        await check(session, "GET", "/api/projects/{project_id}/tasks", project_id=p.id)
        return await board_tools.get_board_summary(agent_ctx(session, p), None)


@tool("List or search tickets (compact). Filter by status, priority, label or text.")
async def list_tickets(
    project: ProjectRef,
    status: Literal["backlog", "todo", "in_progress", "review", "done"] | None = None,
    priority: Literal["low", "medium", "high", "urgent"] | None = None,
    label: str | None = None,
    query: Annotated[str | None, Field(description="Text in title/description")] = None,
    archived: Annotated[bool, Field(description="Only archived tickets")] = False,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
        await check(session, "GET", "/api/projects/{project_id}/tasks", project_id=p.id)
        args = board_tools.ListTasksArgs(status=status, priority=priority, label=label, query=query, archived=archived, limit=limit, offset=offset)
        return await board_tools.list_tasks(agent_ctx(session, p), args)


@tool("Full ticket: description, customer texts, attachments (download URLs), open decisions, last 10 comments, assignments with reports.")
async def get_ticket(project: ProjectRef, number: Number) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
        task = await ticket_of(session, p, number)
        text = await board_service.ticket_text(session, task, comment_limit=10, path=attachment_url)
        customer = "\n".join(f"Customer {k}: {v}" for k, v in (("title", task.customer_title), ("description", task.customer_description)) if v)
        for lang, other in (task.customer_texts or {}).items():
            customer += "".join(f"\nCustomer {k} ({lang}): {v}" for k, v in (("title", other.get("title")), ("description", other.get("description"))) if v)
        return text + (f"\n\n{customer.strip()}" if customer.strip() else "")


@tool("Older ticket comments, page by page (newest first by offset).")
async def get_comments(project: ProjectRef, number: Number, offset: Annotated[int, Field(ge=0)] = 0, limit: Annotated[int, Field(ge=1, le=50)] = 10) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
        await ticket_of(session, p, number)
        return await board_tools.get_comments(agent_ctx(session, p), board_tools.GetCommentsArgs(number=number, offset=offset, limit=limit))


@tool("Create a ticket.", read=False)
async def create_ticket(
    project: ProjectRef,
    title: Annotated[str, Field(min_length=1, max_length=300)],
    description: str = "",
    status: Literal["backlog", "todo", "in_progress", "review", "done"] = "backlog",
    priority: Literal["low", "medium", "high", "urgent"] = "medium",
    labels: list[str] | None = None,
    customer_title: Annotated[str, Field(description="Public title for end users")] = "",
    customer_description: Annotated[str, Field(description="Public text for end users")] = "",
    customer_texts: Annotated[dict[str, CustomerTextIn] | None, Field(description=board_tools.CUSTOMER_TEXTS)] = None,
) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
    body = {"title": title, "description": description, "status": status, "priority": priority, "labels": labels or [], "customer_title": customer_title, "customer_description": customer_description}
    if customer_texts:
        body["customer_texts"] = {lang: t.model_dump(exclude_none=True) for lang, t in customer_texts.items()}
    t = await api("POST", f"/api/projects/{p.id}/tasks", json=body)
    return f"Created #{t['number']} [{t['status']}] {t['title']}"


@tool("Change a ticket: fields, move (status), labels, priority, customer texts, archive.", read=False)
async def update_ticket(
    project: ProjectRef,
    number: Number,
    title: str | None = None,
    description: str | None = None,
    status: Literal["backlog", "todo", "in_progress", "review", "done"] | None = None,
    priority: Literal["low", "medium", "high", "urgent"] | None = None,
    labels: Annotated[list[str] | None, Field(description="The full new list")] = None,
    customer_title: str | None = None,
    customer_description: str | None = None,
    customer_texts: Annotated[dict[str, CustomerTextIn] | None, Field(description=board_tools.CUSTOMER_TEXTS)] = None,
    archived: bool | None = None,
) -> str:
    async with sessionmaker()() as session:
        task = await ticket_of(session, await project_of(session, project), number)
    fields = {"title": title, "description": description, "status": status, "priority": priority, "labels": labels, "customer_title": customer_title, "customer_description": customer_description, "archived": archived}
    if customer_texts:
        fields["customer_texts"] = {lang: t.model_dump(exclude_none=True) for lang, t in customer_texts.items()}
    body = {k: v for k, v in fields.items() if v is not None}
    if not body:
        raise ToolError("Nothing to change.")
    t = await api("PATCH", f"/api/tasks/{task.id}", json=body)
    return f"Updated #{t['number']} [{t['status']}{', archived' if t.get('archived_at') else ''}] ({t['priority']}) {t['title']}"


@tool("Hand a ticket to the manager to work on it now.", read=False)
async def start_ticket(project: ProjectRef, number: Number) -> str:
    async with sessionmaker()() as session:
        task = await ticket_of(session, await project_of(session, project), number)
    run = await api("POST", f"/api/tasks/{task.id}/start")
    return f"#{number} handed to the manager (run {run['id']})."


@tool("Comment on a ticket. The agents read it.", read=False)
async def add_comment(project: ProjectRef, number: Number, body: Annotated[str, Field(min_length=1)]) -> str:
    async with sessionmaker()() as session:
        task = await ticket_of(session, await project_of(session, project), number)
    await api("POST", f"/api/tasks/{task.id}/comments", json={"body": body})
    return f"Comment added to #{number}."


@tool("Mark what a ticket waited for you to do (user action) as done.", read=False)
async def user_action_done(project: ProjectRef, number: Number, note: Annotated[str, Field(description="What you did or found")] = "") -> str:
    async with sessionmaker()() as session:
        task = await ticket_of(session, await project_of(session, project), number)
    await api("POST", f"/api/tasks/{task.id}/user-action/done", json={"note": note})
    return f"#{number}: user action done."


# --- decisions ------------------------------------------------------------------------------


@tool("Open decisions the team waits for: id, ticket, question, numbered options.")
async def list_decisions(project: ProjectRef) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
        await check(session, "GET", "/api/projects/{project_id}/tasks", project_id=p.id)
        rows = (await session.execute(
            select(Decision, Task.number).join(Task, Task.id == Decision.task_id).where(Decision.project_id == p.id, Decision.status == DecisionStatus.OPEN).order_by(Decision.id)
        )).all()  # fmt: skip
    lines = []
    for d, number in rows:
        options = "".join(f"\n  {i}. {o['label']}" + (f": {truncate(o.get('description') or '', 200)}" if o.get("description") else "") for i, o in enumerate(d.options, 1))
        lines.append(f"Decision {d.id} on #{number}: {d.question}" + (f"\n  {truncate(d.details, 600)}" if d.details else "") + options)
    return "\n".join(lines) or "No open decisions."


@tool("Answer a decision: an option's number, and/or your own words.", read=False)
async def answer_decision(decision_id: int, option: Annotated[int | None, Field(ge=1, description="Number of the chosen option")] = None, text: str = "") -> str:
    if option is None and not text.strip():
        raise ToolError("Give an option or a text.")
    async with sessionmaker()() as session:
        decision = await session.get(Decision, decision_id)
        _project_of_call.set(decision.project_id if decision else None)
    await api("POST", f"/api/decisions/{decision_id}/answer", json={"option_index": option - 1 if option else None, "text": text})
    return f"Decision {decision_id} answered."


# --- the team -------------------------------------------------------------------------------


@tool("The team: slug, role, model, status and area.")
async def list_agents(project: ProjectRef) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
        await check(session, "GET", "/api/projects/{project_id}/agents", project_id=p.id)
        return await team_tools.list_agents(agent_ctx(session, p), None)


@tool("Send a message to the project's manager (or a member, if allowed). Returns a run id: get the answer with wait_for_reply.", read=False)
async def chat(project: ProjectRef, message: Annotated[str, Field(min_length=1)], agent: Annotated[str, Field(description="Slug or name")] = "manager") -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
        target = await agent_service.resolve(session, p.id, agent)
    run = await api("POST", f"/api/agents/{target.id}/messages", json={"text": message})
    return f"Sent to {target.slug} (run {run['id']}, {run['status']}). Answer: wait_for_reply(run_id={run['id']})."


@tool(f"Wait up to {WAIT_MAX} s for an agent's turn to finish and return its answer; call again while it still works.")
async def wait_for_reply(run_id: int, wait_seconds: Annotated[int, Field(ge=0, le=WAIT_MAX)] = 45) -> str:
    deadline = anyio.current_time() + wait_seconds
    while True:
        async with sessionmaker()() as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise ToolError("No such run.")
            _project_of_call.set(run.project_id)
            await check(session, "GET", "/api/runs/{run_id}", run_id=run_id)
            status, answer, error = run.status, run.result_text, run.error
        if status not in (RunStatus.QUEUED, RunStatus.RUNNING) or anyio.current_time() >= deadline:
            break
        await anyio.sleep(1)
    if status in (RunStatus.QUEUED, RunStatus.RUNNING):
        return f"Run {run_id} is still {status}: call wait_for_reply again."
    if status == RunStatus.SUCCEEDED:
        return answer or "(finished without a text answer)"
    return f"Run {run_id} {status}" + (f": {error}" if error else ".")


@tool("Read an agent's chat (the manager or a member), newest messages last.")
async def read_chat(
    project: ProjectRef,
    agent: Annotated[str, Field(description="Slug or name")] = "manager",
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    before_id: Annotated[int | None, Field(description="Older: the id of the oldest message you have")] = None,
    include_tools: bool = False,
) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
        target = await agent_service.resolve(session, p.id, agent)
        await check(session, "GET", "/api/agents/{agent_id}/conversation", agent_id=target.id)
        args = team_tools.ReadChatArgs(agent=str(target.id), limit=limit, before_id=before_id, include_tools=include_tools)
        return await team_tools.read_chat(agent_ctx(session, p), args)


@tool("Work orders (assignments) with their reports, by ticket, agent or status.")
async def list_assignments(
    project: ProjectRef,
    ticket: Annotated[int | None, Field(description="Ticket number")] = None,
    agent: str | None = None,
    status: Literal["queued", "running", "done", "failed", "cancelled"] | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
        await check(session, "GET", "/api/projects/{project_id}/assignments", project_id=p.id)
        args = team_tools.ListAssignmentsArgs(ticket_number=ticket, agent=agent, status=status, limit=limit)
        return await team_tools.list_assignments(agent_ctx(session, p), args)


@tool("Recent agent runs (turns): who worked on what, with errors.")
async def list_runs(project: ProjectRef, agent: str | None = None, limit: Annotated[int, Field(ge=1, le=50)] = 10) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
        await check(session, "GET", "/api/projects/{project_id}/runs", project_id=p.id)
        return await team_tools.list_runs(agent_ctx(session, p), team_tools.ListRunsArgs(agent=agent, limit=limit))


@tool("What is in flight: open assignments, branches on their way to the base branch (landing, trains), running turns, red base branches.")
async def in_flight(project: ProjectRef) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
        await check(session, "GET", "/api/projects/{project_id}/runs", project_id=p.id)
        lines, more = await board_service.in_flight(session, p.id)
        names = await board_service.agent_names(session, p.id)
        active = await session.scalars(select(Run).where(Run.project_id == p.id, Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING))).order_by(Run.id))
        turns = [f"- run {r.id} [{r.status}] {names.get(r.agent_id, r.agent_id)} ({r.trigger}): {truncate(r.input_text, 80)}" for r in active]
        red = [r.name for r in await landing_service.red_repositories(session, p.id)]
    parts = (["Work and branches:", *lines] + ([f"(+{more} more)"] if more else [])) if lines else ["Nothing ordered."]
    if turns:
        parts += ["Turns:", *turns]
    if red:
        parts.append(f"Base branch fails its checks: {', '.join(red)}")
    return "\n".join(parts)


# --- knowledge ------------------------------------------------------------------------------


@tool("The project's notes: id, title, pinned, size. Read one with get_note.")
async def list_notes(project: ProjectRef, query: str | None = None) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
    notes = await api("GET", f"/api/projects/{p.id}/notes", params={"q": query} if query else None)
    return "\n".join(f"{n['id']} {n['title']}{' (pinned)' if n['pinned'] else ''} · {len(n['content'])} chars" for n in notes) or "No notes."


@tool("Read a note.")
async def get_note(project: ProjectRef, note_id: int) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
    for n in await api("GET", f"/api/projects/{p.id}/notes"):
        if n["id"] == note_id:
            return f"# {n['title']}\n\n{n['content']}"
    raise ToolError("No such note.")


@tool("Create a project note (Markdown) the team can read.", read=False)
async def create_note(project: ProjectRef, title: Annotated[str, Field(min_length=1, max_length=300)], content: str = "", pinned: Annotated[bool, Field(description="In every agent's prompt; sparingly")] = False) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
    note = await api("POST", f"/api/projects/{p.id}/notes", json={"title": title, "content": content, "pinned": pinned})
    return f"Note {note['id']} created."


@tool("Change a project note.", read=False)
async def update_note(project: ProjectRef, note_id: int, title: str | None = None, content: str | None = None, pinned: bool | None = None) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
    body = {k: v for k, v in {"title": title, "content": content, "pinned": pinned}.items() if v is not None}
    await api("PATCH", f"/api/projects/{p.id}/notes/{note_id}", json=body)
    return f"Note {note_id} updated."


@tool("The project's schedules: what runs when.")
async def list_schedules(project: ProjectRef) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
        names = await board_service.agent_names(session, p.id)
    rows = await api("GET", f"/api/projects/{p.id}/schedules")
    return "\n".join(
        f"{s['id']} {s['name']} → {names.get(s['agent_id'], s['agent_id'])} | {s['cron'] or s['run_at']} ({s['timezone']})"
        + ("" if s["enabled"] else " (off)") + (f" | next {s['next_run_at']}" if s.get("next_run_at") else "") + f": {truncate(s['prompt'], 120)}"
        for s in rows
    ) or "No schedules."


@tool("Search the project's history by meaning and keywords: tickets, reports, decisions, comments, notes, commits.")
async def search_history(
    project: ProjectRef,
    query: Annotated[str, Field(min_length=2, max_length=500)],
    kinds: list[Literal["ticket", "report", "decision", "comment", "note", "commit"]] | None = None,
    limit: Annotated[int, Field(ge=1, le=20)] = 8,
) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
        if not await search_service.enabled(session, p):
            raise ToolError("Semantic search is not available for this project.")
        hits = await search_service.find(session, p, query, kinds=list(kinds) if kinds else None, limit=limit)
        now = await search_service.states(session, p, hits, caller().app.state.runs.runners.of(p.id))
        return search_service.format_hits(hits, query, now)


# --- people and files -----------------------------------------------------------------------


@tool("Where people named you (@username): chats, comments, decisions.")
async def mentions(unread_only: bool = True) -> str:
    out = await api("GET", "/api/mentions")
    items = [m for m in out["items"] if not (unread_only and m.get("read_at"))]
    return "\n".join(
        f"{m['created_at'][:16]} {m['project_name']} {m['kind']}" + (f" #{m['task_number']}" if m.get("task_number") else "") + f" {m['author']}: {m['excerpt']}" for m in items
    ) or "No mentions."


@tool("An attachment: text files come back as text, others as a download URL (send your token as Bearer).")
async def get_attachment(attachment_id: int) -> str:
    async with sessionmaker()() as session:
        await check(session, "GET", "/api/attachments/{attachment_id}", attachment_id=attachment_id)
        attachment = await board_service.get_attachment(session, attachment_id)
        _project_of_call.set(attachment.project_id)
    head = f"{attachment.filename} ({attachment.content_type or 'unknown type'}, {attachment.size} bytes)"
    if attachment.purged_at is not None:
        return f"{head}: {board_service.purged_text(attachment)}"
    path = board_service.storage_path(attachment)
    textual = (attachment.content_type or mimetypes.guess_type(attachment.filename)[0] or "").startswith(("text/", "application/json")) or attachment.filename.endswith((".md", ".txt", ".log", ".csv", ".json", ".yaml", ".yml"))
    if textual and attachment.size <= TEXT_INLINE and path.exists():
        return f"{head}:\n" + (await anyio.Path(path).read_bytes()).decode("utf-8", errors="replace")
    return f"{head}: {attachment_url(attachment)}"


@tool("Attach a file to a ticket: text, or base64 for binary files.", read=False)
async def upload_attachment(
    project: ProjectRef,
    number: Number,
    filename: Annotated[str, Field(min_length=1, max_length=200)],
    text: str | None = None,
    content_base64: str | None = None,
) -> str:
    if (text is None) == (content_base64 is None):
        raise ToolError("Give either text or content_base64.")
    try:
        data = text.encode() if text is not None else base64.b64decode(content_base64 or "", validate=True)
    except ValueError:
        raise ToolError("content_base64 is not valid base64.") from None
    if len(data) > UPLOAD_MAX:
        raise ToolError(f"At most {UPLOAD_MAX // (1024 * 1024)} MB.")
    async with sessionmaker()() as session:
        task = await ticket_of(session, await project_of(session, project), number)
    kind = mimetypes.guess_type(filename)[0] or ("text/plain" if text is not None else "application/octet-stream")
    out = await api("POST", f"/api/tasks/{task.id}/attachments", files=[("files", (filename, data, kind))])
    return "\n".join(f"Attached {a['id']} {a['filename']} to #{number}." for a in out)


@tool("Subscription limits the project's agents run on: use per window, when it resets.")
async def usage_limits(project: ProjectRef) -> str:
    async with sessionmaker()() as session:
        p = await project_of(session, project)
    usable = set(await api("GET", f"/api/projects/{p.id}/accounts"))
    limits = await api("GET", "/api/limits")
    lines = []
    for account in limits["accounts"]:
        if account["account_id"] not in usable:
            continue
        windows = ", ".join(
            f"{w['name']} {round(w['utilization'] * 100)}%" + (f" (resets {datetime.fromtimestamp(w['resets_at'], tz=None).strftime('%Y-%m-%d %H:%M')} UTC)" if w.get("resets_at") else "")
            for w in account["windows"]
        )
        lines.append(f"{account['label']} ({account['provider']}): {windows or ('no plan limits' if not account['available'] else 'not read yet')}")
    return "\n".join(lines) or "No subscription with limits."


# --- the server and its endpoint ------------------------------------------------------------


def _untitled(node: Any) -> Any:
    """A schema without the generated titles: they are sent to the client's model with every request."""
    if isinstance(node, dict):
        return {k: _untitled(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
    return [_untitled(v) for v in node] if isinstance(node, list) else node


def build() -> MCPServer:
    server = MCPServer("plainboard", title="PlainBoard", instructions=INSTRUCTIONS)
    for fn, description, read in _TOOLS:
        hints = ToolAnnotations(read_only_hint=True) if read else ToolAnnotations(read_only_hint=False, destructive_hint=False)
        server.add_tool(fn, name=fn.__name__, description=description, annotations=hints, structured_output=False)
        registered = server._tool_manager.get_tool(fn.__name__)
        registered.parameters = _untitled(registered.parameters)
    return server


def _base_url(conn: HTTPConnection) -> str:
    host = conn.headers.get("x-forwarded-host", "").split(",")[0].strip() or conn.headers.get("host", "localhost")
    return f"{'https' if is_https(conn) else 'http'}://{host}"


class Endpoint:
    """``/mcp``: the token, the rate limit, then the SDK's Streamable HTTP transport."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.server = build()
        # Hosts are not checked here: PlainBoard runs under any name; the Guard refuses other sites' pages.
        self.server.streamable_http_app(
            stateless_http=True, json_response=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
        )
        self.limit = token_service.RateLimit()

    def run(self):  # noqa: ANN201
        """The SDK's task group, for the app's lifespan."""
        return self.server.session_manager.run()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        conn = HTTPConnection(scope)
        scheme, _, secret = conn.headers.get("authorization", "").partition(" ")
        async with sessionmaker()() as session:
            if not await token_service.enabled(session):
                await JSONResponse({"detail": "The MCP server is switched off on this PlainBoard."}, status_code=404)(scope, receive, send)
                return
            found = await token_service.authenticate(session, secret.strip()) if scheme.lower() == "bearer" else None
        if found is None:
            challenge = {"WWW-Authenticate": 'Bearer realm="PlainBoard"'}
            await JSONResponse({"detail": "A personal token is needed (Your account → MCP / API access)."}, status_code=401, headers=challenge)(scope, receive, send)
            return
        user, token = found
        if not self.limit.allow(token.id):
            await JSONResponse({"detail": "Too many requests: slow down."}, status_code=429, headers={"Retry-After": "10"})(scope, receive, send)
            return
        principal = Principal(user=user, token_id=token.id, read_only=token.read_only)
        reset = _caller.set(Caller(self.app, principal, secret.strip(), _base_url(conn)))
        try:
            await self.server.session_manager.handle_request(scope, receive, send)
        finally:
            _caller.reset(reset)

