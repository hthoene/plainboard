"""Served doc sites (#118, services/docs_public.py): public ones without login, private ones for people with a role
in their project.

- ``/docs/<slug>/[<version>/][<lang>/]<page path>``: the page as HTML (without a version: the site's default one, #126): the frontend's ``docs.html`` (a Vite entry of its own) with
  the page's title, description, canonical address and hreflang alternates in its head, the page and its navigation
  rendered in ``#docs-root`` (what search engines read) and ``window.__PB_DOCS__`` = {site, page} for the script;
  without a frontend build a plain page. A private site without a signed-in reader: 401 with a sign-in form
  (``sign_in``: true in the state). ``/docs/<slug>/sitemap.xml`` and ``robots.txt``.
- JSON under ``/api/public/docs/<slug>``: the site with its top level (``?lang=``), ``/nav?parent=&offset=&limit=``
  (children, page by page), ``/page?path=&lang=``, ``/search?q=&lang=``, ``/files/<id>``. ETag on every view. Each
  takes ``?release=<version slug>`` (none: the default version); the site's view names the version shown
  (``version``), the ones to switch to (``versions``) and the site's address without the version (``site_base``);
  a site without versions (``versioned`` false) has none of them and takes no ``release``.
- ``/custom/<page>?lang=``: a page of the site's own (#121, services/docs_custom.py), at ``p/<page>`` in the HTML.
- ``POST /api/public/docs/<slug>/assistant`` {text, conversation?, lang?}: the site's assistant (#119, where
  ``assistant`` is true in the site's view), the answer streamed as NDJSON (``DocsAssistant.stream``); refused with
  {detail} (404 off, 413 too long, 429 a limit, 503 cannot run). ``DELETE .../assistant/<conversation>``: closed.
- At a site's own verified domain (``DocHost``) the same at the root, plus ``/api/auth/login`` and ``logout`` for a
  private site (the session cookie is that domain's). Nothing else of PlainBoard answers there.
Drafts and everything else are 404. Each client address has a request budget per minute.
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import get_config
from ..models import DocSite
from ..security import SESSION_COOKIE
from ..services import board as board_service
from ..services import docs as docs_service
from ..services import docs_custom, docs_domains, docs_theme
from ..services import docs_public as public
from ..services.errors import NotFound, ServiceError
from .deps import Runs, Session
from .guard import _hosts, _netloc, is_https

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/public/docs", tags=["docs"])  # JSON (services/access.py: /api/public is open; they check by themselves)
pages = APIRouter(tags=["docs"])  # the HTML pages: no rule, they check by themselves

PAGES = public.RateLimit(240)
SEARCHES = public.RateLimit(30)
FILES = public.RateLimit(600)
ASSISTANT = public.RateLimit(30)  # requests to the assistant, also refused ones (its own limits: services/docs_assistant.py)
DOMAIN_SLUG = "docs_domain_slug"  # in the request's state: it came to the site's own domain
DOMAIN_PATHS = ("/api/auth/login", "/api/auth/logout")  # what a site's own domain passes on besides the site


def _client(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
    return forwarded.strip() or (request.client.host if request.client else "?")


def _limit(limit: public.RateLimit, request: Request) -> None:
    if not limit.allow(_client(request)):
        raise ServiceError("Too many requests. Try again in a minute.", 429)


def _base(request: Request, site: DocSite) -> str:
    return "" if request.scope.get("state", {}).get(DOMAIN_SLUG) == site.slug else f"/docs/{site.slug}"


Release = Query(default=None, max_length=40, description="A version's slug (#126); none: the site's default version")


async def _version(session, request: Request, site: DocSite, release: str | None) -> public.Version:  # noqa: ANN001
    return await public.version(session, site, release, _base(request, site))


async def _site(session, request: Request, slug: str) -> DocSite:  # noqa: ANN001
    on_domain = request.scope.get("state", {}).get(DOMAIN_SLUG)
    if on_domain is not None and on_domain != slug:
        raise NotFound("Not found")  # a site's domain serves that site only
    if (grant := public.preview_grant(request.cookies.get(public.PREVIEW_COOKIE))) is not None:  # the preview tool's browser (#128)
        found = await session.scalar(select(DocSite).where(DocSite.slug == slug))
        if found is not None and found.id == grant.site_id:
            return found
    site = await public.served(session, slug)
    await public.may_read(session, site, request.cookies.get(SESSION_COOKIE), request.headers.get("authorization"))
    return site


async def _view(request: Request, site: DocSite, key: tuple, make) -> Response:  # noqa: ANN001
    """A JSON view from the cache (keyed by the site's stamp), with an ETag; a private one is never kept by a proxy."""
    full = (site.id, site.updated_at.isoformat(), _base(request, site), *key)
    data = public.CACHE.get(full)
    if data is None:
        data = public.CACHE.put(full, jsonable_encoder(await make()))
    etag = '"' + hashlib.sha256(repr(full).encode()).hexdigest()[:20] + '"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=60" if site.public else "private, no-cache", "Vary": "Cookie"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(data, headers=headers)


@router.get("/{slug}")
async def site(slug: str, request: Request, session: Session, lang: str | None = Query(default=None, max_length=20), release: str | None = Release) -> Response:
    """{name, slug, description, public, base, api, language, languages, lang, lang_base, project, updated_at, home,
    nav: {items: [{id, title, path, children}], total, offset} (the top level), version: {slug, name, working,
    deprecated, default, released_on, notes}, versions: [{slug, name, working, deprecated, default, released_on}],
    site_base}. ``base`` is the version's address (the site's for the default version)."""
    _limit(PAGES, request)
    found = await _site(session, request, slug)
    language = public.lang_of(found, lang)
    ver = await _version(session, request, found, release)
    return await _view(request, found, ("site", language, ver.rel.id), lambda: public.site_view(session, found, ver.base, language, ver))


@router.get("/{slug}/nav")
async def nav(
    slug: str, request: Request, session: Session, parent: int | None = None, lang: str | None = Query(default=None, max_length=20),
    offset: int = Query(default=0, ge=0), limit: int = Query(default=docs_service.PAGE_LIMIT, ge=1, le=docs_service.PAGE_LIMIT), release: str | None = Release,
) -> Response:  # fmt: skip
    """The children of a page (none: the top), page by page: {items: [{id, title, path, children}], total, offset}."""
    _limit(PAGES, request)
    found = await _site(session, request, slug)
    language = public.lang_of(found, lang)
    ver = await _version(session, request, found, release)
    return await _view(request, found, ("nav", language, parent, offset, limit, ver.rel.id), lambda: public.nav(session, found, parent, language, offset, limit, ver.rel))


@router.get("/{slug}/page")
async def page(
    slug: str, request: Request, session: Session, path: str = Query(default="", max_length=2000), lang: str | None = Query(default=None, max_length=20),
    release: str | None = Release,
) -> Response:  # fmt: skip
    """{id, title, path, lang, fallback, html, toc: [{level, id, text}], markdown, description, updated_at, breadcrumbs,
    prev, next, sidebar: [nav of the top, nav of each page on the way down], children: nav}"""
    _limit(PAGES, request)
    found = await _site(session, request, slug)
    language = public.lang_of(found, lang)
    ver = await _version(session, request, found, release)
    return await _view(request, found, ("page", language, path.strip("/"), ver.rel.id), lambda: public.page_view(session, found, path, ver.base, language, ver.rel))


@router.get("/{slug}/custom/{page_slug}")
async def custom_page(slug: str, page_slug: str, request: Request, session: Session, lang: str | None = Query(default=None, max_length=20), release: str | None = Release) -> Response:
    """A page of the site's own (#121: imprint, privacy …, its address p/<page_slug>): {id, slug, path, title, lang,
    fallback, html, toc, description, updated_at}."""
    _limit(PAGES, request)
    found = await _site(session, request, slug)
    language = public.lang_of(found, lang)
    ver = await _version(session, request, found, release)
    return await _view(request, found, ("custom", language, page_slug, ver.rel.id), lambda: docs_custom.view(session, found, page_slug, ver.base, language, ver.rel))


@router.get("/{slug}/search")
async def search(
    slug: str, request: Request, session: Session, q: str = Query(min_length=1, max_length=200), lang: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=10, ge=1, le=20), release: str | None = Release,
) -> JSONResponse:  # fmt: skip
    """[{title, path, url, snippet}] in that language: by words, and by meaning where semantic search runs. Header
    ``X-Search-Words-Only: 1`` when the site's vector searches of this minute were used up."""
    _limit(SEARCHES, request)
    found = await _site(session, request, slug)
    language = public.lang_of(found, lang)
    ver = await _version(session, request, found, release)
    hits, words_only = await public.search_hits(session, found, q, ver.base, language, limit, rel=ver.rel)
    headers = {"Cache-Control": "private, no-cache" if not found.public else "public, max-age=60"}
    if words_only:
        headers = {"Cache-Control": "private, no-cache", "X-Search-Words-Only": "1"}  # not cached: meaning comes back within the minute
    return JSONResponse(jsonable_encoder(hits), headers=headers)


