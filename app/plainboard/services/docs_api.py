"""A doc site's API (#131, api/docs_api.py): the whole documentation of one site for scripts and other systems, public
or private, with a key in the ``Authorization: Bearer`` header.

Keys are per site and read only that site (``DocApiKey``): made by its project's members in the site's settings or by
the manager's doc tool, shown once (only the hash is stored), with a name, optionally an expiry, revoked by deleting
them; "last used" is noted now and then. A person's own token (``pbp_``, services/personal_tokens.py) of someone with
a role in the project (or an admin) reads it too. Each key has its own request budget per minute. A key never stands
in an address (only in the header), so it is in no log.

What it reads: the site (languages, versions), its pages page by page (``tree``: the ids, parents and positions, so the
tree is rebuilt on the other side), one page as Markdown with its files and links, the footer pages, the files, and an
export of a version in a language as JSON lines or as a zip of Markdown files with the files (streamed, batch by
batch, with sessions of its own: thousands of pages never sit in memory). ``since``: only pages changed (or newly
translated) since then; deleted pages are not reported (compare the ids of ``tree``).
"""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import secrets
import zipfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db
from ..models import Attachment, DocApiKey, DocPage, DocRelease, DocSite, DocTranslation, User, utcnow
from . import board as board_service
from . import docs as docs_service
from . import docs_areas, docs_custom, docs_public, docs_releases
from . import personal_tokens as token_service
from . import users as user_service
from .errors import NotFound, ServiceError

PREFIX = "pbd_"  # PlainBoard, docs
USED_EVERY = timedelta(minutes=1)  # how often "last used" is written
MAX_PER_SITE = 50
RATE = 300  # requests per key a minute (an export is one)
EXPORTS_AT_ONCE = 2  # per key
BATCH = 200  # pages per query of an export
TREE_LIMIT = 1000
CHUNK = 1 << 20

LIMITS = docs_public.RateLimit(RATE)
_exporting: dict[str, int] = {}


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


# --- keys -----------------------------------------------------------------------------------------------------


async def keys(session: AsyncSession, site: DocSite) -> list[DocApiKey]:
    return list(await session.scalars(select(DocApiKey).where(DocApiKey.site_id == site.id).order_by(DocApiKey.id)))


async def create_key(session: AsyncSession, site: DocSite, name: str, expires_at: datetime | None = None, created_by: str = "") -> tuple[DocApiKey, str]:
    """The stored key and its secret, which can't be read again later."""
    if expires_at is not None:
        expires_at = expires_at.astimezone(UTC).replace(tzinfo=None) if expires_at.tzinfo else expires_at
        if expires_at <= utcnow():
            raise ServiceError("The expiry date has passed already.")
    if len(await keys(session, site)) >= MAX_PER_SITE:
        raise ServiceError(f"At most {MAX_PER_SITE} API keys per doc site: revoke one first.")
    secret = PREFIX + secrets.token_urlsafe(32)
    key = DocApiKey(
        project_id=site.project_id, site_id=site.id, name=name.strip()[:200], key_hash=_hash(secret), hint=secret[-4:], created_by=created_by[:100], expires_at=expires_at
    )  # fmt: skip
    session.add(key)
    await session.commit()
    return key, secret


async def revoke_key(session: AsyncSession, site: DocSite, key_id: int) -> DocApiKey:
    key = await session.get(DocApiKey, key_id)
    if key is None or key.site_id != site.id:
        raise NotFound("API key not found")
    await session.delete(key)
    await session.commit()
    return key


async def find_key(session: AsyncSession, site: DocSite, ref: str) -> DocApiKey:
    """By id or name (for the manager's tool)."""
    found = [k for k in await keys(session, site) if str(k.id) == ref.strip() or k.name.lower() == ref.strip().lower()]
    if not found:
        raise NotFound("API key not found")
    return found[0]


# --- who reads ------------------------------------------------------------------------------------------------


@dataclass
class Reader:
    site: DocSite
    who: str  # "k<key id>" or "t<token id>": what the limits count by


