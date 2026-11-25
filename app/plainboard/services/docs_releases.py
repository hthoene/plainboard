"""Versions of a doc site (#126): the working version, written now, and the versions cut from it.

Every site has one working version (``DocRelease.working``, "next" until renamed): what people and agents write, what
the docs watcher's update jobs are about. At a release the user (or the manager on request) cuts a version ("1.0"):
from then on it keeps the pages, their translations and the files they show as they were, while the working version
goes on; anyone may still correct a released version (its own copy of the pages changed, services/docs.isolate).

Built for thousands of pages and many versions: a cut copies nothing (it only takes the working version's ``seq``),
and a page is one row for all versions in which it is the same (``DocPage.since_seq``/``until_seq``); only a change
makes a second row, for the versions it goes to. So the rows, the search index and the translations grow with the
changes, not with pages × versions, and every query of a version is the same indexed query with a range condition.

Versions are optional per site (``DocSite.versioned``, off by default): a site without them always shows its current
state (the working version, no version in its addresses, no switcher, no banner). The first cut turns them on;
turned off again, nothing is deleted: the versions stay (and can still be edited in the app) but are not served.

Visitors get the default version at /docs/<site>/ (the one marked ``is_default``, else the latest released one that
is neither hidden nor deprecated, else the working one) and any other at /docs/<site>/<version slug>/. ``hidden``
takes a version off the served site (for the working version: whether it is public at all). The site's footer pages
(imprint, privacy) and its files belong to the site, not to a version: legal texts are the current ones everywhere.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DocPage, DocRelease, DocSite, utcnow
from ..schemas import DocReleaseOut
from . import docs as docs_service
from . import docs_areas
from .errors import NotFound, ServiceError

SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
RESERVED = {"p", "api", "assets", "files", "sitemap.xml", "robots.txt"}
WORKING_WORDS = ("", "working", "work", "next", "dev", "current", "draft")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-._")[:40] or "v"


def _naive(at: datetime | None) -> datetime | None:
    return at.astimezone(UTC).replace(tzinfo=None) if at is not None and at.tzinfo is not None else at


def pick_default(rels: list[DocRelease]) -> DocRelease:
    """What visitors get without naming a version."""
    shown = [r for r in rels if not r.hidden]
    released = sorted((r for r in shown if not r.working), key=lambda r: -r.seq)
    working = next((r for r in rels if r.working), None)
    for r in (
        next((r for r in shown if r.is_default), None),
        next((r for r in released if not r.deprecated), None),
        working if working is not None and not working.hidden else None,
        released[0] if released else None,
    ):
        if r is not None:
            return r
    assert working is not None
    return working


def out(site: DocSite, rel: DocRelease, default: DocRelease) -> DocReleaseOut:
    return DocReleaseOut(
        id=rel.id, site_id=rel.site_id, name=rel.name, slug=rel.slug, working=rel.working, seq=rel.seq, notes=rel.notes, released_on=rel.released_on,
        deprecated=rel.deprecated, hidden=rel.hidden, is_default=rel.is_default, default=rel.id == default.id,
        url=f"/docs/{site.slug}/" if rel.id == default.id else f"/docs/{site.slug}/{rel.slug}/", created_at=rel.created_at, updated_at=rel.updated_at,
    )  # fmt: skip


async def releases(session: AsyncSession, site_id: int) -> list[DocRelease]:
    """The working version first, then the released ones, the newest first."""
    rows = list(await session.scalars(select(DocRelease).where(DocRelease.site_id == site_id).order_by(DocRelease.working.desc(), DocRelease.seq.desc())))
    if not any(r.working for r in rows):
        rows.insert(0, await docs_service.working(session, site_id))
    return rows


async def outs(session: AsyncSession, site: DocSite) -> list[DocReleaseOut]:
    rels = await releases(session, site.id)
    default = pick_default(rels)
    return [out(site, r, default) for r in rels]


async def outs_by_site(session: AsyncSession, site_ids: list[int]) -> dict[int, list[DocReleaseOut]]:
    rows = list(await session.scalars(select(DocRelease).where(DocRelease.site_id.in_(site_ids)).order_by(DocRelease.working.desc(), DocRelease.seq.desc())))
    sites = {s.id: s for s in await session.scalars(select(DocSite).where(DocSite.id.in_(site_ids)))}
    by: dict[int, list[DocRelease]] = {}
    for r in rows:
        by.setdefault(r.site_id, []).append(r)
    result = {}
    for site_id, rels in by.items():
        if any(r.working for r in rels):
            default = pick_default(rels)
            result[site_id] = [out(sites[site_id], r, default) for r in rels]
    return result


async def get(session: AsyncSession, site: DocSite, release_id: int | None) -> DocRelease | None:
    """A version of the site by its id (None: the working one, as None)."""
    if release_id is None:
        return None
    rel = await session.get(DocRelease, release_id)
    if rel is None or rel.site_id != site.id:
        raise NotFound("Doc version not found")
    return None if rel.working else rel


async def get_any(session: AsyncSession, project_id: int, release_id: int) -> tuple[DocSite, DocRelease]:
    rel = await session.get(DocRelease, release_id)
    if rel is None or rel.project_id != project_id:
        raise NotFound("Doc version not found")
    site = await docs_service.get_site(session, project_id, rel.site_id)
    return site, rel


async def find(session: AsyncSession, site: DocSite, ref: str | None) -> DocRelease | None:
    """A version by its name, slug or id (for agents); empty: the working one (None)."""
    rel = await find_any(session, site, ref)
    return None if rel.working else rel


async def find_any(session: AsyncSession, site: DocSite, ref: str | None) -> DocRelease:
    ref = (ref or "").strip()
    rels = await releases(session, site.id)
    for r in rels:
        if ref.lower() in (str(r.id), r.slug, r.name.lower()):
            return r
    if ref.lower() in WORKING_WORDS:
        return next(r for r in rels if r.working)
    names = ", ".join(r.name + (" (working)" if r.working else "") for r in rels)
    raise NotFound(f"No version {ref!r} of {site.slug}. Versions: {names}")


async def _check_slug(session: AsyncSession, site: DocSite, slug: str, exclude_id: int | None) -> str:
    slug = slug.strip().lower()
    if not SLUG.match(slug) or slug in RESERVED or slug == site.language or slug in (site.languages or []):
        raise ServiceError(f"Not a version address: {slug!r} (letters, digits, . _ -; not a language code of the site).")
    found = await session.scalar(select(DocRelease.id).where(DocRelease.site_id == site.id, DocRelease.slug == slug))
    if found is not None and found != exclude_id:
        raise ServiceError(f"The site has a version at {slug!r} already.", 409)
    return slug


async def _changed(session: AsyncSession, site: DocSite) -> None:
    site.updated_at = utcnow()  # the served site's cache
    await session.commit()
    await docs_service.publish_site(session, site)


async def cut(session: AsyncSession, site: DocSite, *, name: str, slug: str | None = None, notes: str = "", released_on: datetime | None = None) -> DocRelease:
    """A version of the site as the working version is now: nothing is copied (see the module), so it takes no time
    whatever the site's size. The working version goes on."""
    name = name.strip()[:80]
    if not name:
        raise ServiceError("A version needs a name, e.g. 1.0.")
    if await session.scalar(select(DocRelease.id).where(DocRelease.site_id == site.id, DocRelease.name == name)) is not None:
        raise ServiceError(f"The site has a version {name} already.", 409)
    slug = await _check_slug(session, site, slug or _slug(name), None)
    working = await docs_service.working(session, site.id)
    rel = DocRelease(
        project_id=site.project_id, site_id=site.id, seq=working.seq, working=False, name=name, slug=slug, notes=notes.strip(), released_on=_naive(released_on) or utcnow(),
    )  # fmt: skip
    working.seq += 1
    site.versioned = True  # the first cut turns versions on
    session.add(rel)
    await _changed(session, site)
    return rel


