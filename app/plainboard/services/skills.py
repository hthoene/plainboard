"""Claude skills of a project: a folder with a SKILL.md (and what it needs), for all or selected agents.

A skill comes as a zip of its folder, a single SKILL.md, or from a git repository (the runner clones it,
with the project's own access). Its files are checked (no paths out of the folder, no links, size caps), packed
anew into a zip of their own and kept in the row (``Skill.archive``, base64), so moving a project carries them.
Before a turn the runner lays the agent's skills into ``<workspace>/.claude/skills/<name>/``, fetching a skill's
zip only when its hash differs from what it laid down there (``SkillSpec``, runner ``skills.py``).
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from plainboard_protocol import Credentials, SkillFetch, SkillSpec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Agent, Project, Skill
from ..schemas import SkillImport, SkillUpdate
from . import permissions
from .errors import NotFound, ServiceError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # the zip as it comes
MAX_FILES_BYTES = 20 * 1024 * 1024  # all its files, unpacked
MAX_FILES = 500
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")  # Claude's rule for a skill's name: lowercase, digits, hyphens
MAX_NAME = 64
MAX_DESCRIPTION = 1024
IGNORED = ("__MACOSX/", ".git/")
IGNORED_NAMES = (".DS_Store", "Thumbs.db")
ENTRY = "SKILL.md"


@dataclass
class Bundle:
    files: dict[str, bytes]  # path in the skill's folder -> content
    name: str
    description: str

    @property
    def size(self) -> int:
        return sum(len(v) for v in self.files.values())

    @property
    def hash(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.files):
            digest.update(path.encode() + b"\0" + hashlib.sha256(self.files[path]).digest())
        return digest.hexdigest()

    def pack(self) -> bytes:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self.files):
                info = zipfile.ZipInfo(path, date_time=(2000, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, self.files[path])
        return out.getvalue()


def _clean_path(raw: str) -> str | None:
    """The entry's path inside the folder; None for what is left out. Raises for a path that leaves the folder."""
    if raw.endswith("/") or any(raw.startswith(p) or f"/{p}" in raw for p in IGNORED):
        return None
    if "\\" in raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ServiceError(f"Not a valid path in a skill: {raw[:200]}")
    parts = PurePosixPath(raw).parts
    if any(p in ("..", ".") for p in parts) or not parts:
        raise ServiceError(f"Not a valid path in a skill: {raw[:200]}")
    if parts[-1] in IGNORED_NAMES:
        return None
    return "/".join(parts)


def _unzip(data: bytes) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ServiceError("Not a zip file") from exc
    files: dict[str, bytes] = {}
    total = 0
    with archive:
        for info in archive.infolist():
            path = _clean_path(info.filename)
            if path is None:
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ServiceError(f"Links are not allowed in a skill: {path[:200]}")
            if len(files) >= MAX_FILES:
                raise ServiceError(f"A skill may have at most {MAX_FILES} files")
            content = b""
            with archive.open(info) as fh:  # read piece by piece: a header may lie about the size
                while chunk := fh.read(65536):
                    content += chunk
                    if total + len(content) > MAX_FILES_BYTES:
                        raise ServiceError(f"A skill may have at most {MAX_FILES_BYTES // (1024 * 1024)} MB of files", 413)
            total += len(content)
            files[path] = content
    return files