async def reader(session: AsyncSession, slug: str, authorization: str | None) -> Reader:
    """The site and who reads it: a key of this site, or a person's token with a role in its project. 401 without a
    valid one; 404 for a site that does not exist or that the key or person may not read (the same answer, so a key
    learns nothing of other sites); 429 over the key's budget."""
    scheme, _, secret = (authorization or "").partition(" ")
    secret = secret.strip()
    if scheme.lower() != "bearer" or not secret:
        raise ServiceError("An API key is needed: Authorization: Bearer <key>.", 401)
    site = await session.scalar(select(DocSite).where(DocSite.slug == slug))
    now = utcnow()
    if secret.startswith(PREFIX):
        key = await session.scalar(select(DocApiKey).where(DocApiKey.key_hash == _hash(secret)))
        if key is None or (key.expires_at is not None and key.expires_at <= now):
            raise ServiceError("This API key is not valid (revoked or expired).", 401)
        if site is None or key.site_id != site.id:
            raise NotFound("Not found")
        if key.last_used_at is None or now - key.last_used_at > USED_EVERY:
            key.last_used_at = now
            await session.commit()
        who = f"k{key.id}"
    else:
        found = await token_service.authenticate(session, secret)
        if found is None:
            raise ServiceError("This API key is not valid (revoked or expired).", 401)
        user, token = found
        if site is None or not await _may_read(session, user, site):
            raise NotFound("Not found")
        who = f"t{token.id}"
    if not LIMITS.allow(who):
        raise ServiceError("Too many requests for this key. Try again in a minute.", 429)
    return Reader(site, who)


async def _may_read(session: AsyncSession, user: User, site: DocSite) -> bool:
    return user.is_admin or await user_service.role_in(session, user, site.project_id) is not None


def start_export(who: str) -> None:
    if _exporting.get(who, 0) >= EXPORTS_AT_ONCE:
        raise ServiceError(f"At most {EXPORTS_AT_ONCE} exports per key at once.", 429)
    _exporting[who] = _exporting.get(who, 0) + 1


def end_export(who: str) -> None:
    left = _exporting.get(who, 1) - 1
    if left > 0:
        _exporting[who] = left
    else:
        _exporting.pop(who, None)


# --- what it reads --------------------------------------------------------------------------------------------


@dataclass
class Scope:
    """A version (None in ``slug`` for a site without versions) in a language, with the addresses of the API."""

    rel: DocRelease
    version: str | None
    lang: str
    api: str  # the site's API address, absolute: /api/v1/docs/<slug> at this host


async def scope(session: AsyncSession, site: DocSite, version: str | None, lang: str | None, api: str) -> Scope:
    """``version``: a version's slug (only for a site with versions; none: its default one); ``lang``: one of the
    site's languages (none: its own)."""
    rel, _, rels = await docs_releases.served(session, site, version)
    return Scope(rel, rel.slug if rels else None, docs_public.lang_of(site, lang), api)


def _version_out(r: DocRelease, default: DocRelease) -> dict:
    return {"slug": r.slug, "name": r.name, "working": r.working, "deprecated": r.deprecated, "default": r.id == default.id, "released_on": r.released_on}


async def info(session: AsyncSession, site: DocSite, api: str) -> dict:
    rel, default, rels = await docs_releases.served(session, site, None)
    count = await session.scalar(select(func.count()).select_from(DocPage).where(DocPage.site_id == site.id, docs_service.visible(rel)))
    return {
        "name": site.name, "slug": site.slug, "description": site.description, "published": site.published, "public": site.public,
        "language": site.language, "languages": [site.language, *(site.languages or [])], "versioned": bool(rels),
        "default_version": default.slug if rels else None, "versions": [_version_out(r, default) for r in rels if not r.hidden or r.id == default.id],
        "pages": count or 0, "updated_at": site.updated_at, "api": api, **await docs_areas.outline(session, site.id, rel),
    }  # fmt: skip


