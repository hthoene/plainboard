"""What a served doc site shows (#118): pages rendered on the server, the navigation piece by piece, the search.

Markdown is rendered with markdown-it (CommonMark, tables, strikethrough), raw HTML off: whatever an agent writes,
no script reaches the site. ``doc-file:<id>`` and ``doc-page:<id>`` become the file's and the page's address, every
heading gets an id for the table of contents. A picture whose address ends in ``#light`` or ``#dark`` (or GitHub's
``#gh-light-mode-only``/``#gh-dark-mode-only``) shows only in that mode: ``data-mode`` on the ``img``, hidden by CSS (#125). Only pages of a served site, only its own files; a private site only for
people with a role in its project (``may_read``).

Scales with the site: nothing reads all its pages. The navigation comes as the top level and, per page, the children
of the way down to it (each level at most ``docs.PAGE_LIMIT``, more with ``nav``), the search reads at most
``CANDIDATES`` pages found by the database. Rendered views are cached in this process, keyed by the site's stamp
(``DocSite.updated_at``, moved by every change), so a change is visible at once and a busy site renders each page once.

Languages: the site's own language at /docs/<slug>/<path>, each further one at /docs/<slug>/<lang>/<path>; a page
without its translation yet shows the original (``fallback``).
"""

from __future__ import annotations

import html
import json
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from markdown_it import MarkdownIt
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DocPage, DocRelease, DocSite, DocTranslation, Project
from . import docs as docs_service
from . import docs_areas, docs_custom, docs_theme
from .errors import NotFound, ServiceError
from .util import slugify

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(["table", "strikethrough"])
_WORDS = re.compile(r"[^\W_]+")
_MODE = re.compile(r"#(?:gh-)?(light|dark)(?:-mode-only)?$")
CANDIDATES = 200
SITEMAP_LIMIT = 50000


@dataclass
class Rendered:
    html: str
    toc: list[dict]  # {level, id, text}
    text: str  # plain text: the description, the search


def _url(base: str, path: str) -> str:
    return f"{base}/{path}" if path else f"{base}/"


def render(body: str, *, base: str, files_base: str, page_paths: dict[int, str]) -> Rendered:
    """``base``: the site's address in this language ("" on its own domain, /docs/<slug> here); ``files_base``: where its files are."""
    tokens = _md.parse(body or "")
    toc: list[dict] = []
    used: set[str] = set()
    words: list[str] = []
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and i + 1 < len(tokens):
            text = tokens[i + 1].content
            anchor, n = slugify(text, "section"), 2
            while anchor in used:
                anchor, n = f"{slugify(text, 'section')}-{n}", n + 1
            used.add(anchor)
            token.attrSet("id", anchor)
            toc.append({"level": int(token.tag[1]), "id": anchor, "text": text})
        if token.children:
            token.children = _unlink(token.children, page_paths)
        for child in token.children or []:
            src = child.attrGet("src") if child.type == "image" else None
            if isinstance(src, str) and (mode := _MODE.search(src)):
                child.attrSet("src", src[: mode.start()])
                child.attrSet("data-mode", mode.group(1))
            for attr in ("href", "src"):
                value = child.attrGet(attr)
                if isinstance(value, str):
                    child.attrSet(attr, _resolve(value, base, files_base, page_paths))
            if child.type in ("text", "code_inline"):
                words.append(child.content)
        if token.type == "inline":
            words.append("\n")
    return Rendered(_md.renderer.render(tokens, _md.options, {}), toc, re.sub(r"\s+\n", "\n", " ".join(words)).strip())


def _unlink(children: list, page_paths: dict[int, str]) -> list:
    """A link to a page that is not there (deleted, of another site, not in this version) stays as its text, no dead link."""
    out, dropping = [], False
    for child in children:
        if child.type == "link_open" and (m := re.fullmatch(r"doc-page:(\d+)(#[\w-]+)?", str(child.attrGet("href") or ""))) and int(m.group(1)) not in page_paths:
            dropping = True
            continue
        if dropping and child.type == "link_close":
            dropping = False
            continue
        out.append(child)
    return out


