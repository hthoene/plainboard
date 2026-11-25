"""Areas and groups of a doc site's navigation (#159): order for large sites.

An area ("Guides", "Reference") is a part of the site with its own pages at the top: the served site shows the areas
in a switcher at the top of its navigation (with icon and a short description) and as cards on its home page, and its
navigation only the pages of the area the open page is in. A group is a heading over pages at the top, inside an area
or without one. Only pages at the top name an area and a group (``DocPage.area_id``/``group_id``, by key); the pages
below them go with them. What names no area shows in every area, what names no group above the groups; a site
without areas and groups looks as it always did.

Versioned like pages (services/docs.py, #126): a row holds an area or group in a range of the site's versions, a change
in one version splits it off first (copy on write), and ``area_key``/``group_key`` name it in every version (the id of
its row with the highest range, kept when that row goes). A site has a few dozen of them at most, so every read takes
all of a version at once.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DocArea, DocGroup, DocPage, DocRelease, DocSite, utcnow
from . import docs as docs_service
from .errors import NotFound, ServiceError

MAX_AREAS = 30
MAX_GROUPS = 200
ICONS = (
    "book-open", "rocket", "code", "terminal", "settings", "shield", "server", "cloud", "database", "plug", "puzzle", "layers", "compass",
    "graduation-cap", "lightbulb", "wrench", "users", "newspaper", "life-buoy", "map", "box", "zap", "globe", "file-text",
)  # fmt: skip

Row = DocArea | DocGroup


def _key(row: Row) -> int:
    return (row.area_key if isinstance(row, DocArea) else row.group_key) or row.id


def _visible(model: type[Row], rel: DocRelease | None):
    if rel is None or rel.working:
        return model.until_seq.is_(None)
    from sqlalchemy import and_, or_

    return and_(model.since_seq <= rel.seq, or_(model.until_seq.is_(None), model.until_seq >= rel.seq))


async def areas(session: AsyncSession, site_id: int, rel: DocRelease | None = None) -> list[DocArea]:
    return list(await session.scalars(select(DocArea).where(DocArea.site_id == site_id, _visible(DocArea, rel)).order_by(DocArea.position, DocArea.id)))


async def groups(session: AsyncSession, site_id: int, rel: DocRelease | None = None) -> list[DocGroup]:
    return list(await session.scalars(select(DocGroup).where(DocGroup.site_id == site_id, _visible(DocGroup, rel)).order_by(DocGroup.position, DocGroup.id)))


def area_out(a: DocArea) -> dict[str, Any]:
    return {"id": _key(a), "title": a.title, "description": a.description, "icon": a.icon, "position": a.position, "texts": dict(a.texts or {})}


def group_out(g: DocGroup) -> dict[str, Any]:
    return {"id": _key(g), "area_id": g.area_id, "title": g.title, "position": g.position, "texts": dict(g.texts or {})}


async def outline(session: AsyncSession, site_id: int, rel: DocRelease | None = None) -> dict[str, list[dict[str, Any]]]:
    return {"areas": [area_out(a) for a in await areas(session, site_id, rel)], "groups": [group_out(g) for g in await groups(session, site_id, rel)]}


def served(rows_a: list[DocArea], rows_g: list[DocGroup], lang: str) -> tuple[list[dict], list[dict]]:
    """For the served site, in a language (a missing translation: the original)."""

    def text(row: Row, field: str) -> str:
        return str(((row.texts or {}).get(lang) or {}).get(field) or getattr(row, field))

    return (
        [{"id": _key(a), "title": text(a, "title"), "description": text(a, "description"), "icon": a.icon or "book-open"} for a in rows_a],
        [{"id": _key(g), "area": g.area_id, "title": text(g, "title")} for g in rows_g],
    )


# --- copy on write, as for pages ------------------------------------------------------------------------------


def _copy(row: Row, since: int, until: int | None) -> Row:
    model = type(row)
    cols = [c.key for c in model.__table__.columns if c.key not in ("id", "since_seq", "until_seq")]
    twin = model(**{c: getattr(row, c) for c in cols}, since_seq=since, until_seq=until)
    twin.texts = dict(row.texts or {})
    return twin


async def _isolate(session: AsyncSession, row: Row, span: docs_service.Span) -> Row:
    below, above = docs_service._parts(row, span)  # noqa: SLF001
    if below is None and above is None:
        return row
    if above is not None:
        target = _copy(row, *span)
        session.add(target)
        row.since_seq, row.until_seq = above
    else:
        target = row
        row.since_seq, row.until_seq = span
    if below is not None:
        session.add(_copy(row, *below))
    await session.flush()
    return target


async def _remove(session: AsyncSession, row: Row) -> None:
    """A row gone from every version; the key row first takes over its highest other row (the key stays)."""
    model = type(row)
    key_col = model.area_key if model is DocArea else model.group_key
    if _key(row) == row.id:
        other = await session.scalar(select(model).where(key_col == row.id, model.id != row.id).order_by(model.since_seq.desc()).limit(1))
        if other is not None:
            for c in model.__table__.columns:
                if c.key not in ("id",):
                    setattr(row, c.key, getattr(other, c.key))
            await session.delete(other)
            await session.flush()
            return
    await session.delete(row)
    await session.flush()


async def _drop(session: AsyncSession, row: Row, span: docs_service.Span) -> None:
    below, above = docs_service._parts(row, span)  # noqa: SLF001
    if above is not None:
        if below is not None:
            session.add(_copy(row, *below))
        row.since_seq, row.until_seq = above
    elif below is not None:
        row.since_seq, row.until_seq = below
    else:
        await _remove(session, row)
    await session.flush()


async def release_deleted(session: AsyncSession, site_id: int, seq: int, others: list[int]) -> None:
    """A released version goes: the rows only it had go with it (services/docs_releases.delete)."""
    for model in (DocGroup, DocArea):
        q = select(model).where(model.site_id == site_id, model.since_seq <= seq, model.until_seq.is_not(None), model.until_seq >= seq)
        for row in list(await session.scalars(q)):
            if not any(row.since_seq <= s <= (row.until_seq or 0) for s in others):
                await _remove(session, row)


# --- changes -------------------------------------------------------------------------------------------------


async def _find(session: AsyncSession, model: type[Row], site: DocSite, key: int, rel: DocRelease | None) -> Row:
    key_col = model.area_key if model is DocArea else model.group_key
    row = await session.scalar(select(model).where(model.site_id == site.id, key_col == key, _visible(model, rel)).limit(1))
    if row is None:
        what = "area" if model is DocArea else "group"
        raise NotFound(f"No {what} {key} in doc site {site.slug}" + (f" (version {rel.name})" if rel is not None and not rel.working else ""))
    return row


async def get_area(session: AsyncSession, site: DocSite, key: int, rel: DocRelease | None = None) -> DocArea:
    return await _find(session, DocArea, site, key, rel)  # type: ignore[return-value]


async def get_group(session: AsyncSession, site: DocSite, key: int, rel: DocRelease | None = None) -> DocGroup:
    return await _find(session, DocGroup, site, key, rel)  # type: ignore[return-value]


def _icon(icon: str) -> str:
    icon = icon.strip().lower()
    if icon and icon not in ICONS:
        raise ServiceError(f"Unknown icon {icon!r}. Icons: {', '.join(ICONS)}")
    return icon


def _texts(texts: dict[str, Any], fields: tuple[str, ...]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for lang, value in (texts or {}).items():
        if isinstance(value, dict):
            kept = {f: str(value.get(f) or "").strip()[:200] for f in fields if str(value.get(f) or "").strip()}
            if kept:
                out[str(lang)[:20]] = kept
    return out


async def _reorder(session: AsyncSession, model: type[Row], site: DocSite, row: Row, position: int | None, rel: DocRelease | None, span: docs_service.Span) -> Row:
    """Put the row at ``position`` among the others of this version (None: last), numbering them anew."""
    rows = [r for r in (await areas(session, site.id, rel) if model is DocArea else await groups(session, site.id, rel)) if _key(r) != _key(row)]
    at = len(rows) if position is None else max(0, min(position, len(rows)))
    rows.insert(at, row)
    for n, r in enumerate(rows):
        if r.position != n:
            if r is not row and r.id is not None:
                r = await _isolate(session, r, span)
            r.position = n
    return row


async def _changed(session: AsyncSession, site: DocSite) -> None:
    site.updated_at = utcnow()  # the served site's cache
    await session.commit()
    await docs_service.publish_site(session, site)


async def create_area(
    session: AsyncSession, site: DocSite, *, title: str = "", description: str = "", icon: str = "", position: int | None = None,
    texts: dict | None = None, rel: DocRelease | None = None,
) -> DocArea:  # fmt: skip
    if len(await areas(session, site.id, rel)) >= MAX_AREAS:
        raise ServiceError(f"A site has at most {MAX_AREAS} areas.")
    span = await docs_service.span_of(session, site.id, rel)
    row = DocArea(
        project_id=site.project_id, site_id=site.id, title=_title(title), description=description.strip()[:200], icon=_icon(icon),
        texts=_texts(texts or {}, ("title", "description")), since_seq=span[0], until_seq=span[1], position=0,
    )  # fmt: skip
    await _reorder(session, DocArea, site, row, position, rel, span)
    session.add(row)
    await session.flush()
    row.area_key = row.id
    await _changed(session, site)
    return row


async def create_group(
    session: AsyncSession, site: DocSite, *, title: str = "", area_id: int | None = None, position: int | None = None, texts: dict | None = None,
    rel: DocRelease | None = None,
) -> DocGroup:  # fmt: skip
    if len(await groups(session, site.id, rel)) >= MAX_GROUPS:
        raise ServiceError(f"A site has at most {MAX_GROUPS} groups.")
    if area_id is not None:
        await get_area(session, site, area_id, rel)
    span = await docs_service.span_of(session, site.id, rel)
    row = DocGroup(
        project_id=site.project_id, site_id=site.id, title=_title(title), area_id=area_id, texts=_texts(texts or {}, ("title",)),
        since_seq=span[0], until_seq=span[1], position=0,
    )  # fmt: skip
    await _reorder(session, DocGroup, site, row, position, rel, span)
    session.add(row)
    await session.flush()
    row.group_key = row.id
    await _changed(session, site)
    return row


def _title(title: str) -> str:
    title = title.strip()[:100]
    if not title:
        raise ServiceError("A title is needed.")
    return title


async def update_area(session: AsyncSession, site: DocSite, area: DocArea, rel: DocRelease | None = None, **changes: Any) -> DocArea:
    """``title``, ``description``, ``icon``, ``texts``, ``position``: only what is given changes, in that version only."""
    span = await docs_service.span_of(session, site.id, rel)
    area = await _isolate(session, area, span)  # type: ignore[assignment]
    if changes.get("title") is not None:
        area.title = _title(changes["title"])
    if changes.get("description") is not None:
        area.description = changes["description"].strip()[:200]
    if changes.get("icon") is not None:
        area.icon = _icon(changes["icon"])
    if changes.get("texts") is not None:
        area.texts = _texts(changes["texts"], ("title", "description"))
    if "position" in changes and changes["position"] is not None:
        await _reorder(session, DocArea, site, area, changes["position"], rel, span)
    area.updated_at = utcnow()
    await _changed(session, site)
    return area


async def update_group(session: AsyncSession, site: DocSite, group: DocGroup, rel: DocRelease | None = None, **changes: Any) -> DocGroup:
    """``title``, ``area_id`` (None: no area; given only to change it), ``texts``, ``position``. Its pages follow it into
    another area."""
    span = await docs_service.span_of(session, site.id, rel)
    group = await _isolate(session, group, span)  # type: ignore[assignment]
    if changes.get("title") is not None:
        group.title = _title(changes["title"])
    if changes.get("texts") is not None:
        group.texts = _texts(changes["texts"], ("title",))
    if "area_id" in changes and changes["area_id"] != group.area_id:
        if changes["area_id"] is not None:
            await get_area(session, site, changes["area_id"], rel)
        group.area_id = changes["area_id"]
        for page in await _pages(session, site.id, rel, DocPage.group_id == _key(group)):
            page = await docs_service.isolate(session, page, span)
            page.area_id = group.area_id
    if "position" in changes and changes["position"] is not None:
        await _reorder(session, DocGroup, site, group, changes["position"], rel, span)
    group.updated_at = utcnow()
    await _changed(session, site)
    return group


async def _pages(session: AsyncSession, site_id: int, rel: DocRelease | None, cond) -> list[DocPage]:  # noqa: ANN001
    return list(await session.scalars(select(DocPage).where(DocPage.site_id == site_id, DocPage.parent_id.is_(None), docs_service.visible(rel), cond)))


async def delete_area(session: AsyncSession, site: DocSite, area: DocArea, rel: DocRelease | None = None) -> None:
    """From that version only; its pages and groups stay, without an area."""
    span = await docs_service.span_of(session, site.id, rel)
    key = _key(area)
    for page in await _pages(session, site.id, rel, DocPage.area_id == key):
        page = await docs_service.isolate(session, page, span)
        page.area_id = None
    for group in await groups(session, site.id, rel):
        if group.area_id == key:
            group = await _isolate(session, group, span)  # type: ignore[assignment]
            group.area_id = None
    await _drop(session, area, span)
    await _changed(session, site)


async def delete_group(session: AsyncSession, site: DocSite, group: DocGroup, rel: DocRelease | None = None) -> None:
    """From that version only; its pages stay, without a group (in its area)."""
    span = await docs_service.span_of(session, site.id, rel)
    for page in await _pages(session, site.id, rel, DocPage.group_id == _key(group)):
        page = await docs_service.isolate(session, page, span)
        page.group_id = None
    await _drop(session, group, span)
    await _changed(session, site)


async def place(session: AsyncSession, site: DocSite, page: DocPage, area_id: int | None, group_id: int | None, rel: DocRelease | None) -> None:
    """Set a top-level page's area and group (keys; None: none). A group in an area puts the page in that area too."""
    if page.parent_id is not None:
        if area_id is not None or group_id is not None:
            raise ServiceError("Only pages at the top have an area or a group; the pages below them go with them.")
        page.area_id = page.group_id = None
        return
    if group_id is not None:
        group = await get_group(session, site, group_id, rel)
        if group.area_id is not None:
            if area_id is not None and area_id != group.area_id:
                raise ServiceError(f"Group {group_id} is in area {group.area_id}, not in area {area_id}.")
            area_id = group.area_id
    if area_id is not None:
        await get_area(session, site, area_id, rel)
    page.area_id, page.group_id = area_id, group_id


async def unplace_below(session: AsyncSession, page_key: int) -> None:
    """A page moved below another one: no area or group any more (in every row: only pages at the top have them)."""
    await session.execute(update(DocPage).where(DocPage.page_key == page_key, DocPage.parent_id.is_not(None)).values(area_id=None, group_id=None))