@router.get("/{slug}/files/{attachment_id}")
async def file(slug: str, attachment_id: int, request: Request, session: Session) -> FileResponse:
    _limit(FILES, request)
    found = await _site(session, request, slug)
    attachment = await docs_service.get_file(session, found, attachment_id)
    path = board_service.storage_path(attachment)
    if not path.exists():
        raise NotFound("Not found")
    shown = board_service.inline_type(path, attachment.filename)  # pictures, PDF, text inline; never a page or an SVG
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": f"{'public' if found.public else 'private'}, max-age=3600"}
    if shown != "application/pdf":
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    if shown:
        return FileResponse(path, media_type=shown, filename=attachment.filename, content_disposition_type="inline", headers=headers)
    return FileResponse(path, media_type=attachment.content_type, filename=attachment.filename, content_disposition_type="attachment", headers=headers)


class AssistantIn(BaseModel):
    text: str = Field(min_length=1, max_length=10000)  # the assistant's own, friendlier limit is lower
    conversation: str | None = Field(default=None, max_length=100)  # a follow-up: the conversation it goes on
    lang: str | None = Field(default=None, max_length=20)  # the visitor's language: one of the site's
    release: str | None = Field(default=None, max_length=40)  # the version the visitor reads (its slug; none: the default one)


def _same_site(request: Request, site: DocSite) -> None:
    """A private site's assistant answers with the reader's cookie: only to a page of this host."""
    origin = request.headers.get("origin")
    if not site.public and origin and _netloc(origin) not in _hosts(request.headers):
        raise ServiceError("Not allowed", 403)