async def update(
    session: AsyncSession, site: DocSite, rel: DocRelease, *, name: str | None = None, slug: str | None = None, notes: str | None = None,
    released_on: datetime | None = None, deprecated: bool | None = None, hidden: bool | None = None, default: bool | None = None,
) -> DocRelease:  # fmt: skip
    """Rename it, give it a new address, notes or date, mark it deprecated or hidden, make it the visitors' default
    (False: back to the latest released one). The working version: name, address, hidden (public or not), default."""
    if name is not None and name.strip() and name.strip() != rel.name:
        if await session.scalar(select(DocRelease.id).where(DocRelease.site_id == site.id, DocRelease.name == name.strip(), DocRelease.id != rel.id)) is not None:
            raise ServiceError(f"The site has a version {name.strip()} already.", 409)
        rel.name = name.strip()[:80]
    if slug is not None and slug.strip().lower() != rel.slug:
        rel.slug = await _check_slug(session, site, slug, rel.id)
    if notes is not None:
        rel.notes = notes.strip()
    if released_on is not None and not rel.working:
        rel.released_on = _naive(released_on)
    if deprecated is not None:
        if rel.working and deprecated:
            raise ServiceError("The working version cannot be deprecated.")
        rel.deprecated = deprecated
    if hidden is not None:
        rel.hidden = hidden
    if default is not None:
        for other in await releases(session, site.id):
            other.is_default = default and other.id == rel.id
    rel.updated_at = utcnow()
    await _changed(session, site)
    return rel


async def delete(session: AsyncSession, site: DocSite, rel: DocRelease) -> None:
    """A released version, with what only it had (its own copies of pages and their translations)."""
    if rel.working:
        raise ServiceError("The working version cannot be deleted.")
    others = [r.seq for r in await releases(session, site.id) if not r.working and r.id != rel.id]
    q = select(DocPage).where(DocPage.site_id == site.id, DocPage.since_seq <= rel.seq, DocPage.until_seq.is_not(None), DocPage.until_seq >= rel.seq)
    for row in list(await session.scalars(q.order_by(DocPage.depth.desc()))):  # the deepest first: nothing goes by a cascade before its turn
        if not any(row.since_seq <= seq <= (row.until_seq or 0) for seq in others):
            await docs_service.remove_row(session, row)
    await docs_areas.release_deleted(session, site.id, rel.seq, others)
    await session.delete(rel)
    await _changed(session, site)


# --- the served site ----------------------------------------------------------------------------------------------


async def served(session: AsyncSession, site: DocSite, slug: str | None) -> tuple[DocRelease, DocRelease, list[DocRelease]]:
    """(the version asked for, the default, all versions) for a visitor; a hidden one only as the default."""
    rels = await releases(session, site.id)
    if not site.versioned:  # always the current state, at the addresses without a version
        if slug:
            raise NotFound("Not found")
        working = next(r for r in rels if r.working)
        return working, working, []
    default = pick_default(rels)
    if not slug:
        return default, default, rels
    rel = next((r for r in rels if r.slug == slug), None)
    if rel is None or (rel.hidden and rel.id != default.id):
        raise NotFound("Not found")
    return rel, default, rels


def public_list(rels: list[DocRelease], default: DocRelease) -> list[dict]:
    """The versions a visitor may switch to: the working one first, then the newest released."""
    return [
        {"slug": r.slug, "name": r.name, "working": r.working, "deprecated": r.deprecated, "default": r.id == default.id, "released_on": r.released_on}
        for r in rels
        if not r.hidden or r.id == default.id
    ]