def _changed_since(since: datetime | None, lang: str, site: DocSite):  # noqa: ANN202
    if since is None:
        return None
    if lang == site.language:
        return DocPage.updated_at >= since
    newer = select(DocTranslation.id).where(DocTranslation.page_id == DocPage.id, DocTranslation.lang == lang, DocTranslation.translated_at >= since)
    return or_(DocPage.updated_at >= since, newer.exists())


async def _batch(session: AsyncSession, site: DocSite, sc: Scope, after: int, limit: int, since: datetime | None) -> list[DocPage]:
    q = select(DocPage).where(DocPage.site_id == site.id, docs_service.visible(sc.rel), DocPage.id > after)
    if (changed := _changed_since(since, sc.lang, site)) is not None:
        q = q.where(changed)
    return list(await session.scalars(q.order_by(DocPage.id).limit(limit)))


async def _translations(session: AsyncSession, site: DocSite, pages: list[DocPage], lang: str) -> dict[int, DocTranslation]:
    if lang == site.language or not pages:
        return {}
    rows = await session.scalars(select(DocTranslation).where(DocTranslation.page_id.in_([p.id for p in pages]), DocTranslation.lang == lang))
    return {t.page_id: t for t in rows if t.translated_at}


def _text(page: DocPage, translated: DocTranslation | None) -> tuple[str, str, datetime]:
    """Title, Markdown, when it last changed: of the translation, else of the page."""
    if translated is None:
        return page.title, page.body, page.edited_at
    return translated.title or page.title, translated.body, translated.translated_at or page.edited_at


def _item(page: DocPage, sc: Scope, translated: DocTranslation | None, fallback: bool) -> dict:
    title, _, updated = _text(page, translated)
    return {
        "id": page.page_key, "parent_id": page.parent_id, "position": page.position, "depth": page.depth, "path": page.path, "title": title,
        "lang": sc.lang, "fallback": fallback, "version": sc.version, "updated_at": updated, "area_id": page.area_id, "group_id": page.group_id,
    }  # fmt: skip


async def tree(session: AsyncSession, site: DocSite, sc: Scope, cursor: int, limit: int, since: datetime | None) -> dict:
    """{items: [{id, parent_id, position, depth, path, title, lang, fallback, version, updated_at, area_id, group_id}], next_cursor}: the
    pages in the order they were made, ``limit`` at a time (``next_cursor`` null: the last ones)."""
    rows = await _batch(session, site, sc, cursor, limit, since)
    named = await _translations(session, site, rows, sc.lang)
    other = sc.lang != site.language
    items = [_item(p, sc, named.get(p.id), other and p.id not in named) for p in rows]
    return {"items": items, "next_cursor": rows[-1].id if len(rows) == limit else None}


def _file_out(a: Attachment, sc: Scope) -> dict:
    return {"id": a.id, "name": a.filename, "content_type": a.content_type, "size": a.size, "url": f"{sc.api}/files/{a.id}"}


async def _files(session: AsyncSession, site: DocSite, bodies: list[str]) -> dict[int, Attachment]:
    ids = {int(m) for body in bodies for m in docs_service.FILE_REF.findall(body or "")}
    if not ids:
        return {}
    rows = await session.scalars(select(Attachment).where(Attachment.id.in_(ids), Attachment.doc_site_id == site.id))
    return {a.id: a for a in rows}


async def _pages_out(session: AsyncSession, site: DocSite, sc: Scope, rows: list[DocPage]) -> tuple[list[dict], dict[int, Attachment]]:
    """Full pages: their Markdown (``doc-file:<id>``, ``doc-page:<id>`` as written), ``files`` it names (with download
    addresses) and ``links``: the paths of the pages it names, by id."""
    named = await _translations(session, site, rows, sc.lang)
    texts = {p.id: _text(p, named.get(p.id)) for p in rows}
    bodies = [body for _, body, _ in texts.values()]
    files = await _files(session, site, bodies)
    paths = await docs_public.linked_paths(session, site.id, *bodies, rel=sc.rel)
    other = sc.lang != site.language
    out = []
    for p in rows:
        _, body, _ = texts[p.id]
        mine = {int(m) for m in docs_service.FILE_REF.findall(body or "")}
        links = {int(m) for m in docs_service.PAGE_REF.findall(body or "")}
        out.append({
            **_item(p, sc, named.get(p.id), other and p.id not in named), "markdown": body,
            "files": [_file_out(files[i], sc) for i in sorted(mine) if i in files], "links": {str(i): paths[i] for i in sorted(links) if i in paths},
        })  # fmt: skip
    return out, files


