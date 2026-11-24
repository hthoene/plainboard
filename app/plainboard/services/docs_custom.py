"""A doc site's own pages outside its docs tree (#121): an imprint, a privacy policy, terms, any page the footer links.

Each has a title, an address (/p/<slug> under the site and its languages) and a Markdown text with the site's files
and links (``doc-file:``, ``doc-page:``), and its title and text in the site's further languages by hand (legal
texts are not machine-translated); a language without its text shows the original. People write them in the app,
no agent tool reaches them, and the docs search and the assistant leave them out. The footer lists them in their
order. Deleting one keeps its files (they are the site's).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DocCustomPage, DocSite, utcnow
from ..schemas import DocCustomPageOut, DocCustomText
from . import docs as docs_service
from .errors import NotFound, ServiceError
from .util import slugify

LIMIT = 30  # pages of one site


def path(page: DocCustomPage) -> str:
    return f"p/{page.slug}"


def out(page: DocCustomPage) -> DocCustomPageOut:
    return DocCustomPageOut(
        id=page.id, site_id=page.site_id, position=page.position, title=page.title, slug=page.slug, body=page.body,
        texts={k: DocCustomText(**v) for k, v in (page.texts or {}).items()}, path=path(page), edited_by=page.edited_by, updated_at=page.updated_at,
    )  # fmt: skip


async def pages(session: AsyncSession, site_id: int) -> list[DocCustomPage]:
    return list(await session.scalars(select(DocCustomPage).where(DocCustomPage.site_id == site_id).order_by(DocCustomPage.position, DocCustomPage.id)))


async def get(session: AsyncSession, project_id: int, page_id: int) -> DocCustomPage:
    page = await session.get(DocCustomPage, page_id)
    if page is None or page.project_id != project_id:
        raise NotFound("Page not found")
    return page


async def by_slug(session: AsyncSession, site_id: int, slug: str) -> DocCustomPage | None:
    return await session.scalar(select(DocCustomPage).where(DocCustomPage.site_id == site_id, DocCustomPage.slug == slug).limit(1))


def _texts(site: DocSite, texts: dict[str, DocCustomText]) -> dict:
    """Only the site's further languages, only those with a text."""
    return {lang: {"title": t.title.strip()[:200], "body": t.body} for lang, t in texts.items() if lang in (site.languages or []) and (t.title.strip() or t.body.strip())}


async def _free_slug(session: AsyncSession, site_id: int, base: str, exclude_id: int | None) -> str:
    q = select(DocCustomPage.slug).where(DocCustomPage.site_id == site_id)
    if exclude_id is not None:
        q = q.where(DocCustomPage.id != exclude_id)
    taken = set(await session.scalars(q))
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


async def create(session: AsyncSession, site: DocSite, author: str, *, title: str, slug: str | None = None, body: str = "", texts: dict[str, DocCustomText] | None = None) -> DocCustomPage:
    count = int(await session.scalar(select(func.count()).select_from(DocCustomPage).where(DocCustomPage.site_id == site.id)) or 0)
    if count >= LIMIT:
        raise ServiceError(f"A site has at most {LIMIT} pages of its own.")
    page = DocCustomPage(
        project_id=site.project_id, site_id=site.id, position=count, title=title.strip()[:200], body=body, texts=_texts(site, texts or {}),
        slug=await _free_slug(session, site.id, slug or slugify(title, "page"), None), edited_by=author,
    )  # fmt: skip
    session.add(page)
    site.updated_at = utcnow()
    await session.commit()
    return page


async def update(
    session: AsyncSession, site: DocSite, page: DocCustomPage, author: str, *, title: str | None = None, slug: str | None = None, body: str | None = None,
    texts: dict[str, DocCustomText] | None = None, position: int | None = None,
) -> DocCustomPage:  # fmt: skip
    if title is not None:
        page.title = title.strip()[:200]
    if slug is not None and slug != page.slug:
        if await by_slug(session, site.id, slug) is not None:
            raise ServiceError(f"Another page of this site has the address p/{slug}.", 409)
        page.slug = slug
    if body is not None:
        page.body = body
    if texts is not None:
        page.texts = _texts(site, texts)
    if position is not None:
        ordered = [p for p in await pages(session, site.id) if p.id != page.id]
        ordered.insert(min(position, len(ordered)), page)
        for n, p in enumerate(ordered):
            p.position = n
    page.edited_by = author
    page.updated_at = site.updated_at = utcnow()
    await session.commit()
    return page


async def delete(session: AsyncSession, site: DocSite, page: DocCustomPage) -> None:
    await session.delete(page)
    await session.flush()
    for n, p in enumerate(await pages(session, site.id)):
        p.position = n
    site.updated_at = utcnow()
    await session.commit()


def _in(page: DocCustomPage, site: DocSite, lang: str) -> tuple[str, str, bool]:
    """Title and text in a language, and whether that is the original instead (no text in it yet)."""
    if lang == site.language:
        return page.title, page.body, False
    text = (page.texts or {}).get(lang) or {}
    if not (text.get("body") or "").strip():
        return text.get("title") or page.title, page.body, True
    return text.get("title") or page.title, text["body"], False


async def footer(session: AsyncSession, site: DocSite, lang: str) -> list[dict]:
    """In their order: the title in this language, and in each of the site's (``titles``: the switcher needs no request)."""
    langs = [site.language, *(site.languages or [])]
    return [{"slug": p.slug, "title": _in(p, site, lang)[0], "titles": {code: _in(p, site, code)[0] for code in langs}, "path": path(p)} for p in await pages(session, site.id)]


async def view(session: AsyncSession, site: DocSite, slug: str, base: str, lang: str, rel=None) -> dict:  # noqa: ANN001
    """A page as the served site shows it: rendered as its docs pages are."""
    from . import docs_public as public

    page = await by_slug(session, site.id, slug)
    if page is None:
        raise NotFound("Not found")
    title, body, fallback = _in(page, site, lang)
    here = public.lang_base(site, base, lang)
    rendered = public.render(body, base=here, files_base=f"{public.api_base(site)}/files", page_paths=await public.linked_paths(session, site.id, body, rel=rel))
    return {
        "id": page.id, "slug": page.slug, "path": path(page), "title": title, "lang": lang, "fallback": fallback, "html": rendered.html, "toc": rendered.toc,
        "description": public.describe(rendered.text) or site.description, "updated_at": page.updated_at,
    }  # fmt: skip


def relink(page: DocCustomPage, files: dict[int, int], page_ids: dict[int, int]) -> None:
    """After a project moved: its files and links by their new ids."""
    page.body = docs_service._relink(page.body, files, page_ids)
    page.texts = {lang: {**t, "body": docs_service._relink(t.get("body") or "", files, page_ids)} for lang, t in (page.texts or {}).items()}