def _resolve(value: str, base: str, files_base: str, page_paths: dict[int, str]) -> str:
    if m := re.fullmatch(r"doc-file:(\d+)", value):
        return f"{files_base}/{m.group(1)}"
    if m := re.fullmatch(r"doc-page:(\d+)(#[\w-]+)?", value):
        path = page_paths.get(int(m.group(1)))
        return _url(base, path) + (m.group(2) or "") if path is not None else "#"
    return value


async def linked_paths(session: AsyncSession, site_id: int, *bodies: str, rel: DocRelease | None = None) -> dict[int, str]:
    """The addresses of the pages these texts link to (doc-page:<id>) in this version, in one query."""
    ids = {int(m) for body in bodies for m in docs_service.PAGE_REF.findall(body or "")}
    if not ids:
        return {}
    q = select(DocPage.page_key, DocPage.path).where(DocPage.site_id == site_id, DocPage.page_key.in_(ids), docs_service.visible(rel))
    return dict((await session.execute(q)).all())


def describe(text: str, size: int = 160) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= size else flat[: size - 1].rsplit(" ", 1)[0] + "…"


# --- who may read ---------------------------------------------------------------------------------------------


async def served(session: AsyncSession, slug: str) -> DocSite:
    site = await session.scalar(select(DocSite).where(DocSite.slug == slug))
    if site is None or not site.published:
        raise NotFound("Not found")
    return site


async def may_read(session: AsyncSession, site: DocSite, cookie: str | None, authorization: str | None) -> None:
    """A public site: everyone. A private one: people with a role in its project (and admins, read keys); else 401
    (sign in) or, for someone signed in without a part in it, 404."""
    if site.public:
        return
    from . import access
    from . import users as user_service

    who = await access.principal(session, cookie, authorization)
    if who is None:
        raise ServiceError("Sign in to read this documentation.", 401)
    if who.read_key or who.is_admin:
        return
    if who.user is None or await user_service.role_in(session, who.user, site.project_id) is None:
        raise NotFound("Not found")


# --- languages and titles -------------------------------------------------------------------------------------


def lang_of(site: DocSite, lang: str | None) -> str:
    if not lang or lang == site.language:
        return site.language
    if lang not in (site.languages or []):
        raise NotFound("Not found")
    return lang


def lang_base(site: DocSite, base: str, lang: str) -> str:
    return base if lang == site.language else f"{base}/{lang}"


async def titles(session: AsyncSession, site: DocSite, ids: list[int], lang: str) -> dict[int, str]:
    if lang == site.language or not ids:
        return {}
    rows = await session.execute(select(DocTranslation.page_id, DocTranslation.title).where(DocTranslation.page_id.in_(ids), DocTranslation.lang == lang, DocTranslation.title != ""))
    return dict(rows.all())


async def nav(
    session: AsyncSession, site: DocSite, parent_id: int | None, lang: str, offset: int = 0, limit: int = docs_service.PAGE_LIMIT, rel: DocRelease | None = None
) -> dict:
    """{items: [{id, title, path, children}], total, offset}: one page of a page's children (None: the top)."""
    rows, counts, total = await docs_service.children(session, site.id, parent_id, offset, limit, rel)
    named = await titles(session, site, [p.id for p in rows], lang)
    items = [{"id": p.page_key, "title": named.get(p.id, p.title), "path": p.path, "children": counts.get(p.page_key, 0)} for p in rows]
    if parent_id is None:  # the top: each page's area and group (#159)
        for item, p in zip(items, rows, strict=True):
            item["area"], item["group"] = p.area_id, p.group_id
    return {"items": items, "total": total, "offset": offset}


def api_base(site: DocSite) -> str:
    return f"/api/public/docs/{site.slug}"