def _root(files: dict[str, bytes]) -> dict[str, bytes]:
    """The skill's folder: the top level if SKILL.md is there, else the one folder everything is in."""
    if ENTRY in files:
        return files
    tops = {p.split("/", 1)[0] for p in files}
    if len(tops) == 1 and (top := tops.pop()) and f"{top}/{ENTRY}" in files:
        return {p.split("/", 1)[1]: v for p, v in files.items()}
    raise ServiceError(f"No {ENTRY} at the top of the skill's folder")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def frontmatter(text: str) -> dict[str, str]:
    """The simple YAML at the top of a SKILL.md: ``key: value``, quoted values, and ``>``/``|`` blocks."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ServiceError(f"{ENTRY} must begin with a frontmatter block (---)")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise ServiceError(f"The frontmatter of {ENTRY} is not closed (---)") from None
    values: dict[str, str] = {}
    key = ""
    block: list[str] | None = None
    for line in lines[1:end]:
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if match:
            key, value = match.group(1), match.group(2).strip()
            block = [] if value in (">", "|", ">-", "|-") else None
            values[key] = "" if block is not None else _unquote(value)
        elif key and line.strip() and (line[0] in " \t"):  # a block or a value that goes on
            values[key] = f"{values[key]} {line.strip()}".strip()
    return values


def read(files: dict[str, bytes]) -> Bundle:
    files = _root(files)
    try:
        meta = frontmatter(files[ENTRY].decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ServiceError(f"{ENTRY} is not UTF-8 text") from exc
    name, description = meta.get("name", "").strip(), meta.get("description", "").strip()
    if not name:
        raise ServiceError(f"{ENTRY} needs a name in its frontmatter")
    if len(name) > MAX_NAME or not NAME.match(name):
        raise ServiceError(f"The skill's name '{name[:80]}' may only have lowercase letters, digits and hyphens (at most {MAX_NAME})")
    if not description:
        raise ServiceError(f"{ENTRY} needs a description in its frontmatter")
    if len(description) > MAX_DESCRIPTION:
        raise ServiceError(f"The skill's description may have at most {MAX_DESCRIPTION} characters")
    return Bundle(files=files, name=name, description=description)


def parse_upload(filename: str, data: bytes) -> Bundle:
    """A zip of the skill's folder or a single SKILL.md."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ServiceError(f"A skill may be at most {MAX_UPLOAD_BYTES // (1024 * 1024)} MB", 413)
    if zipfile.is_zipfile(io.BytesIO(data)):
        return read(_unzip(data))
    if not filename.lower().endswith(".md"):
        raise ServiceError(f"Upload a zip of the skill's folder or its {ENTRY}")
    return read({ENTRY: data})


def archive_bytes(skill: Skill) -> bytes:
    return base64.b64decode(skill.archive)


async def list_for_project(session: AsyncSession, project_id: int) -> list[Skill]:
    return list(await session.scalars(select(Skill).where(Skill.project_id == project_id).order_by(Skill.name)))


async def get(session: AsyncSession, skill_id: int, project_id: int) -> Skill:
    skill = await session.get(Skill, skill_id)
    if skill is None or skill.project_id != project_id:
        raise NotFound("Skill not found")
    return skill


async def _agent_ids(session: AsyncSession, project_id: int, ids: list[int]) -> list[int]:
    if not ids:
        return []
    known = set(await session.scalars(select(Agent.id).where(Agent.project_id == project_id, Agent.id.in_(ids))))
    return [i for i in dict.fromkeys(ids) if i in known]


def _fill(skill: Skill, bundle: Bundle) -> None:
    skill.name, skill.description = bundle.name, bundle.description
    skill.archive = base64.b64encode(bundle.pack()).decode()
    skill.content_hash, skill.size, skill.file_count = bundle.hash, bundle.size, len(bundle.files)


async def _check_name(session: AsyncSession, project_id: int, name: str, skill_id: int | None = None) -> None:
    q = select(Skill.id).where(Skill.project_id == project_id, Skill.name == name)
    if skill_id:
        q = q.where(Skill.id != skill_id)
    if await session.scalar(q) is not None:
        raise ServiceError(f"The project already has a skill named '{name}'", 409)


async def store(
    session: AsyncSession,
    project_id: int,
    bundle: Bundle,
    *,
    skill: Skill | None = None,
    source: str = "upload",
    url: str = "",
    ref: str = "",
    path: str = "",
    all_agents: bool = True,
    agent_ids: list[int] | None = None,
) -> Skill:
    """A new skill, or new files for ``skill`` (its name may change, its agents stay)."""
    await _check_name(session, project_id, bundle.name, skill.id if skill else None)
    if skill is None:
        skill = Skill(project_id=project_id, all_agents=all_agents, agent_ids=await _agent_ids(session, project_id, agent_ids or []))
        session.add(skill)
    skill.source, skill.source_url, skill.source_ref, skill.source_path = source, url, ref, path
    _fill(skill, bundle)
    await session.commit()
    await session.refresh(skill)
    return skill