@router.post("/{slug}/assistant")
async def ask_assistant(slug: str, body: AssistantIn, request: Request, session: Session, runs: Runs) -> StreamingResponse:
    _limit(ASSISTANT, request)
    found = await _site(session, request, slug)
    _same_site(request, found)
    language = public.lang_of(found, body.lang)
    ver = await _version(session, request, found, body.release)
    chat, spec, ctx = await runs.docs_assistant.start(session, found, _client(request), body.text, body.conversation, language, ver.base, ver.rel)
    max_tokens = found.assistant_max_tokens
    await session.close()  # the answer takes a while: no database connection is held for it
    headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    return StreamingResponse(runs.docs_assistant.stream(chat, spec, ctx, max_tokens), media_type="application/x-ndjson", headers=headers)


@router.delete("/{slug}/assistant/{conversation}", status_code=204)
async def close_assistant(slug: str, conversation: str, request: Request, session: Session, runs: Runs) -> None:
    found = await _site(session, request, slug)
    runs.docs_assistant.close(conversation, found.id, _client(request))


# --- HTML ------------------------------------------------------------------------------------------------------


def _template() -> str | None:
    path = get_config().static_dir / "docs.html"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _origin(request: Request) -> str:
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip() or request.headers.get("host") or next(iter(sorted(_hosts(request.headers))), "localhost")
    return f"{'https' if is_https(request) else 'http'}://{host}"


@pages.get("/docs/{slug}", include_in_schema=False)
async def site_root(slug: str) -> Response:
    return RedirectResponse(f"/docs/{slug}/", status_code=308)