@dataclass
class Version:
    """The version a visitor reads (#126, services/docs_releases.py) and where it lives: ``base`` is the site's
    address with the version's segment (none for the default one), ``site_base`` without it."""

    rel: DocRelease
    default: DocRelease
    releases: list[DocRelease]
    site_base: str

    @property
    def base(self) -> str:
        return self.site_base if self.rel.id == self.default.id else f"{self.site_base}/{self.rel.slug}"

    def view(self) -> dict:
        from . import docs_releases

        r = self.rel
        if not self.releases:  # a site without versions
            return {"versioned": False, "version": None, "versions": [], "site_base": self.site_base}
        return {
            "versioned": True,
            "version": {"slug": r.slug, "name": r.name, "working": r.working, "deprecated": r.deprecated, "default": r.id == self.default.id, "released_on": r.released_on, "notes": r.notes},
            "versions": docs_releases.public_list(self.releases, self.default), "site_base": self.site_base,
        }  # fmt: skip


async def version(session: AsyncSession, site: DocSite, slug: str | None, site_base: str) -> Version:
    """The version a visitor asked for (none: the default one); a hidden or unknown one is 404."""
    from . import docs_releases

    rel, default, rels = await docs_releases.served(session, site, slug)
    return Version(rel, default, rels, site_base)


async def site_view(session: AsyncSession, site: DocSite, base: str, lang: str, ver: Version | None = None) -> dict:
    """``base``: the version's address (``Version.base``)."""
    rel = ver.rel if ver else None
    top = await nav(session, site, None, lang, rel=rel)
    project = await session.get(Project, site.project_id)
    first = top["items"][0]["path"] if top["items"] else None
    return {
        **(ver.view() if ver else {}),
        "name": site.name, "slug": site.slug, "description": site.description, "public": site.public, "base": base, "api": api_base(site),
        "language": site.language, "languages": [site.language, *(site.languages or [])], "lang": lang, "lang_base": lang_base(site, base, lang),
        "project": project.name if project else "", "updated_at": site.updated_at, "home": first, "nav": top,
        "assistant": site.assistant_enabled, "theme": docs_theme.public(site.theme or {}, f"{api_base(site)}/files"),
        "footer": await docs_custom.footer(session, site, lang),
        **dict(zip(("areas", "groups"), docs_areas.served(await docs_areas.areas(session, site.id, rel), await docs_areas.groups(session, site.id, rel), lang), strict=True)),
    }  # fmt: skip


async def page_view(session: AsyncSession, site: DocSite, path: str, base: str, lang: str, rel: DocRelease | None = None) -> dict:
    """A page by its address ("" : the home page) in a version (``base``: its address). ``sidebar``: the top level and
    the children of every page on the way down to it (each with ``children`` counts), what a navigation shows opened
    at this page."""
    page = await docs_service.by_path(session, site.id, path, rel) or await docs_service.moved(session, site.id, path, rel)
    if page is None:
        raise NotFound("Not found")
    title, body, fallback, translated = page.title, page.body, False, None
    if lang != site.language:
        translated = await session.scalar(select(DocTranslation).where(DocTranslation.page_id == page.id, DocTranslation.lang == lang))
        if translated is not None and translated.translated_at:
            title, body = translated.title or page.title, translated.body
        else:
            fallback = True
    here = lang_base(site, base, lang)
    rendered = render(body, base=here, files_base=f"{api_base(site)}/files", page_paths=await linked_paths(session, site.id, body, rel=rel))
    up = await docs_service.ancestors(session, page, rel)
    prev, nxt = await docs_service.neighbours(session, page, rel, up)
    named = await titles(session, site, [p.id for p in [*up, *(x for x in (prev, nxt) if x)]], lang)
    near = lambda p: {"title": named.get(p.id, p.title), "path": p.path} if p else None  # noqa: E731
    parents = [None, *(p.page_key for p in up), page.page_key]
    opened = await docs_service.levels(session, site.id, parents, rel)
    listed = [p for rows, _ in opened.values() for p in rows]
    counts = await docs_service.child_counts(session, [p.page_key for p in listed], rel)
    named |= await titles(session, site, [p.id for p in listed], lang)
    def entry(p: DocPage) -> dict:
        out = {"id": p.page_key, "title": named.get(p.id, p.title), "path": p.path, "children": counts.get(p.page_key, 0)}
        return out | {"area": p.area_id, "group": p.group_id} if p.parent_id is None else out  # the top: its area and group (#159)

    sidebar = [{"items": [entry(p) for p in opened[parent][0]], "total": opened[parent][1], "offset": 0} for parent in parents[:-1]]
    if page.page_key not in {i["id"] for level in sidebar for i in level["items"]}:  # far down a long list: its own place in it
        sidebar[-1]["items"].append(entry(page))
    kids, total = opened[page.page_key]
    return {
        "id": page.page_key, "title": title, "path": page.path, "lang": lang, "fallback": fallback, "html": rendered.html, "toc": rendered.toc, "markdown": body,
        "description": describe(rendered.text) or site.description, "updated_at": translated.translated_at if translated and not fallback else page.edited_at,
        "breadcrumbs": [near(p) for p in up], "prev": near(prev), "next": near(nxt), "sidebar": sidebar,
        "children": {"items": [entry(k) for k in kids], "total": total, "offset": 0},
    }  # fmt: skip


