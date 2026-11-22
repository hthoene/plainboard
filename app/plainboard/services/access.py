"""Who may do what: one table for the whole API.

Every route has a rule here; the API checks it before the endpoint runs (``api/deps.authorize``).
A route without a rule is for admins only, and a test fails until it has one, so a new endpoint
is never open by accident.

Three kinds of needs:
- ``PUBLIC``: no login. The route checks by itself (sign-in, the runner's token, a public API key).
- ``USER``, ``CREATOR``, ``ADMIN``: about the installation.
- ``VIEWER``, ``MEMBER``, ``OWNER``: a role in the project the route is about. The project is named
  by the path: directly, or by a row (an agent, a ticket, ...) that belongs to it.

A read key (``services/access_keys.py``) reads everything an admin reads and changes nothing.

What this protects is the API. Agents of all projects share one runner, so a member who can chat
with agents can have them read whatever the runner holds: give a role in a project to people you
trust with the installation's code and subscriptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Agent, AgentNote, Assignment, Attachment, Decision, ProjectRole, PublicApiKey, Repository, Run, Secret, Task, Todo, User
from ..security import read_session_cookie
from . import access_keys as access_key_service
from . import permissions
from . import sessions as session_service
from . import users as user_service
from .errors import NotFound, ServiceError


class Need(StrEnum):
    PUBLIC = "public"
    USER = "user"  # anyone signed in
    CREATOR = "creator"  # may create projects
    ADMIN = "admin"
    VIEWER = "viewer"
    MEMBER = "member"
    OWNER = "owner"


PROJECT_RANK = {Need.VIEWER: 1, Need.MEMBER: 2, Need.OWNER: 3}
READS = ("GET", "HEAD", "OPTIONS")
# What a read key never gets, although it is a GET: the keys themselves, the provider accounts and
# their sign-in, and what is fetched with the installation's GitHub token.
READ_KEY_HIDDEN = ("/api/access-keys", "/api/accounts", "/api/github", "/api/projects/{project_id}/github")


@dataclass(frozen=True)
class Principal:
    """Who a request comes from: a user, or a read key."""

    user: User | None = None
    read_key: bool = False
    session_id: str = ""  # the browser session of a user (services/sessions.py)

    @property
    def is_admin(self) -> bool:
        return bool(self.user and self.user.is_admin)

    @property
    def name(self) -> str:
        return self.user.name if self.user else ""


@dataclass(frozen=True)
class Rule:
    path: str  # a route's path, or the beginning of it up to a "/"
    read: Need
    write: Need | None = None  # None: as for reading
    param: str = ""  # a project role: the path parameter that names the project ...
    table: type | None = None  # ... or the row of this table that belongs to it


def _of(path: str, param: str, table: type, read: Need = Need.VIEWER, write: Need = Need.MEMBER) -> Rule:
    return Rule(path, read, write, param, table)


def _project(path: str, read: Need = Need.VIEWER, write: Need = Need.OWNER) -> Rule:
    return Rule(f"/api/projects/{{project_id}}{path}", read, write, "project_id")


RULES: tuple[Rule, ...] = (
    # --- no login: these check by themselves
    Rule("/api/auth", Need.PUBLIC),
    Rule("/api/health", Need.PUBLIC),
    Rule("/api/internal", Need.PUBLIC),
    Rule("/api/public", Need.PUBLIC),
    Rule("/api/runners/connect", Need.PUBLIC),  # an external runner's socket: its own token
    # --- the installation
    Rule("/api/auth/me", Need.USER),
    Rule("/api/meta", Need.USER),
    Rule("/api/limits", Need.USER),
    Rule("/api/pause", Need.USER, Need.ADMIN),  # everyone sees that everything is paused; an admin does it
    Rule("/api/settings", Need.USER, Need.ADMIN),  # reading shows no secret, and the UI needs the defaults
    Rule("/api/settings/check/github", Need.CREATOR),  # a token typed for a new project; the installation's: admins
    # Everyone adds their own subscriptions; who may change one is its owner or an admin (accounts.managed),
    # the list holds what the user may use (accounts.visible), secrets are never read back.
    Rule("/api/accounts", Need.USER),
    Rule("/api/accounts/{account_id}/signin", Need.ADMIN),  # a sign-in on the runner: the installation's only
    Rule("/api/secrets", Need.ADMIN),
    Rule("/api/access-keys", Need.ADMIN),
    Rule("/api/inspect", Need.ADMIN),
    Rule("/api/runners", Need.ADMIN),  # external runners: their tokens and machines
    Rule("/api/runners/choices", Need.USER),  # names only: where the user's new project may start
    Rule("/api/usage", Need.ADMIN),  # disk and memory of the whole installation
    Rule("/api/github", Need.ADMIN),  # lists what the installation's GitHub token sees
    Rule("/api/users", Need.ADMIN),
    Rule("/api/users/directory", Need.USER),  # names only: whom a project can be shared with
    Rule("/api/avatars", Need.USER),  # the profile pictures of those names
    Rule("/api/mentions", Need.USER, Need.USER),  # each person's own
    Rule("/api/projects", Need.USER, Need.CREATOR),  # the list holds the user's own projects only
    # --- a project. Everything in it can be read by its viewers; what is not listed is changed by owners only.
    _project(""),
    _project("/agents", write=Need.MEMBER),
    _project("/tasks", write=Need.MEMBER),
    _project("/notes", write=Need.MEMBER),
    _project("/schedules", write=Need.MEMBER),
    _project("/browser", read=Need.MEMBER),  # the browser window is driven, not only watched
    _project("/previews", write=Need.MEMBER),  # which servers run; a member stops one
    _project("/export", read=Need.OWNER),  # the whole project as a file, credentials included
    _project("/secrets", read=Need.OWNER),
    _project("/skills"),  # its Claude skills: everyone sees which, its owner changes them
    _project("/accounts", write=Need.ADMIN),  # which of the installation's subscriptions it may use: not its owner's call
    _project("/runner"),
    _project("/owner", read=Need.OWNER),  # who owns it: an admin, or its owner (services/ownership.py)  # which runner its agents use: its owner chooses
    _project("/public-api-keys", read=Need.OWNER),
    _project("/github", read=Need.OWNER),  # what its own token sees on GitHub (an admin: else the installation's)
    _of("/api/agents/{agent_id}", "agent_id", Agent),
    _of("/api/agent-notes/{note_id}", "note_id", AgentNote),
    _of("/api/tasks/{task_id}", "task_id", Task),
    _of("/api/todos/{todo_id}", "todo_id", Todo),
    _of("/api/runs/{run_id}", "run_id", Run),
    _of("/api/assignments/{assignment_id}", "assignment_id", Assignment),
    _of("/api/attachments/{attachment_id}", "attachment_id", Attachment),
    _of("/api/decisions/{decision_id}", "decision_id", Decision),
    _of("/api/repositories/{repo_id}", "repo_id", Repository, write=Need.OWNER),
    _of("/api/public-api-keys/{key_id}", "key_id", PublicApiKey, read=Need.OWNER, write=Need.OWNER),
    _of("/api/secrets/{secret_id}", "secret_id", Secret, read=Need.OWNER, write=Need.OWNER),  # a secret of no project: admins
)


def rule_for(path: str) -> Rule | None:
    """The rule of a route: the one with the longest path that the route's path begins with."""
    matching = [r for r in RULES if path == r.path or path.startswith(r.path + "/")]
    return max(matching, key=lambda r: len(r.path), default=None)


