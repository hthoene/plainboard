"""Move a project to another PlainBoard: one file out, one file in.

The file is a zip: ``project.json`` (the rows of the project's tables, as they are) and ``files/``
(the attachments). It carries the project's settings, the team with its notes, task lists and
schedules, the repositories, the board with its whole history (comments, questions, assignments and
their reports, where each ticket stands), the notes, the MCP servers, the public API keys, and the
chats if wanted. Credentials (GitHub tokens, SSH keys, secrets, the environment of MCP servers) travel
only when a passphrase is given: they are sealed with it. What belongs to the installation or to the
moment stays behind: users and roles, subscriptions, work that is running, the workspaces (the code
comes from git again) and the statistics of turns.

The tables are described, not coded one by one (``TABLES``): which rows belong to the project, which
columns point at other rows (their ids are new on the other side) and which hold credentials. A column
this side does not know is dropped on import, and one the file lacks takes its default, so versions
close to each other understand each other.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import secrets
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import DateTime, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..models import AssignmentStatus, Base, Project, Schedule, utcnow
from ..security import decrypt, encrypt
from . import projects as project_service
from . import schedules as schedule_service
from . import version as version_service
from .errors import NotFound, ServiceError

FORMAT = 1
MOVED = "Was open when the project was moved to this PlainBoard. Order it again if it is still needed."


@dataclass(frozen=True)
class Table:
    name: str
    # Which rows: those whose column points at an exported row of that table. None: the project's own (project_id).
    parent: tuple[str, str] | None = None
    refs: dict[str, str] = field(default_factory=dict)  # column -> the table its id is of
    lists: dict[str, str] = field(default_factory=dict)  # column with a list of ids -> that table
    late: dict[str, str] = field(default_factory=dict)  # as refs, but of rows that are written later
    sealed: tuple[str, ...] = ()  # credentials, encrypted with the installation's own key
    reset: dict[str, Any] = field(default_factory=dict)  # what starts anew on the other side
    key: str | None = "id"  # None: a link table
    chats: bool = False  # only with the chats


AGENT = {"created_by_agent_id": "agents"}
# Not moved: search_documents and search_cursors (services/search), made again from these on the other side,
# and idea_changes (what can be undone right now: its ids are this side's).
TABLES: tuple[Table, ...] = (
    Table("projects", sealed=("github_token_enc",), reset={"workspace_path": None, "account_id": None, "account_ids": [], "blocked_account_ids": None, "runner_id": None, "owner_id": None}),
    Table("agents", reset={"account_id": None, "team_pause": False}),
    Table("repositories", sealed=("token_enc", "ssh_key_enc"), reset={"tidied_at": None, "tidy_listed": None, "failing_told_at": None, "slow_told_at": None, "slow_told_seconds": None, "checks_baseline_at": None}),
    Table("mcp_servers", sealed=("env_enc", "headers_enc")),
    Table("secrets", sealed=("value_enc",)),
    Table("skills", lists={"agent_ids": "agents"}),
    Table("public_api_keys"),
    Table("agent_mcp_servers", ("agent_id", "agents"), {"agent_id": "agents", "mcp_server_id": "mcp_servers"}, key=None),
    Table("agent_repositories", ("agent_id", "agents"), {"agent_id": "agents", "repository_id": "repositories"}, key=None),
    Table("agent_notes", refs={"agent_id": "agents", **AGENT}),
    Table("notes", refs=AGENT),
    Table("todos", refs={"agent_id": "agents", **AGENT}),
    Table("schedules", refs={"agent_id": "agents", **AGENT}, reset={"last_run_id": None}),
    Table("tasks", refs=AGENT),
    Table("task_transitions"),
    Table("attachments", refs={"task_id": "tasks", "uploaded_by_agent_id": "agents"}),
    Table("comments", ("task_id", "tasks"), {"task_id": "tasks", "author_agent_id": "agents"}),
    Table("decisions", refs={"task_id": "tasks", "requested_by_agent_id": "agents"}, lists={"attachment_ids": "attachments"}),
    Table("assignments", refs={"task_id": "tasks", "agent_id": "agents", **AGENT}, reset={"run_id": None, "landing_id": None, "train_id": None}),
    Table("idea_topics"),
    Table("ideas", refs={"topic_id": "idea_topics", "task_id": "tasks"}, late={"merged_into_id": "ideas"}),
    Table("idea_links", refs={"idea_id": "ideas", "other_id": "ideas"}),
    Table("idea_versions", refs={"idea_id": "ideas"}),
    Table("ticket_states", ("task_id", "tasks"), {"task_id": "tasks", "until_assignment_id": "assignments"}, key="task_id"),
    # A chat goes on from its summary and its latest messages: the provider's own session stays behind.
    Table(
        "conversations",
        ("agent_id", "agents"),
        {"agent_id": "agents"},
        late={"summary_until_message_id": "messages"},
        reset={"provider_session_id": None, "session_sections": None, "session_topic": None, "account_key": None, "todos_shown": None, "context_tokens": 0, "session_base_tokens": 0, "session_cost_usd": 0},
    ),
    Table("messages", ("conversation_id", "conversations"), {"conversation_id": "conversations"}, reset={"run_id": None}, chats=True),
)
# What the import reports having brought along (the UI has the words for them).
COUNTED = ("agents", "tasks", "comments", "assignments", "notes", "schedules", "repositories", "attachments", "messages")


def _table(name: str):
    return Base.metadata.tables[name]


def _fernet(passphrase: str, salt: bytes) -> Fernet:
    key = hashlib.scrypt(passphrase.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=2**26, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def _plain(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _chunks(ids: list[int], size: int = 500):
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


async def _rows(session: AsyncSession, spec: Table, project_id: int, ids: dict[str, list[int]]) -> list[dict[str, Any]]:
    table = _table(spec.name)
    order = [c for c in table.primary_key.columns] or list(table.c)[:2]
    if spec.name == "projects":
        conditions = [table.c.id == project_id]
    elif spec.parent is None:
        conditions = [table.c.project_id == project_id]
    else:
        column, parent = spec.parent
        conditions = [table.c[column].in_(chunk) for chunk in _chunks(ids[parent])]
    rows: list[dict[str, Any]] = []
    for condition in conditions:
        rows += [dict(row) for row in (await session.execute(select(table).where(condition).order_by(*order))).mappings()]
    return rows


async def export_project(session: AsyncSession, project_id: int, out: IO[bytes], *, passphrase: str = "", chats: bool = True) -> str:
    """Write the project's file to ``out``. Returns a name for it."""
    project = await session.get(Project, project_id)
    if project is None:
        raise NotFound("Project not found")
    ids: dict[str, list[int]] = {}
    tables: dict[str, list[dict[str, Any]]] = {}
    sealed: dict[str, dict[str, dict[str, str]]] = {}
    left_out: list[str] = []
    for spec in TABLES:
        if spec.chats and not chats:
            continue
        rows = await _rows(session, spec, project_id, ids)
        ids[spec.name] = [row[spec.key] for row in rows] if spec.key else []
        for row in rows:
            for column in spec.sealed:
                secret = decrypt(row.get(column))
                row[column] = None
                if secret:
                    sealed.setdefault(spec.name, {}).setdefault(str(row[spec.key]), {})[column] = secret
                    if not passphrase:
                        left_out.append(_credential(spec.name, row))
        tables[spec.name] = [{key: _plain(value) for key, value in row.items()} for row in rows]
    document: dict[str, Any] = {
        "format": FORMAT,
        "plainboard": version_service.current(),
        "exported_at": utcnow().isoformat(),
        "project": project.name,
        "chats": chats,
        "tables": tables,
        "sealed": None,
        "left_out": sorted(set(left_out)),
    }
    if passphrase and sealed:
        salt = secrets.token_bytes(16)
        document["sealed"] = {"salt": base64.b64encode(salt).decode(), "data": _fernet(passphrase, salt).encrypt(json.dumps(sealed).encode()).decode()}
    uploads = get_config().uploads_dir
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("project.json", json.dumps(document, ensure_ascii=False))
        for row in tables.get("attachments", []):
            path = uploads / row["storage_name"]
            if path.is_file():
                archive.write(path, f"files/{row['storage_name']}")
    return f"{project.slug}.plainboard.zip"


