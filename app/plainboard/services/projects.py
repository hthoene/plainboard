from __future__ import annotations

from plainboard_protocol import ProjectFiles
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..events import bus
from ..models import Attachment, InboxItem, Project, ProjectMember, ProjectRole, User, utcnow
from ..schemas import AgentCreate, ProjectCreate, ProjectOut, ProjectUpdate, TaskCreate
from ..security import decrypt, encrypt
from . import accounts as account_service
from . import agents as agent_service
from . import board as board_service
from . import customer_texts
from . import repositories as repo_service
from . import schedules as schedule_service
from . import permissions
from . import settings as settings_service
from .errors import NotFound, ServiceError
from .util import slugify

# What a project made in the UI starts with, in To do: a manager knows how to run a project, not what
# this one is, and a team is easy to get wrong (too many members, the strongest model for everyone).
# No model is named here: names age, and list_models shows what the installation offers today.
FIRST_STEPS = (
    TaskCreate(
        title="Find out with the user what this project is about",
        status="todo",
        priority="high",
        description="""\
The first step in a new project. Ask the user in the chat, a few questions at a time, in their language.
The questions are a suggestion; follow where the user leads:
- What is being built, for whom, and what should exist first.
- Where the code is: an existing repository (connect it with add_repository) or a new one.
- How it is checked and released: tests, CI, who may publish.
- What the user wants to decide themselves, and what the team may decide.

Set the language the user writes in as the project's language (update_project language), so every agent
writes to them in it from now on.

Write the result into the project description (update_project) and a pinned note (create_note), so every
agent starts from it. Then move this ticket to done.""",
    ),
    TaskCreate(
        title="Propose a team and how many agents work at once, agree with the user, set it up",
        status="todo",
        priority="high",
        description="""\
After it is clear what the project is about. Everything below is a recommendation to start from, not a
rule: propose it to the user in the chat, say why, and let them decide. They may decide differently.
Create nothing before their yes.

- The team: one member per area of the product that will see steady work, not one per task. Few to
  start with; another one when an area appears.
- Models: a strong one plans and checks, a cheaper one builds. Look up what this installation offers
  with list_models (every model with its class: xlarge > large > medium > small) and take the newest
  model of the class you need. Never pick a model by a name you remember: newer ones may exist.
  - You, the manager, plan and decide: a strong model, one of the two top classes.
  - The members who write most of the code do the bulk of the work, and that is where the quota
    goes: a mid-range model, a class or two below yours.
  - A reviewer on a strong model, as strong as yours. It only reads: it looks once more over
    important core logic that a cheaper model planned or wrote, and the cheaper model makes the
    fixes. Not over everything: a review is a handover and a wait, so only where a mistake is
    expensive.
  - The most important core work (the architecture, what everything else builds on, security,
    money, data that must not be lost) is built by a strong model in the first place: propose a
    member for it, or which member takes it.
  Only models of a provider the user has a subscription for.
- How many members work at the same time (set_parallel_agents; get_project shows what you may set).
  More finishes sooner and uses the quota faster. Say what you suggest for this project and why.
- How finished work reaches the base branch, per repository (update_repository land_first). After
  the checks pass: the branch stays green, right for a branch that is released from. At once, checked
  afterwards: nothing queues behind long check runs, right for a development branch. Say which fits
  each repository.

When the user has decided, set it up as they decided: create_agent for the members, the limit, the
repositories, and your own model with update_agent if it should change. Then move this ticket to done.""",
    ),
)
FIRST_STEPS_NOTE = (
    "This project is new. Two tickets are in To do to set it up: find out with the user what it is about, then propose a team "
    "and how many agents work at once. Start with the first one: read it (get_task) and ask the user."
)

MANAGER_DESCRIPTION = (
    "Coordinates the project: turns tickets into clear assignments for the team, "
    "manages agents, notes, schedules and integrations, and reports to the user."
)


async def unique_slug(session: AsyncSession, name: str) -> str:
    base = slugify(name, "project")
    slug, n = base, 2
    while await session.scalar(select(Project.id).where(Project.slug == slug)) is not None:
        slug, n = f"{base}-{n}", n + 1
    return slug


async def create(session: AsyncSession, data: ProjectCreate, owner: User | None = None) -> Project:
    """All or nothing: a step that fails takes the project with it. ``owner``: whoever creates it owns
    it from the start, and what that person may use holds for it (services/permissions.py)."""
    if data.account_id is not None:
        await account_service.get(session, data.account_id)
        await permissions.check_account(session, None, data.account_id, owner)
    runner_id = await permissions.start_runner(session, owner, data.runner_id, "runner_id" in data.model_fields_set)
    project = Project(
        owner_id=owner.id if owner else None,
        runner_id=runner_id,
        name=data.name,
        slug=await unique_slug(session, data.name),
        description=data.description,
        workspace_path=normalize_workspace_path(data.workspace_path),
        github_token_enc=encrypt(data.github_token),
        parallel_agents=data.parallel_agents,
        account_id=data.account_id,
    )
    session.add(project)
    await session.flush()
    if owner is not None:
        session.add(ProjectMember(project_id=project.id, user_id=owner.id, role=ProjectRole.OWNER))
    await session.commit()
    project_id = project.id
    try:
        for repo in data.repositories:
            await repo_service.create(session, project.id, repo)
        manager = await agent_service.create(
            session,
            project.id,
            AgentCreate(name="Manager", role="Project Manager", description=MANAGER_DESCRIPTION, toolset="board"),
            is_manager=True,
        )
        await schedule_service.create_tidy(session, manager)
        if data.first_steps:
            await board_service.create_tasks(session, project.id, list(FIRST_STEPS))
            session.add(InboxItem(agent_id=manager.id, text=FIRST_STEPS_NOTE))  # read with its first turn; no turn is spent on it
            await session.commit()
    except Exception:
        await session.rollback()
        await delete(session, await get(session, project_id))
        raise
    return project