async def principal(session: AsyncSession, cookie: str | None, authorization: str | None = None) -> Principal | None:
    """Who sent this: the user of a session cookie, else a read key in the Authorization header."""
    if signed := read_session_cookie(cookie):
        user = await session_service.user_of(session, signed[0], signed[2], signed[1])
        if user is not None:
            return Principal(user=user, session_id=signed[2])
    scheme, _, secret = (authorization or "").partition(" ")
    if scheme.lower() == "bearer" and secret.strip() and await access_key_service.valid(session, secret.strip()):
        return Principal(read_key=True)
    return None


async def _project_of(session: AsyncSession, rule: Rule, params: dict) -> int | None:
    try:
        value = int(params[rule.param])
    except (KeyError, TypeError, ValueError):
        raise NotFound("Not found") from None
    if rule.table is None:
        return value
    row = (await session.execute(select(rule.table.id, rule.table.project_id).where(rule.table.id == value))).first()
    if row is None:
        raise NotFound("Not found")
    return row[1]


async def check(session: AsyncSession, who: Principal | None, method: str, path: str, params: dict) -> None:
    """Raises unless ``who`` may send ``method`` to the route with this path (its template)."""
    rule = rule_for(path)
    reading = method.upper() in READS
    need = Need.ADMIN if rule is None else rule.read if reading else rule.write or rule.read
    if need == Need.PUBLIC:
        return
    if who is None:
        raise ServiceError("Not authenticated", 401)
    if who.read_key:
        if not reading or path.startswith(READ_KEY_HIDDEN):
            raise ServiceError("This key can only read", 403)
        return
    user = who.user
    assert user is not None
    if user.is_admin or need == Need.USER:
        return
    if need == Need.CREATOR:  # also whether one more is allowed, before anything of it is created
        await permissions.check_create(session, user)
        return
    if need == Need.ADMIN or rule is None:
        raise ServiceError("Only an admin can do this.", 403)
    await require_role(session, user, await _project_of(session, rule, params), need)


async def require_role(session: AsyncSession, user: User, project_id: int | None, need: Need) -> ProjectRole:
    """The user's role in the project, if it is enough. A project the user has no part in does not exist for them."""
    if project_id is None:  # belongs to no project: the installation's
        if not user.is_admin:
            raise ServiceError("Only an admin can do this.", 403)
        return ProjectRole.OWNER
    role = await user_service.role_in(session, user, project_id)
    if role is None:
        raise NotFound("Project not found")
    if PROJECT_RANK[Need(role)] < PROJECT_RANK[need]:
        raise ServiceError(f"Your role in this project ({role}) does not allow this.", 403)
    return role