def _clean_source(url: str, path: str) -> tuple[str, str]:
    url = url.strip()
    if not url or url.startswith("-") or not re.match(r"^(https?://|ssh://|git@)", url):
        raise ServiceError("Give the git address of the repository (https://, ssh:// or git@)")
    path = path.strip().strip("/")
    if path and any(p in ("..", ".") for p in PurePosixPath(path).parts):
        raise ServiceError("Not a valid folder in the repository")
    return url, path


async def fetch(session: AsyncSession, runner, project: Project, url: str, ref: str, path: str) -> Bundle:
    """The skill's folder from git, through the project's runner and with the project's own access (#33).
    Nothing is written before the runner answers."""
    url, path = _clean_source(url, path)
    ref = ref.strip()
    if ref.startswith("-"):
        raise ServiceError("Not a valid branch or tag")
    creds = await permissions.credentials(session, project)
    access = Credentials(github_token=creds.github_token, git_ssh_key=creds.git_ssh_key)
    await session.commit()  # nothing waits on the database while the runner clones
    data = await runner.fetch_skill(SkillFetch(url=url, ref=ref, path=path, credentials=access))
    if len(data) > MAX_UPLOAD_BYTES:
        raise ServiceError(f"A skill may be at most {MAX_UPLOAD_BYTES // (1024 * 1024)} MB", 413)
    return read(_unzip(data))


async def import_git(session: AsyncSession, runner, project: Project, body: SkillImport) -> Skill:
    bundle = await fetch(session, runner, project, body.url, body.ref, body.path)
    url, path = _clean_source(body.url, body.path)
    return await store(session, project.id, bundle, source="git", url=url, ref=body.ref.strip(), path=path, all_agents=body.all_agents, agent_ids=body.agent_ids)


async def reimport(session: AsyncSession, runner, skill: Skill) -> Skill:
    """A skill from git, fetched again."""
    if skill.source != "git":
        raise ServiceError("This skill was uploaded: upload it again to update it")
    project = await session.get(Project, skill.project_id)
    assert project is not None
    bundle = await fetch(session, runner, project, skill.source_url, skill.source_ref, skill.source_path)
    return await store(session, skill.project_id, bundle, skill=skill, source="git", url=skill.source_url, ref=skill.source_ref, path=skill.source_path)


async def update(session: AsyncSession, skill: Skill, body: SkillUpdate) -> Skill:
    if body.enabled is not None:
        skill.enabled = body.enabled
    if body.all_agents is not None:
        skill.all_agents = body.all_agents
    if body.agent_ids is not None:
        skill.agent_ids = await _agent_ids(session, skill.project_id, body.agent_ids)
    await session.commit()
    await session.refresh(skill)
    return skill


async def delete(session: AsyncSession, skill: Skill) -> None:
    await session.delete(skill)
    await session.commit()


def for_agent(skills: list[Skill], agent_id: int) -> list[Skill]:
    return [s for s in skills if s.enabled and s.archive and (s.all_agents or agent_id in (s.agent_ids or []))]


async def specs_for_agent(session: AsyncSession, agent: Agent) -> list[SkillSpec]:
    rows = (await session.execute(
        select(Skill.id, Skill.name, Skill.content_hash, Skill.enabled, Skill.all_agents, Skill.agent_ids).where(Skill.project_id == agent.project_id)
    )).all()
    return [SkillSpec(id=r.id, name=r.name, hash=r.content_hash) for r in rows if r.enabled and r.content_hash and (r.all_agents or agent.id in (r.agent_ids or []))]


async def archive_for_agent(session: AsyncSession, project_id: int, agent_id: int, skill_id: int) -> bytes:
    """The zip of a skill the agent gets (the runner fetches it with the turn's token)."""
    skill = await get(session, skill_id, project_id)
    if not for_agent([skill], agent_id):
        raise NotFound("Skill not found")
    return archive_bytes(skill)