# --- search ---------------------------------------------------------------------------------------------------


def _terms(query: str) -> list[str]:
    from .search.store import STOPWORDS

    return [w for w in dict.fromkeys(_WORDS.findall(query.lower())) if len(w) >= 2 and w not in STOPWORDS][:8]


def _snippet(text: str, terms: list[str], size: int = 200) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    lowered = flat.lower()
    at = min((i for t in terms if (i := lowered.find(t)) >= 0), default=0)
    start = max(0, at - size // 3)
    return ("…" if start else "") + flat[start : start + size].strip() + ("…" if start + size < len(flat) else "")


async def search(session: AsyncSession, site: DocSite, query: str, base: str, lang: str, limit: int = 10, rel: DocRelease | None = None) -> list[dict]:
    return (await search_hits(session, site, query, base, lang, limit, rel=rel))[0]


async def search_hits(
    session: AsyncSession, site: DocSite, query: str, base: str, lang: str, limit: int = 10, *, budget: bool = True, rel: DocRelease | None = None
) -> tuple[list[dict], bool]:  # fmt: skip
    """By words in the pages of this language (title, address and text; the database finds at most ``CANDIDATES``,
    those with all the words first; title words count three times) and, where semantic search runs, by meaning over
    the site's pages; fused by rank. Each hit: id, title, path, url, snippet. Also whether the site's vector searches
    were used up (``VECTOR_SEARCHES``), so it searched by words only. ``budget=False`` (the site's own doc agent)
    neither counts against nor is limited by the visitors' budget. Only pages of the version ``rel`` (None: the working
    one); a hit's id is the page's key."""
    terms = _terms(query) or [query.lower().strip()[:50]]
    if lang == site.language:
        q = select(DocPage.id, DocPage.title, DocPage.body, DocPage.path, DocPage.page_key).where(DocPage.site_id == site.id, docs_service.visible(rel))
        title_col, body_col = DocPage.title, DocPage.body
    else:
        q = select(DocTranslation.page_id, DocTranslation.title, DocTranslation.body, DocPage.path, DocPage.page_key).join(DocPage, DocPage.id == DocTranslation.page_id)
        q = q.where(DocTranslation.site_id == site.id, DocTranslation.lang == lang, docs_service.visible(rel))
        title_col, body_col = DocTranslation.title, DocTranslation.body
    has = [or_(func.lower(title_col).like(f"%{t}%"), func.lower(body_col).like(f"%{t}%"), DocPage.path.like(f"%{t}%")) for t in terms]
    order = (DocPage.depth, DocPage.position, DocPage.id)
    found = {row[0]: row for row in (await session.execute(q.where(*has).order_by(*order).limit(CANDIDATES))).all()}
    if len(terms) > 1 and len(found) < CANDIDATES:  # then those with some of the words
        rest = q.where(or_(*has)).order_by(*order).limit(CANDIDATES)
        for row in (await session.execute(rest)).all():
            if len(found) >= CANDIDATES:
                break
            found.setdefault(row[0], row)
    scored = []
    for pid, title, body, path, _ in found.values():
        t, b = title.lower(), (body or "").lower()
        scored.append((sum(3 * t.count(w) + min(b.count(w), 10) + (2 if w in path else 0) + 5 * (w in t or w in b or w in path) for w in terms), pid))
    by_words = [pid for _, pid in sorted(scored, key=lambda x: -x[0])]
    by_meaning = await _by_meaning(session, site, query, budget=budget, rel=rel)
    words_only = by_meaning is None
    by_meaning = by_meaning or []
    fused: dict[int, float] = {}
    for ranking in (by_words, by_meaning):
        for rank, pid in enumerate(ranking):
            fused[pid] = fused.get(pid, 0.0) + 1.0 / (60 + rank)
    best = sorted(fused, key=lambda pid: -fused[pid])[:limit]
    missing = [pid for pid in best if pid not in found]
    if missing:  # found by meaning only: their text in this language
        for row in (await session.execute(select(DocPage.id, DocPage.title, DocPage.body, DocPage.path, DocPage.page_key).where(DocPage.id.in_(missing)))).all():
            found[row[0]] = row
        if lang != site.language:
            rows = await session.execute(select(DocTranslation.page_id, DocTranslation.title, DocTranslation.body).where(DocTranslation.page_id.in_(missing), DocTranslation.lang == lang))
            for pid, title, body in rows.all():
                if body:
                    found[pid] = (pid, title, body, found[pid][3], found[pid][4])
    here = lang_base(site, base, lang)
    out = []
    for pid in best:
        if pid not in found:
            continue
        _, title, body, path, key = found[pid]
        out.append({"id": key, "title": title, "path": path, "url": _url(here, path), "snippet": _snippet(render(body, base=here, files_base="", page_paths={}).text, terms)})
    return out, words_only


async def _by_meaning(session: AsyncSession, site: DocSite, query: str, *, budget: bool = True, rel: DocRelease | None = None) -> list[int] | None:
    """The site's pages nearest in meaning (the index holds the pages in the site's own language; the models are
    multilingual, so a query in another language finds them too). None when the site's vector searches of this
    minute are used up (public search and the doc assistant together): then words alone must do."""
    from . import search as search_service
    from .search import store

    project = await session.get(Project, site.project_id)
    if project is None or not await search_service.enabled(session, project):
        return []
    model = await search_service.embedder(session)
    if model.state != "ready":
        return []
    if budget and not VECTOR_SEARCHES.allow(str(site.id)):
        return None
    vector = await model.query(query)
    hits = await store.nearest_of_kind(session, project.id, vector, model.name, "doc", 30)
    ids = [int(h.source_id) for h in hits if h.source_id.isdigit()]
    if not ids:
        return []
    mine = set(await session.scalars(select(DocPage.id).where(DocPage.id.in_(ids), DocPage.site_id == site.id, docs_service.visible(rel))))
    return [i for i in ids if i in mine][:10]


# --- for search engines ---------------------------------------------------------------------------------------


async def sitemap(session: AsyncSession, site: DocSite, site_url: str, rel: DocRelease | None = None) -> str:
    """The default version's pages (``rel``)."""
    q = select(DocPage.path).where(DocPage.site_id == site.id, docs_service.visible(rel)).order_by(DocPage.depth, DocPage.position).limit(SITEMAP_LIMIT)
    paths = list(await session.scalars(q))
    urls = [f"{site_url}/"] + [f"{site_url}/{p}" for p in paths]
    for lang in site.languages or []:
        urls += [f"{site_url}/{lang}/"] + [f"{site_url}/{lang}/{p}" for p in paths]
    items = "".join(f"<url><loc>{html.escape(u)}</loc></url>" for u in urls[:SITEMAP_LIMIT])
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'


def _nav_html(sidebar: list[dict], base: str, current: str) -> str:
    """The top level, opened along the way to the current page."""
    if not sidebar:
        return ""
    levels = {}
    for level in sidebar[1:]:
        if level["items"]:
            levels[level["items"][0]["path"].rsplit("/", 1)[0]] = level["items"]

    def ul(items: list[dict]) -> str:
        out = []
        for n in items:
            mark = ' aria-current="page"' if n["path"] == current else ""
            below = ul(levels[n["path"]]) if n["path"] in levels else ""
            out.append(f'<li><a href="{html.escape(_url(base, n["path"]))}"{mark}>{html.escape(n["title"])}</a>{below}</li>')
        return f"<ul>{''.join(out)}</ul>"

    return ul(sidebar[0]["items"])


def head_html(site_view_: dict, page: dict | None, canonical: str, alternates: dict[str, str]) -> str:
    title = f"{page['title']} – {site_view_['name']}" if page else site_view_["name"]
    description = (page or {}).get("description") or site_view_["description"]
    e = html.escape
    links = "".join(f'<link rel="alternate" hreflang="{e(lang)}" href="{e(url)}">' for lang, url in alternates.items()) if len(alternates) > 1 else ""
    robots = "" if site_view_["public"] else '<meta name="robots" content="noindex">'
    return (
        f"<title>{e(title)}</title>"
        f'<meta name="description" content="{e(description)}">{robots}'
        f'<link rel="canonical" href="{e(canonical)}">{links}'
        f'<meta property="og:type" content="article"><meta property="og:title" content="{e(title)}">'
        f'<meta property="og:description" content="{e(description)}"><meta property="og:url" content="{e(canonical)}">'
        f'<meta property="og:site_name" content="{e(site_view_["name"])}">'
    )


def body_html(site_view_: dict, page: dict | None) -> str:
    """The page as plain HTML: what a search engine reads, and what shows before (or without) the site's script."""
    e, base = html.escape, site_view_["lang_base"]
    article = (
        f'<article><h1>{e(page["title"])}</h1>{page["html"]}</article>'
        if page
        else f"<article><h1>{e(site_view_['name'])}</h1><p>{e(site_view_['description'])}</p></article>"
    )
    sidebar = page.get("sidebar") or [site_view_["nav"]] if page else [site_view_["nav"]]
    footer = "".join(f'<a href="{e(_url(base, f["path"]))}">{e(f["title"])}</a> ' for f in site_view_.get("footer") or [])
    footer = f"<footer>{footer}</footer>" if footer else ""
    return f'<nav aria-label="{e(site_view_["name"])}"><a href="{e(_url(base, ""))}">{e(site_view_["name"])}</a>{_nav_html(sidebar, base, (page or {}).get("path", ""))}</nav><main>{article}{footer}</main>'


SIGN_IN = """<article><h1>{name}</h1><p>This documentation is private. Sign in with your PlainBoard account.</p>
<form method="post" onsubmit="event.preventDefault();fetch('/api/auth/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{username:this.u.value,password:this.p.value}})}}).then(r=>r.ok?location.reload():alert('Sign-in failed'))">
<p><input name="u" autocomplete="username" placeholder="Username" required> <input name="p" type="password" autocomplete="current-password" placeholder="Password" required> <button>Sign in</button></p></form></article>"""

FALLBACK = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<!--pb-docs-head--><style>body{font:16px/1.6 system-ui,sans-serif;margin:0;display:flex;gap:2rem;color:#1f2328}nav{min-width:15rem;padding:1.5rem;border-right:1px solid #ddd}
nav ul{list-style:none;padding-left:1rem}main{max-width:48rem;padding:1.5rem}pre{background:#f6f8fa;padding:1rem;overflow:auto}img{max-width:100%}
table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:.3rem .6rem}a[aria-current]{font-weight:600}
@media (prefers-color-scheme:dark){img[data-mode=light]{display:none}}@media not (prefers-color-scheme:dark){img[data-mode=dark]{display:none}}</style></head>
<body><div id="docs-root"><!--pb-docs-body--></div></body></html>"""


def document(template: str | None, state: dict, head: str, body: str, lang: str = "en", theme: str = "") -> str:
    """The HTML of a page: the frontend's ``docs.html`` (or a plain one without a build) with the page's head, the
    rendered page in its body and the state for the script (``window.__PB_DOCS__``); ``theme``: the site's own look
    (services/docs_theme.head), at the end of the head, after the site's own styles."""
    data = json.dumps(state, default=str, ensure_ascii=False).replace("<", "\\u003c")
    head += f"<script>window.__PB_DOCS__={data}</script>"
    out = template or FALLBACK
    out = out.replace('<html lang="en">', f'<html lang="{html.escape(lang)}">', 1)
    if theme:
        if 'rel="icon"' in theme:
            out = re.sub(r'<link rel="icon" href="[^"]*"\s*/?>', "", out, count=1)  # its href is an SVG with ">" in it
        out = out.replace("</head>", theme + "</head>", 1)
    out = out.replace("<!--pb-docs-head-->", head, 1) if "<!--pb-docs-head-->" in out else out.replace("</head>", head + "</head>", 1)
    return out.replace("<!--pb-docs-body-->", body, 1)


def theme_head(site: DocSite, theme: dict | None = None) -> str:
    return docs_theme.head(site.theme or {} if theme is None else theme, f"{api_base(site)}/files", site.name)


# --- previews for the agents' screenshots (#128) --------------------------------------------------------------
# The preview tool's browser carries a short-lived grant as a cookie: it may read that one site even unpublished or
# private, and with a theme not saved yet. Only the app makes grants (tools/docs_theme.preview_doc_site); kept in
# this process, as the app runs in one.

PREVIEW_COOKIE = "pb_doc_preview"
PREVIEW_SECONDS = 300


@dataclass
class PreviewGrant:
    site_id: int
    theme: dict | None  # None: the site's own
    until: float


_grants: OrderedDict[str, PreviewGrant] = OrderedDict()


def grant_preview(site_id: int, theme: dict | None) -> str:
    import secrets

    now = time.monotonic()
    for token in [t for t, g in _grants.items() if g.until < now]:
        del _grants[token]
    while len(_grants) >= 200:
        _grants.popitem(last=False)
    token = secrets.token_urlsafe(24)
    _grants[token] = PreviewGrant(site_id, theme, now + PREVIEW_SECONDS)
    return token


def preview_grant(token: str | None) -> PreviewGrant | None:
    grant = _grants.get(token or "")
    return grant if grant is not None and grant.until >= time.monotonic() else None


def sign_in_page(template: str | None, site: DocSite) -> str:
    state = {"site": {"name": site.name, "slug": site.slug, "public": False}, "page": None, "sign_in": True}
    head = f'<title>{html.escape(site.name)}</title><meta name="robots" content="noindex">'
    return document(template, state, head, SIGN_IN.format(name=html.escape(site.name)), site.language, theme_head(site))


# --- cache and limits -----------------------------------------------------------------------------------------


class Cache:
    """Rendered views by (site, its stamp, what): a change moves the stamp, so nothing stale is ever served."""

    def __init__(self, size: int = 2000) -> None:
        self.size = size
        self._items: OrderedDict[tuple, object] = OrderedDict()

    def get(self, key: tuple) -> object | None:
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
        return None

    def put(self, key: tuple, value: object) -> object:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)
        return value


CACHE = Cache()


class RateLimit:
    """Requests per client address in a sliding window, in this process."""

    def __init__(self, rate: int, window: float = 60.0) -> None:
        self.rate, self.window = rate, window
        self._seen: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if len(self._seen) > 10000:  # many addresses: forget the quiet ones
            self._seen = {k: v for k, v in self._seen.items() if v and v[-1] > now - self.window}
        seen = self._seen.setdefault(key, deque())
        while seen and seen[0] <= now - self.window:
            seen.popleft()
        if len(seen) >= self.rate:
            return False
        seen.append(now)
        return True


# Embedding a query costs the server real work: per doc site at most this many vector searches a minute, for all its
# visitors and its assistant together (one process: the app runs a single uvicorn worker).
VECTOR_SEARCHES_PER_MINUTE = 15
VECTOR_SEARCHES = RateLimit(VECTOR_SEARCHES_PER_MINUTE)