async def update(session: AsyncSession, project: Project, data: ProjectUpdate) -> Project:
    fields = data.model_dump(exclude_unset=True)
    if "github_token" in fields:
        project.github_token_enc = encrypt(fields.pop("github_token"))
    if "workspace_path" in fields:
        fields["workspace_path"] = normalize_workspace_path(fields["workspace_path"])
    if fields.get("account_id") is not None:
        await account_service.get(session, fields["account_id"])
        await permissions.check_account(session, project, fields["account_id"])
    if "language" in fields:
        fields["language"] = (fields["language"] or "").strip()
    changed = []
    if fields.get("customer_languages") is not None:
        # Removing a language keeps its texts (customer_texts): they come back with the language.
        fields["customer_languages"] = list(dict.fromkeys(fields["customer_languages"]))
        if fields["customer_languages"][0] != customer_texts.default(project):
            changed = await customer_texts.switch_default(session, project, fields["customer_languages"][0])
    else:
        fields.pop("customer_languages", None)
    for key, value in fields.items():
        setattr(project, key, value)
    await session.commit()
    bus.publish(project.id, "project.updated", to_out(project))
    for task in changed:
        await board_service.publish_task(session, task)
    return project


async def parallel_limit(session: AsyncSession, project: Project) -> int:
    """How many members of this project work at the same time: its own number, else the one from
    the settings. Every project counts by itself; 0 pauses its members."""
    own = project.parallel_agents if project.parallel_agents is not None else int(await settings_service.get(session, "max_parallel_runs"))
    cap = await permissions.max_parallel(session, project)  # across all projects of its owner: none of them alone gets more
    return own if cap is None else min(own, cap)


async def set_parallel_by_manager(session: AsyncSession, project: Project, count: int) -> None:
    """A manager changes how many members of its project work at once, within the range the user allows."""
    limit = int(await settings_service.get(session, "manager_parallel_limit"))
    if limit < 1:
        raise ServiceError("The user has not allowed managers to change this. Ask the user (Settings).")
    if not 1 <= count <= limit:
        raise ServiceError(f"Must be between 1 and {limit}. The user can raise that limit in Settings.")
    project.parallel_agents = count
    await session.commit()
    bus.publish(project.id, "project.updated", to_out(project))


async def set_label_color(session: AsyncSession, project: Project, label: str, color: str | None) -> None:
    colors = {name: value for name, value in project.label_colors.items() if name != label}
    if color:
        colors[label] = color
    project.label_colors = colors
    await session.commit()
    bus.publish(project.id, "project.updated", to_out(project))


_deleting: set[int] = set()  # projects on their way out


async def delete(session: AsyncSession, project: Project, runner=None, *, files: bool = False) -> None:
    """The project with everything PlainBoard knows about it. The runner's own clones of its repositories go
    with it. The folders its agents worked in go only when asked (`files`): they may hold work that was never
    pushed. A folder of the user's own (`workspace_path`) always stays.

    Deleting folders can take minutes. Until it is over the project rests (nothing starts in folders
    that are on their way out), and a second request to delete it is refused."""
    if project.id in _deleting:
        raise ServiceError("This project is being deleted already. With its folders that can take a few minutes.", 409)
    _deleting.add(project.id)
    try:
        await _delete(session, project, runner, files)
    finally:
        _deleting.discard(project.id)


async def _delete(session: AsyncSession, project: Project, runner, files: bool) -> None:
    from . import landings as landing_service
    from .board import storage_path

    if runner is not None:
        folder = f"{get_config().workspaces_root}/{project.slug}" if files and not project.workspace_path else None
        trains = [k for r in await repo_service.list_for_project(session, project.id) for k in (landing_service.train_key(r), f"{landing_service.train_key(r)}-release")]  # the promotions' clone too (services/promotions.key)
        rested = project.paused_at is not None
        if not rested:
            project.paused_at = utcnow()
            await session.commit()

        async def stays() -> None:  # not deleted after all: as it was before
            if not rested:
                project.paused_at = None
                await session.commit()

        try:
            result = await runner.remove_files(ProjectFiles(workspace=folder, trains=trains))
        except BaseException:
            await stays()
            raise
        if folder and not result.ok:
            await stays()
            raise ServiceError(f"The project's folders could not be deleted, so the project is still there: {result.detail}. Try again, or delete it without them.")
    attachments = (await session.scalars(select(Attachment).where(Attachment.project_id == project.id))).all()
    files = [storage_path(a) for a in attachments]
    await session.delete(project)
    await session.commit()
    for path in files:
        path.unlink(missing_ok=True)


async def get(session: AsyncSession, project_id: int) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise NotFound("Project not found")
    return project


async def list_all(session: AsyncSession) -> list[Project]:
    return list((await session.scalars(select(Project).order_by(Project.id))).all())


def normalize_workspace_path(value: str | None) -> str | None:
    """Relative path below the workspaces root; '' or None = per-agent directories."""
    if not value:
        return None
    parts = [p for p in value.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ServiceError("Workspace path must stay inside the workspaces folder")
    return "/".join(parts) or None


def github_token(project: Project) -> str | None:
    return decrypt(project.github_token_enc)


def to_out(project: Project, role: ProjectRole | None = None) -> ProjectOut:
    out = ProjectOut.model_validate(project)
    out.has_github_token = bool(project.github_token_enc)
    out.role = role
    return out