@pages.get("/docs/{slug}/{path:path}", include_in_schema=False)
async def html_page(slug: str, path: str, request: Request, session: Session) -> Response:
    template = _template()
    try:
        _limit(PAGES, request)
        found = await _site(session, request, slug)
    except ServiceError as exc:
        if exc.status_code == 401:
            site_ = await public.served(session, slug)
            return HTMLResponse(public.sign_in_page(template, site_), status_code=401, headers={"Cache-Control": "no-store"})
        return PlainTextResponse(str(exc), status_code=exc.status_code)
    base = _base(request, found)
    site_url = _origin(request) + base
    if path == "robots.txt":
        rules = "Allow: /" if found.public else "Disallow: /"
        return PlainTextResponse(f"User-agent: *\n{rules}\nSitemap: {site_url}/sitemap.xml\n")
    ver = await public.version(session, found, None, base)
    if path == "sitemap.xml":
        if not found.public:
            return PlainTextResponse("Not found", status_code=404)
        return Response(await public.sitemap(session, found, site_url, ver.default), media_type="application/xml", headers={"Cache-Control": "public, max-age=600"})
    first, _, rest = path.partition("/")
    asked = next((r for r in ver.releases if r.slug == first and (not r.hidden or r.id == ver.default.id)), None)
    if asked is not None:  # a version of the site (#126): its address comes before the language
        ver = public.Version(asked, ver.default, ver.releases, base)
        path = rest
        first, _, rest = path.partition("/")
    lang = found.language
    if first in (found.languages or []):
        lang, path = first, rest
    cache = {"Cache-Control": "public, max-age=60" if found.public else "private, no-store"}
    key = (found.id, found.updated_at.isoformat(), base, "html", ver.rel.id, lang, path.strip("/"), _origin(request))
    grant = public.preview_grant(request.cookies.get(public.PREVIEW_COOKIE))
    draft = grant.theme if grant is not None and grant.site_id == found.id else None  # a theme not saved yet (#128)
    if draft is not None:
        cache, key = {"Cache-Control": "no-store"}, (*key, id(grant))
    elif (done := public.CACHE.get(key)) is not None:
        return HTMLResponse(done, headers=cache)
    view = jsonable_encoder(await public.site_view(session, found, ver.base, lang, ver))
    if draft is not None:
        view["theme"] = docs_theme.public(draft, f"{public.api_base(found)}/files")
    here = public.lang_base(found, ver.base, lang)
    theme = public.theme_head(found, draft)
    custom = None
    if (m := path.strip("/")).startswith("p/") and "/" not in m[2:] and any(f["path"] == m for f in view["footer"]):  # a page of its own (#121)
        custom = jsonable_encoder(await docs_custom.view(session, found, m[2:], ver.base, lang, ver.rel))
    try:
        page_view = jsonable_encoder(await public.page_view(session, found, path, ver.base, lang, ver.rel)) if view["nav"]["items"] and custom is None else None
    except NotFound:
        return HTMLResponse(public.document(template, {"site": view, "page": None}, public.head_html(view, None, site_url + "/", {}), public.body_html(view, None), lang, theme), status_code=404)
    shown = custom or page_view
    page_path = shown["path"] if shown and path.strip("/") else ""
    canonical = f"{_origin(request)}{here}/{page_path}"
    version_url = _origin(request) + ver.base
    alternates = {code: f"{version_url}{'' if code == found.language else '/' + code}/{page_path}" for code in view["languages"]}
    if len(alternates) > 1:
        alternates["x-default"] = alternates[found.language]
    state = {"site": view, "page": page_view, "custom": custom}
    out = public.document(template, state, public.head_html(view, shown, canonical, alternates), public.body_html(view, shown), lang, theme)
    return HTMLResponse(out if draft is not None else public.CACHE.put(key, out), headers=cache)


# --- a site's own domain ---------------------------------------------------------------------------------------


class DocHost:
    """A request to a doc site's own domain: that site, its files, the frontend's assets and the sign-in, nothing else."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        found = None
        try:
            for host in _hosts(Headers(scope=scope)):
                if found := await docs_domains.lookup(host.split(":", 1)[0]):
                    break
        except Exception:  # no database yet (starting): PlainBoard as always
            log.debug("Doc domains not readable", exc_info=True)
        if found is None:
            await self.app(scope, receive, send)
            return
        _, slug, token, served = found
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4404})
            return
        path = scope["path"]
        if path == docs_domains.WELL_KNOWN:
            await PlainTextResponse(token)(scope, receive, send)
            return
        if not served:
            await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
            return
        state = {**scope.get("state", {}), DOMAIN_SLUG: slug}
        own_api = f"/api/public/docs/{slug}"
        if path == own_api or path.startswith(own_api + "/") or path.startswith("/assets/") or path in DOMAIN_PATHS:
            await self.app({**scope, "state": state}, receive, send)
            return
        rewritten = f"/docs/{slug}{path}"
        await self.app({**scope, "path": rewritten, "raw_path": rewritten.encode(), "state": state}, receive, send)