async def page(session: AsyncSession, site: DocSite, sc: Scope, page_id: int | None, path: str | None) -> dict:
    """One page by its id or its path ("": the home page)."""
    if page_id is not None:
        row = await docs_service.row_of(session, page_id, sc.rel)
        if row is None or row.site_id != site.id:
            raise NotFound("Not found")
    else:
        row = await docs_service.by_path(session, site.id, (path or "").strip("/"), sc.rel)
        if row is None:
            raise NotFound("Not found")
    return (await _pages_out(session, site, sc, [row]))[0][0]


async def footer(session: AsyncSession, site: DocSite, sc: Scope) -> list[dict]:
    """The site's own pages (imprint, privacy …) in their order, as Markdown."""
    pages = await docs_custom.pages(session, site.id)
    files = await _files(session, site, [docs_custom._in(p, site, sc.lang)[1] for p in pages])
    out = []
    for p in pages:
        title, body, fallback = docs_custom._in(p, site, sc.lang)
        mine = sorted({int(m) for m in docs_service.FILE_REF.findall(body or "")})
        out.append({"slug": p.slug, "path": docs_custom.path(p), "title": title, "lang": sc.lang, "fallback": fallback, "markdown": body, "updated_at": p.updated_at,
                    "files": [_file_out(files[i], sc) for i in mine if i in files]})  # fmt: skip
    return out


async def file(session: AsyncSession, site: DocSite, attachment_id: int) -> Attachment:
    return await docs_service.get_file(session, site, attachment_id)


# --- exports --------------------------------------------------------------------------------------------------


def _json(value: object) -> str:
    return json.dumps(value, default=lambda v: v.isoformat() + "Z" if isinstance(v, datetime) else str(v), ensure_ascii=False)


async def _walk(site_id: int, sc: Scope, since: datetime | None) -> AsyncIterator[tuple[DocSite, list[dict], dict[int, Attachment]]]:
    """The pages batch by batch, each batch with a session of its own (an export may run for minutes)."""
    after = 0
    while True:
        async with db.sessionmaker()() as session:
            site = await session.get(DocSite, site_id)
            if site is None:
                return
            rows = await _batch(session, site, sc, after, BATCH, since)
            if not rows:
                return
            after = rows[-1].id
            pages, files = await _pages_out(session, site, sc, rows)
        yield site, pages, files


async def export_jsonl(site_id: int, sc: Scope, since: datetime | None) -> AsyncIterator[bytes]:
    """One JSON object a line: {"type": "site", ...}, then {"type": "page", ...} for each page (as ``page``), then
    {"type": "footer", ...} for the footer pages and {"type": "file", ...} for each file the pages name, last
    {"type": "end", "pages": n, "files": n}: without it the export broke off."""
    async with db.sessionmaker()() as session:
        site = await session.get(DocSite, site_id)
        if site is None:
            return
        head = {"type": "site", **await info(session, site, sc.api), "version": sc.version, "lang": sc.lang, "since": since}
        footer_pages = await footer(session, site, sc)
    yield (_json(head) + "\n").encode()
    count, seen = 0, {}
    async for _, pages, files in _walk(site_id, sc, since):
        for p in pages:
            yield (_json({"type": "page", **p}) + "\n").encode()
        count += len(pages)
        seen.update(files)
    for p in footer_pages:
        yield (_json({"type": "footer", **p}) + "\n").encode()
    for a in seen.values():
        yield (_json({"type": "file", **_file_out(a, sc)}) + "\n").encode()
    yield (_json({"type": "end", "pages": count, "files": len(seen)}) + "\n").encode()