def _credential(table: str, row: dict[str, Any]) -> str:
    """A credential by the name the user knows it by."""
    if table == "projects":
        return "the project's GitHub token"
    what = {"repositories": "the access of repository", "mcp_servers": "the environment of MCP server", "secrets": "the secret"}[table]
    return f"{what} {row.get('name', '')}".strip()


# What a project file may unpack to: a small file must not fill the memory or the disk (a zip bomb).
MAX_ENTRIES = 100_000
MAX_DOCUMENT_MB = 512  # project.json, which is read whole
MAX_TOTAL_MB = 16 * 1024


def _read_entry(archive: zipfile.ZipFile, name: str, max_bytes: int, out: IO[bytes] | None = None) -> bytes:
    """An entry, read piece by piece and stopped at ``max_bytes`` whatever its header claims.
    Written to ``out`` if given (then nothing is returned), else returned."""
    buffer = io.BytesIO() if out is None else out
    size = 0
    with archive.open(name) as entry:
        while chunk := entry.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise ServiceError(f"{name} in this file is larger than PlainBoard takes ({max_bytes // (1024 * 1024)} MB).", 413)
            buffer.write(chunk)
    return buffer.getvalue() if out is None else b""


def _check_size(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ServiceError(f"This file holds more than {MAX_ENTRIES} entries.", 413)
    if sum(info.file_size for info in infos) > MAX_TOTAL_MB * 1024 * 1024:
        raise ServiceError(f"This file unpacks to more than {MAX_TOTAL_MB // 1024} GB.", 413)


def _open(source: IO[bytes]) -> tuple[zipfile.ZipFile, dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(source)
        _check_size(archive)
        document = json.loads(_read_entry(archive, "project.json", MAX_DOCUMENT_MB * 1024 * 1024))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ServiceError("This is not a project file of PlainBoard.") from exc
    if not isinstance(document, dict) or not isinstance(document.get("tables"), dict):
        raise ServiceError("This is not a project file of PlainBoard.")
    if document.get("format") != FORMAT:
        raise ServiceError("This file was made by a newer PlainBoard. Update this one first.")
    return archive, document


def _unseal(document: dict[str, Any], passphrase: str) -> dict[str, dict[str, dict[str, str]]]:
    box = document.get("sealed")
    if not box or not passphrase:
        return {}
    try:
        return json.loads(_fernet(passphrase, base64.b64decode(box["salt"])).decrypt(box["data"].encode()))
    except (InvalidToken, KeyError, ValueError) as exc:
        raise ServiceError("The passphrase does not open this file.") from exc


@dataclass
class Imported:
    project: Project
    counts: dict[str, int]
    missing: list[str]  # credentials that did not come along and have to be entered again


async def import_project(session: AsyncSession, source: IO[bytes], *, passphrase: str = "") -> Imported:
    """Create a project from a file made by ``export_project``. Nothing of it starts by itself: what
    was open is closed, and the project waits for its user."""
    archive, document = _open(source)
    unsealed = _unseal(document, passphrase)
    new: dict[str, dict[int, int]] = {}  # table -> old id -> new id
    counts: dict[str, int] = {}
    missing: list[str] = [] if unsealed or not document.get("sealed") else ["everything with a credential: no passphrase was given for this file"]
    missing += [str(item) for item in document.get("left_out") or []]
    later: list[tuple[Table, int, dict[str, Any]]] = []
    uploads = get_config().uploads_dir
    now = utcnow()
    try:
        for spec in TABLES:
            table = _table(spec.name)
            ids = new.setdefault(spec.name, {})
            refs = {**spec.refs, **({"project_id": "projects"} if "project_id" in table.c else {})}
            for row in document["tables"].get(spec.name) or []:
                old = row.get(spec.key) if spec.key else None
                values = {key: value for key, value in row.items() if key in table.c and key != "id"}
                if not _point(values, refs, new, table) or (spec.key and old is None):
                    continue
                for column, target in spec.lists.items():
                    values[column] = [new[target][i] for i in values.get(column) or [] if i in new.get(target, {})]
                for column in spec.late:
                    values[column] = 0 if not table.c[column].nullable else None
                values.update({column: value for column, value in spec.reset.items() if column in table.c})
                for column, secret in unsealed.get(spec.name, {}).get(str(old), {}).items():
                    if column in spec.sealed:
                        values[column] = encrypt(secret)
                for column in table.c:
                    if isinstance(column.type, DateTime) and isinstance(values.get(column.name), str):
                        values[column.name] = datetime.fromisoformat(values[column.name])
                if spec.name == "projects":
                    values["slug"] = await project_service.unique_slug(session, str(values.get("name") or "project"))
                elif spec.name == "assignments" and values.get("status") in (AssignmentStatus.QUEUED, AssignmentStatus.RUNNING):
                    values.update(status=AssignmentStatus.CANCELLED, finished_at=now, result=values.get("result") or MOVED)
                elif spec.name == "attachments":
                    stored = _store(archive, uploads, str(values.get("storage_name") or ""))
                    if stored is None:
                        continue  # its file is not in the archive
                    values["storage_name"] = stored
                elif spec.name == "public_api_keys" and await session.scalar(select(table.c.id).where(table.c.key_hash == values.get("key_hash"))):
                    continue  # this installation has the key already (the project was copied, not moved)
                result = await session.execute(insert(table).values(**values))
                if spec.key == "id":
                    ids[old] = result.inserted_primary_key[0]
                elif spec.key:
                    ids[old] = values[spec.key]
                if spec.late and spec.key:
                    later.append((spec, ids[old], row))
                counts[spec.name] = counts.get(spec.name, 0) + 1
        if not new["projects"]:
            raise ServiceError("This file holds no project.")
        for spec, row_id, row in later:
            table = _table(spec.name)
            found = {column: new.get(target, {}).get(row.get(column)) for column, target in spec.late.items()}
            if values := {column: value for column, value in found.items() if value is not None}:
                await session.execute(update(table).where(table.c[spec.key] == row_id).values(**values))
        # A schedule fires at its next time from now, not for every time it missed on the way.
        for schedule in list(await session.scalars(select(Schedule).where(Schedule.id.in_(list(new["schedules"].values()))))):
            schedule.next_run_at = schedule_service.compute_next_run(schedule, after=now)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    project = await session.get(Project, next(iter(new["projects"].values())))
    assert project is not None
    return Imported(project, {name: counts[name] for name in COUNTED if counts.get(name)}, missing)


def _point(values: dict[str, Any], refs: dict[str, str], new: dict[str, dict[int, int]], table) -> bool:
    """Replace the ids a row points at by the new ones. False: it needs a row that did not come along."""
    for column, target in refs.items():
        if not values.get(column):  # nothing, or 0 where a column cannot be empty
            continue
        found = new.get(target, {}).get(values[column])
        if found is None and not table.c[column].nullable:
            return False
        values[column] = found
    return True


def _store(archive: zipfile.ZipFile, uploads: Path, name: str) -> str | None:
    """Put an attachment's file where this installation keeps them, under a name of its own."""
    try:
        archive.getinfo(f"files/{name}")
    except KeyError:
        return None
    suffix = Path(name).suffix
    stored = uuid.uuid4().hex + (suffix if len(suffix) <= 12 and suffix[1:].isalnum() else "")
    uploads.mkdir(parents=True, exist_ok=True)
    try:
        with (uploads / stored).open("wb") as out:
            _read_entry(archive, f"files/{name}", get_config().max_upload_mb * 1024 * 1024, out)
    except BaseException:
        (uploads / stored).unlink(missing_ok=True)
        raise
    return stored


def to_bytes(document: dict[str, Any]) -> bytes:
    """A project file from a document, without attachments (for tests and tools)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("project.json", json.dumps(document))
    return buffer.getvalue()