class _Sink(io.RawIOBase):
    """Where the zip is written: drained after every entry (a stream, so the zip uses data descriptors)."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:  # noqa: ANN001
        self.buffer += data
        return len(data)

    def take(self) -> bytes:
        out = bytes(self.buffer)
        self.buffer.clear()
        return out


def _md_name(path: str) -> str:
    return f"pages/{path or 'index'}.md"


def _file_name(a: Attachment) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in a.filename)[:120] or "file"
    return f"files/{a.id}-{safe}"


def _relative(body: str, own: str, files: dict[int, Attachment], paths: dict[int, str]) -> str:
    """``doc-file:`` and ``doc-page:`` links as relative links inside the zip."""
    here = posixpath.dirname(own)

    def page_link(m) -> str:  # noqa: ANN001
        key = int(m.group(1))
        return posixpath.relpath(_md_name(paths[key]), here) + (m.group(2) or "") if key in paths else m.group(0)

    body = re.sub(r"doc-page:(\d+)(#[\w-]+)?", page_link, body or "")
    return re.sub(r"doc-file:(\d+)", lambda m: posixpath.relpath(_file_name(files[int(m.group(1))]), here) if int(m.group(1)) in files else m.group(0), body)


def _front(p: dict) -> str:
    fields = {k: p[k] for k in ("id", "parent_id", "position", "title", "path", "lang", "fallback", "version", "updated_at")}
    return "---\n" + "".join(f"{k}: {_json(v)}\n" for k, v in fields.items()) + "---\n\n"


def _put_file(archive: zipfile.ZipFile, sink: _Sink, a: Attachment) -> Iterator[bytes]:
    """A file into the zip, a chunk at a time."""
    path = board_service.storage_path(a)
    if not path.exists():
        return
    entry = zipfile.ZipInfo(_file_name(a), date_time=a.created_at.timetuple()[:6])
    entry.compress_type = zipfile.ZIP_STORED
    with path.open("rb") as source, archive.open(entry, "w", force_zip64=a.size > (1 << 30)) as target:
        while chunk := source.read(CHUNK):
            target.write(chunk)
            yield sink.take()
    yield sink.take()


async def export_zip(site_id: int, sc: Scope, since: datetime | None) -> AsyncIterator[bytes]:
    """``site.json``; ``pages/<path>.md`` for each page (front matter with id, parent_id, position, title …; links
    between pages and to files relative inside the zip); ``footer/<slug>.md``; ``files/<id>-<name>``; last
    ``index.json`` with every page's id, parent_id, position, title and file."""
    sink = _Sink()
    archive = zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED)
    async with db.sessionmaker()() as session:
        site = await session.get(DocSite, site_id)
        if site is None:
            return
        head = {**await info(session, site, sc.api), "version": sc.version, "lang": sc.lang, "since": since}
        footer_pages = await footer(session, site, sc)
        footer_files = await _files(session, site, [p["markdown"] for p in footer_pages])
    archive.writestr("site.json", _json(head))
    yield sink.take()
    index, written = [], set()
    async for _, pages, files in _walk(site_id, sc, since):
        paths = {int(k): v for p in pages for k, v in p["links"].items()}
        for p in pages:
            name = _md_name(p["path"])
            archive.writestr(name, _front(p) + _relative(p["markdown"], name, files, paths))
            index.append({k: p[k] for k in ("id", "parent_id", "position", "title", "path")} | {"file": name})
            yield sink.take()
        for a in files.values():
            if a.id not in written:
                written.add(a.id)
                for chunk in _put_file(archive, sink, a):
                    yield chunk
    for p in footer_pages:
        name = f"footer/{p['slug']}.md"
        front = f"---\ntitle: {_json(p['title'])}\nlang: {_json(p['lang'])}\nfallback: {_json(p['fallback'])}\n---\n\n"
        archive.writestr(name, front + _relative(p["markdown"], name, footer_files, {}))
        yield sink.take()
    for a in footer_files.values():
        if a.id not in written:
            written.add(a.id)
            for chunk in _put_file(archive, sink, a):
                yield chunk
    archive.writestr("index.json", _json(index))
    archive.close()
    yield sink.take()
