"""Doc sites (#118): documentation that lives in PlainBoard, written by people and agents, kept current by agents.

A project has any number of sites. A site has a doc spec (what the user wants in it: content, structure, tone,
audience; written by the manager with the user, followed by every agent that writes the site) and pages in a tree.
Every saved state of a page is kept as a version (who, when, why) and can be restored. Files of a site are
attachments with ``doc_site_id``: the retention (services/retention.py) only ever touches files of done tickets and
the cleanup only workspaces, so they stay until their site goes or someone deletes them.

Built for sites of thousands of pages in deep trees: every page keeps its address (``path``) and ``depth``, so a page
is found by its address with one indexed query, and nothing here reads a whole site: children come page by page
(``children``), the way to a page from its address prefixes, its neighbours along the tree with a few small queries.
Every change moves ``DocSite.updated_at``: the stamp the public site's cache is keyed by.

In a page, ``doc-file:<attachment id>`` names a file of the site and ``doc-page:<page id>`` another page
(``![Diagram](doc-file:12)``, ``[Setup](doc-page:3)``): links that survive moves, renames and the move of a project;
the renderer (services/docs_public.py) turns them into addresses. Sites are draft, private (people with a role in the
project) or public (api/docs_public.py), at /docs/<slug>/ and, once verified, at their own domain
(services/docs_domains.py); translations into further languages: services/docs_translate.py. What keeps pages current
is orchestration/docs_watcher.py.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import BinaryIO

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Agent, Attachment, DocCustomPage, DocPage, DocPageVersion, DocSite, DocTranslation, utcnow
from ..schemas import AgentCreate, AgentUpdate, AttachmentOut, DocAgentOut, DocPageItem, DocSiteOut, DocTheme
from . import board as board_service
from .errors import NotFound, ServiceError
from .util import slugify

DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
LANGUAGE = re.compile(r"^[a-z]{2,3}(-[a-zA-Z0-9]{2,8})?$")
FILE_REF = re.compile(r"doc-file:(\d+)")
PAGE_REF = re.compile(r"doc-page:(\d+)")
PAGE_LIMIT = 200  # children of one page at a time
MAX_DEPTH = 50


def source_hash(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:16]


# --- sites ----------------------------------------------------------------------------------------------------


async def unique_site_slug(session: AsyncSession, name: str, exclude_id: int | None = None) -> str:
    base = slugify(name, "docs")[:70]
    slug, n = base, 2
    while (found := await session.scalar(select(DocSite.id).where(DocSite.slug == slug))) is not None and found != exclude_id:
        slug, n = f"{base}-{n}", n + 1
    return slug


def clean_domain(value: str) -> str:
    """A host name as typed (also as a URL) in lower case, or an error."""
    host = value.strip().lower()
    host = re.sub(r"^[a-z]+://", "", host).split("/", 1)[0].split(":", 1)[0].rstrip(".")
    if not DOMAIN.match(host) or host.endswith((".local", ".localhost", ".internal")):
        raise ServiceError("Not a domain name: give a host name like docs.example.com.")
    return host


def clean_languages(values: list[str], language: str) -> list[str]:
    out = []
    for value in values:
        code = value.strip()
        if not LANGUAGE.match(code):
            raise ServiceError(f"Not a language code: {value!r} (e.g. de, fr, pt-BR).")
        if code != language and code not in out:
            out.append(code)
    return out[:30]


def site_out(site: DocSite, pages: int = 0, stale: int = 0, agent: Agent | None = None) -> DocSiteOut:
    return DocSiteOut(
        id=site.id, project_id=site.project_id, name=site.name, slug=site.slug, description=site.description, spec=site.spec,
        published=site.published, public=site.public, language=site.language, languages=list(site.languages or []),
        translator=site.translator, translate_provider=site.translate_provider, translate_model=site.translate_model,
        deepl_key_set=bool(site.deepl_key_enc), google_key_set=bool(site.google_key_enc),
        domain=site.domain, domain_verified_at=site.domain_verified_at,
        cloudflare_token_set=bool(site.cloudflare_token_enc), **{k: getattr(site, k) for k in ASSISTANT_FIELDS}, assistant_warning=ASSISTANT_WARNING, theme=DocTheme(**(site.theme or {})), url=f"/docs/{site.slug}/", pages=pages, stale_pages=stale,
        agent=DocAgentOut(id=agent.id, slug=agent.slug, name=agent.name, provider=agent.provider, model=agent.model, effort=agent.effort, enabled=agent.enabled) if agent else None,
        created_at=site.created_at, updated_at=site.updated_at,
    )  # fmt: skip


async def site_outs(session: AsyncSession, sites: list[DocSite]) -> list[DocSiteOut]:
    if not sites:
        return []
    ids = [s.id for s in sites]
    rows = await session.execute(
        select(DocPage.site_id, func.count(), func.count(DocPage.flagged_at)).where(DocPage.site_id.in_(ids)).group_by(DocPage.site_id)
    )
    counts = {site_id: (int(n), int(stale)) for site_id, n, stale in rows.all()}
    agents = {a.doc_site_id: a for a in await session.scalars(select(Agent).where(Agent.doc_site_id.in_(ids)))}
    return [site_out(s, *counts.get(s.id, (0, 0)), agents.get(s.id)) for s in sites]


async def publish_site(session: AsyncSession, site: DocSite) -> None:
    (out,) = await site_outs(session, [site])
    bus.publish(site.project_id, "doc_site.upserted", out)


async def sites(session: AsyncSession, project_id: int) -> list[DocSite]:
    return list(await session.scalars(select(DocSite).where(DocSite.project_id == project_id).order_by(DocSite.id)))


async def get_site(session: AsyncSession, project_id: int, site_id: int) -> DocSite:
    site = await session.get(DocSite, site_id)
    if site is None or site.project_id != project_id:
        raise NotFound("Doc site not found")
    return site


async def find_site(session: AsyncSession, project_id: int, ref: str) -> DocSite:
    """A site by its id, slug or name (for agents)."""
    ref = ref.strip()
    for site in await sites(session, project_id):
        if ref in (str(site.id), site.slug) or ref.lower() == site.name.lower():
            return site
    names = ", ".join(f"{s.slug} ({s.name})" for s in await sites(session, project_id)) or "none yet"
    raise NotFound(f"No doc site {ref!r}. Sites: {names}")


async def create_site(
    session: AsyncSession, project_id: int, *, name: str, slug: str | None = None, description: str = "", spec: str = "", published: bool = False,
    public: bool = False, language: str = "en", languages: list[str] | None = None, agent_provider: str | None = None, agent_model: str | None = None,
    agent_effort: str | None = None,
) -> DocSite:  # fmt: skip
    """With its doc agent, the one agent that writes it (``ensure_agent``)."""
    if slug and await session.scalar(select(DocSite.id).where(DocSite.slug == slug)) is not None:
        raise ServiceError(f"The address /docs/{slug}/ is taken: choose another slug.", 409)
    if not LANGUAGE.match(language):
        raise ServiceError(f"Not a language code: {language!r}")
    site = DocSite(
        project_id=project_id, name=name.strip(), slug=slug or await unique_site_slug(session, name), description=description.strip(), spec=spec.strip(),
        published=published, public=public, language=language, languages=clean_languages(languages or [], language), domain_token=secrets.token_urlsafe(24),
    )  # fmt: skip
    session.add(site)
    await session.commit()
    await ensure_agent(session, site, provider=agent_provider, model=agent_model, effort=agent_effort)
    await publish_site(session, site)
    return site


# --- the doc agent ---------------------------------------------------------------------------------------------
# Every site has its own agent, hidden from the team and the board: it alone writes the site's pages (the user does
# in the app), so the site reads as one. Its standing instructions are the doc spec; the watcher hands it the pages
# to update after landings, the manager orders it work (tools/docs.py order_docs). It counts into the parallel limit.

AGENT_GUIDE = """You are the doc agent of the doc site "{name}" (slug {slug}, /docs/{slug}/), and the only agent that writes it.
- Read the code and the project's history to document what is real; follow the doc spec below in every page.
- Write pages with write_doc_page (a short reason each time), files with attach_doc_file, link pages with doc-page:ID.
- Pictures for one mode: ![alt](doc-file:ID#light) shows only in light mode, #dark only in dark mode, without one in
  both. For screenshots, where it helps, give both variants (a light and a dark one) side by side.
- Give each page the code paths it describes (paths): changes there after a landing come back to you. On a page you
  touch, check that its paths still point at that code.
- Change little: first check whether a page is really wrong or missing something. Only then change it, and only the
  sentences or sections concerned: no rewording or restructuring without a reason. Otherwise confirm it (still_current).
- Pages PlainBoard names as possibly outdated: fix what the change made wrong, or confirm them (still_current).
- In every update job also look yourself, a few searches (list_docs query, by meaning and words): search the change's
  meaning and its concrete terms (settings, commands, endpoints, UI labels) for other pages that describe what changed,
  also pages nobody marked; change them only where they now describe it wrongly. A new feature without a page: add one
  per the doc spec. Don't read all pages.
- Keep a short overview of your site in your own notes (remember: which page covers what) and update it when you add,
  move or remove pages.
- Keep pages short and find before you write (list_docs query): change a page rather than add a second one.
- You write no code and change no repository.
- A theme (the site's look): doc_theme. Given the product's website, start from theme_from_website. Always look at
  the result with preview_doc_site (a draft before saving): contrast and readability in light and dark, on desktop
  and phone; fix what is off, briefly, then save and report with screenshots (task_number attaches them).

## Doc spec
{spec}"""


def agent_instructions(site: DocSite) -> str:
    return AGENT_GUIDE.format(name=site.name, slug=site.slug, spec=site.spec.strip() or "None yet: the manager agrees it with the user. Write no pages before it is there.")


async def doc_agent(session: AsyncSession, site_id: int) -> Agent | None:
    return await session.scalar(select(Agent).where(Agent.doc_site_id == site_id).limit(1))


async def ensure_agent(session: AsyncSession, site: DocSite, *, provider: str | None = None, model: str | None = None, effort: str | None = None) -> Agent:
    """The site's doc agent, made if it has none; ``provider``, ``model``, ``effort``: None keeps (or takes the defaults)."""
    from . import agents as agent_service

    agent = await doc_agent(session, site.id)
    name = f"Docs {site.name}"[:100]
    if agent is None:
        data = AgentCreate(
            name=name, role="Doc agent", description=f"Writes and keeps current the doc site {site.slug}", instructions=agent_instructions(site),
            provider=provider or None, model=model or None, effort=effort or None,
        )  # fmt: skip
        return await agent_service.create(session, site.project_id, data, doc_site_id=site.id)
    fields: dict = {}
    if agent.instructions != agent_instructions(site):
        fields["instructions"] = agent_instructions(site)
    if agent.name != name:
        fields["name"] = name
    for key, value in (("provider", provider), ("model", model), ("effort", effort)):
        if value is not None and value != (getattr(agent, key) or ""):
            fields[key] = value
    return await agent_service.update(session, agent, AgentUpdate(**fields)) if fields else agent


ASSISTANT_FIELDS = (
    "assistant_enabled", "assistant_provider", "assistant_model", "assistant_effort", "assistant_code", "assistant_daily_messages",
    "assistant_daily_cost", "assistant_max_tokens",
)  # fmt: skip
SITE_FIELDS = ("name", "description", "spec", "published", "public", "translator", "translate_provider", "translate_model", *ASSISTANT_FIELDS)
ASSISTANT_PROVIDERS = ("", "claude_code")  # a provider whose runs can go without any built-in tool (toolset "none")
ASSISTANT_WARNING = (
    "With this on, the assistant reads the project's code and the public texts of its tickets to answer visitors. "
    "Secrets are kept from it (files like .env and keys are never opened, anything shaped like a key or password is "
    "hidden, it is told to refuse internal details), but this is not a guarantee: whatever stands in the code, in a "
    "comment or in a ticket's public text can reach any visitor of the site. Use it only for code you could publish."
)


async def update_site(session: AsyncSession, site: DocSite, *, author: Author | None = None, reason: str = "", **changes) -> DocSite:
    """Name, slug, description, spec, published, public, languages, translator and its model; ``domain``: "" removes
    it, another one has to be verified again. A changed ``theme`` is kept as a version (by ``author``, ``reason``)."""
    slug = changes.pop("slug", None)
    if slug is not None and slug != site.slug:
        if await session.scalar(select(DocSite.id).where(DocSite.slug == slug, DocSite.id != site.id)) is not None:
            raise ServiceError(f"The address /docs/{slug}/ is taken: choose another slug.", 409)
        site.slug = slug
    for key in SITE_FIELDS:
        value = changes.get(key)
        if value is not None:
            setattr(site, key, value.strip() if isinstance(value, str) else value)
    if site.translator not in ("", "llm", "deepl", "google"):
        raise ServiceError("Translator: llm, deepl, google or none.")
    if site.assistant_provider not in ASSISTANT_PROVIDERS:
        raise ServiceError("The assistant runs on Claude only: other providers' runs always have a shell.")
    if changes.get("languages") is not None:
        site.languages = clean_languages(changes["languages"], site.language)
    if changes.get("theme") is not None:
        from . import docs_theme

        theme = await docs_theme.clean(session, site, DocTheme.model_validate(changes["theme"]))
        if theme != (site.theme or {}):
            await docs_theme.record(session, site, theme, author or Author("someone"), reason)
            site.theme = theme
    domain = changes.get("domain")
    if domain is not None:
        new = clean_domain(domain) if domain.strip() else None
        if new != site.domain:
            from . import docs_domains

            if new is not None:
                await docs_domains.check_free(session, new, site.id)
            site.domain, site.domain_verified_at = new, None
            site.domain_token = secrets.token_urlsafe(24)
    site.updated_at = utcnow()
    await session.commit()
    await ensure_agent(session, site, provider=changes.get("agent_provider"), model=changes.get("agent_model"), effort=changes.get("agent_effort"))
    from . import docs_domains

    docs_domains.forget()
    await publish_site(session, site)
    return site


async def delete_site(session: AsyncSession, site: DocSite) -> None:
    """With its pages, their versions and translations, and its files (on disk too)."""
    project_id, site_id = site.project_id, site.id
    for attachment in await files(session, site_id):
        board_service.storage_path(attachment).unlink(missing_ok=True)
    if (agent := await doc_agent(session, site_id)) is not None:
        from . import agents as agent_service

        await agent_service.delete(session, agent)
    await session.delete(site)
    await session.commit()
    from . import docs_domains

    docs_domains.forget()
    bus.publish(project_id, "doc_site.deleted", {"id": site_id})


async def touch(session: AsyncSession, site_id: int) -> None:
    await session.execute(update(DocSite).where(DocSite.id == site_id).values(updated_at=utcnow()))


# --- the tree -------------------------------------------------------------------------------------------------


def _under(parent_id: int | None):
    return DocPage.parent_id.is_(None) if parent_id is None else DocPage.parent_id == parent_id


async def children(session: AsyncSession, site_id: int, parent_id: int | None, offset: int = 0, limit: int = PAGE_LIMIT) -> tuple[list[DocPage], dict[int, int], int]:
    """One page of a page's children (None: the top), with how many children each has, and how many there are."""
    q = select(DocPage).where(DocPage.site_id == site_id, _under(parent_id)).order_by(DocPage.position, DocPage.id)
    rows = list(await session.scalars(q.offset(offset).limit(min(limit, PAGE_LIMIT))))
    total = int(await session.scalar(select(func.count()).select_from(DocPage).where(DocPage.site_id == site_id, _under(parent_id))) or 0)
    return rows, await child_counts(session, [p.id for p in rows]), total


async def levels(session: AsyncSession, site_id: int, parent_ids: list[int | None]) -> dict[int | None, tuple[list[DocPage], int]]:
    """The children of several pages at once (None: the top), at most ``PAGE_LIMIT`` each, with how many there are:
    the navigation opened down to a page, in two queries whatever its depth."""
    if not parent_ids:
        return {}
    ids = [p for p in parent_ids if p is not None]
    where = or_(DocPage.parent_id.in_(ids), DocPage.parent_id.is_(None)) if None in parent_ids else DocPage.parent_id.in_(ids)
    numbered = (
        select(DocPage.id, func.row_number().over(partition_by=DocPage.parent_id, order_by=(DocPage.position, DocPage.id)).label("n"))
        .where(DocPage.site_id == site_id, where)
        .subquery()
    )
    rows = list(await session.scalars(select(DocPage).join(numbered, numbered.c.id == DocPage.id).where(numbered.c.n <= PAGE_LIMIT).order_by(DocPage.position, DocPage.id)))
    totals = dict((await session.execute(select(DocPage.parent_id, func.count()).where(DocPage.site_id == site_id, where).group_by(DocPage.parent_id))).all())
    out: dict[int | None, tuple[list[DocPage], int]] = {p: ([], int(totals.get(p, 0))) for p in parent_ids}
    for page in rows:
        out[page.parent_id][0].append(page)
    return out


async def child_counts(session: AsyncSession, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    rows = await session.execute(select(DocPage.parent_id, func.count()).where(DocPage.parent_id.in_(ids)).group_by(DocPage.parent_id))
    return {pid: int(n) for pid, n in rows.all()}


async def home(session: AsyncSession, site_id: int) -> DocPage | None:
    """The first page at the top: the site's start page."""
    return await session.scalar(select(DocPage).where(DocPage.site_id == site_id, DocPage.parent_id.is_(None)).order_by(DocPage.position, DocPage.id).limit(1))


async def by_path(session: AsyncSession, site_id: int, path: str) -> DocPage | None:
    path = path.strip("/")
    if not path:
        return await home(session, site_id)
    return await session.scalar(select(DocPage).where(DocPage.site_id == site_id, DocPage.path == path).limit(1))


def prefixes(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts) - 1)]


async def ancestors(session: AsyncSession, page: DocPage) -> list[DocPage]:
    """From the top down to its parent: one query by the prefixes of its address."""
    if not page.parent_id:
        return []
    rows = {p.path: p for p in await session.scalars(select(DocPage).where(DocPage.site_id == page.site_id, DocPage.path.in_(prefixes(page.path))))}
    return [rows[p] for p in prefixes(page.path) if p in rows]


async def _sibling(session: AsyncSession, page: DocPage, after: bool) -> DocPage | None:
    q = select(DocPage).where(DocPage.site_id == page.site_id, _under(page.parent_id), DocPage.id != page.id)
    if after:
        q = q.where((DocPage.position > page.position) | ((DocPage.position == page.position) & (DocPage.id > page.id))).order_by(DocPage.position, DocPage.id)
    else:
        q = q.where((DocPage.position < page.position) | ((DocPage.position == page.position) & (DocPage.id < page.id))).order_by(DocPage.position.desc(), DocPage.id.desc())
    return await session.scalar(q.limit(1))


async def neighbours(session: AsyncSession, page: DocPage) -> tuple[DocPage | None, DocPage | None]:
    """The page before and after it in reading order (depth first), in a few queries along the tree."""
    prev = await _sibling(session, page, after=False)
    if prev is not None:  # the last page under the sibling before it
        for _ in range(MAX_DEPTH):
            last = await session.scalar(select(DocPage).where(DocPage.parent_id == prev.id).order_by(DocPage.position.desc(), DocPage.id.desc()).limit(1))
            if last is None:
                break
            prev = last
    elif page.parent_id:
        prev = await session.get(DocPage, page.parent_id)
    nxt = await session.scalar(select(DocPage).where(DocPage.parent_id == page.id).order_by(DocPage.position, DocPage.id).limit(1))
    at = page
    for _ in range(MAX_DEPTH):
        if nxt is not None:
            break
        nxt = await _sibling(session, at, after=True)
        if nxt is not None or not at.parent_id:
            break
        at = await session.get(DocPage, at.parent_id)
        if at is None:
            break
    return prev, nxt


async def search_titles(session: AsyncSession, site_id: int, query: str, limit: int = 20) -> list[DocPage]:
    """Pages whose title or address has all these words."""
    q = select(DocPage).where(DocPage.site_id == site_id)
    for word in query.lower().split()[:6]:
        like = f"%{word}%"
        q = q.where(or_(func.lower(DocPage.title).like(like), DocPage.path.like(like)))
    return list(await session.scalars(q.order_by(DocPage.depth, DocPage.position).limit(limit)))


def item(page: DocPage, children_count: int = 0) -> DocPageItem:
    return DocPageItem(
        id=page.id, site_id=page.site_id, parent_id=page.parent_id, position=page.position, title=page.title, slug=page.slug, path=page.path,
        depth=page.depth, children=children_count, paths=list(page.paths or []), edited_by=page.edited_by, edited_at=page.edited_at, drift=page.drift,
        flagged_at=page.flagged_at, flag_reason=page.flag_reason, updated_at=page.updated_at,
    )  # fmt: skip


async def get_page(session: AsyncSession, project_id: int, page_id: int) -> DocPage:
    page = await session.get(DocPage, page_id)
    if page is None or page.project_id != project_id:
        raise NotFound(f"Doc page {page_id} not found")
    return page


async def publish_page(session: AsyncSession, page: DocPage) -> None:
    counts = await child_counts(session, [page.id])
    bus.publish(page.project_id, "doc_page.upserted", item(page, counts.get(page.id, 0)))


async def _unique_page_slug(session: AsyncSession, site_id: int, parent_id: int | None, base: str, exclude_id: int | None) -> str:
    q = select(DocPage.slug).where(DocPage.site_id == site_id, _under(parent_id), DocPage.slug.like(f"{base}%"))
    if exclude_id is not None:
        q = q.where(DocPage.id != exclude_id)
    taken = set(await session.scalars(q))
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


async def _place(session: AsyncSession, page: DocPage, parent_id: int | None, position: int | None) -> DocPage | None:
    """Put the page among its new siblings at ``position`` (None: last). Returns the parent."""
    parent = None
    if parent_id is not None:
        parent = await session.get(DocPage, parent_id)
        if parent is None or parent.site_id != page.site_id:
            raise ServiceError("The parent page is not a page of this site.")
        if page.id is not None and (parent.id == page.id or parent.path.startswith(page.path + "/")):
            raise ServiceError("A page cannot be moved below itself.")
        if parent.depth + 1 >= MAX_DEPTH:
            raise ServiceError(f"Pages nest at most {MAX_DEPTH} deep.")
    same = page.id is not None and page.parent_id == parent_id
    count = int(await session.scalar(select(func.count()).select_from(DocPage).where(DocPage.site_id == page.site_id, _under(parent_id))) or 0)
    if same:
        count -= 1
    at = count if position is None else min(position, count)
    shift = select(DocPage.id).where(DocPage.site_id == page.site_id, _under(parent_id), DocPage.position >= at)
    if page.id is not None:
        shift = shift.where(DocPage.id != page.id)
    if same:  # close the gap it leaves, then open one where it goes
        await session.execute(update(DocPage).where(DocPage.site_id == page.site_id, _under(parent_id), DocPage.position > page.position, DocPage.id != page.id).values(position=DocPage.position - 1))
    elif page.id is not None:
        await session.execute(update(DocPage).where(DocPage.site_id == page.site_id, _under(page.parent_id), DocPage.position > page.position).values(position=DocPage.position - 1))
    await session.execute(update(DocPage).where(DocPage.id.in_(shift)).values(position=DocPage.position + 1).execution_options(synchronize_session="fetch"))
    page.parent_id, page.position = parent_id, at
    return parent


async def _repath(session: AsyncSession, page: DocPage, parent: DocPage | None) -> None:
    """Its address and depth from its parent's, and those of every page below it (level by level)."""
    old = page.path
    page.path = f"{parent.path}/{page.slug}" if parent else page.slug
    page.depth = parent.depth + 1 if parent else 0
    if not old or old == page.path or page.id is None:
        return
    level = [page]
    for _ in range(MAX_DEPTH):
        ids = {p.id: p for p in level}
        below = list(await session.scalars(select(DocPage).where(DocPage.parent_id.in_(list(ids)))))
        if not below:
            break
        for child in below:
            up = ids[child.parent_id]
            child.path, child.depth = f"{up.path}/{child.slug}", up.depth + 1
        level = below


@dataclass
class Author:
    name: str
    agent_id: int | None = None


def _paths(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v.strip().lstrip("./").strip() for v in values if v.strip()))[:50]


async def create_page(
    session: AsyncSession, site: DocSite, author: Author, *, title: str, body: str = "", parent_id: int | None = None, position: int | None = None,
    slug: str | None = None, paths: list[str] | None = None, reason: str = "",
) -> DocPage:  # fmt: skip
    now = utcnow()
    title = title.strip()[:200]
    page = DocPage(project_id=site.project_id, site_id=site.id, title=title, body=body, paths=_paths(paths or []), edited_by=author.name, edited_at=now, source_hash=source_hash(title, body))
    parent = await _place(session, page, parent_id, position)
    page.slug = await _unique_page_slug(session, site.id, parent_id, slug or slugify(title, "page"), None)
    await _repath(session, page, parent)
    session.add(page)
    await session.flush()
    session.add(DocPageVersion(project_id=site.project_id, page_id=page.id, title=page.title, body=body, author=author.name, agent_id=author.agent_id, reason=(reason or "created")[:300], created_at=now))
    site.updated_at = now
    await session.commit()
    await publish_page(session, page)
    return page


async def update_page(
    session: AsyncSession, page: DocPage, author: Author, *, title: str | None = None, body: str | None = None, move: bool = False,
    parent_id: int | None = None, position: int | None = None, slug: str | None = None, paths: list[str] | None = None, reason: str = "",
) -> DocPage:  # fmt: skip
    """A new title or text is a new version (and makes its translations outdated); a move, a new slug or new linked
    paths are none."""
    now = utcnow()
    repath = False
    parent = await session.get(DocPage, page.parent_id) if page.parent_id else None
    if move or position is not None:
        target = parent_id if move else page.parent_id
        parent = await _place(session, page, target, position)
        if slug is None:  # its slug stays unless a sibling there has it
            page.slug = await _unique_page_slug(session, page.site_id, target, page.slug, page.id)
        repath = True
    if slug is not None and slug != page.slug:
        page.slug = await _unique_page_slug(session, page.site_id, page.parent_id, slug, page.id)
        repath = True
    if repath:
        await _repath(session, page, parent)
    if paths is not None:
        page.paths = _paths(paths)
    changed = (title is not None and title.strip() != page.title) or (body is not None and body != page.body)
    if changed:
        page.title = title.strip()[:200] if title is not None else page.title
        page.body = body if body is not None else page.body
        page.source_hash = source_hash(page.title, page.body)
        page.edited_by, page.edited_at = author.name, now
        page.drift, page.flagged_at, page.flag_reason, page.reminded_at = 0, None, "", None  # it was looked at
        session.add(DocPageVersion(project_id=page.project_id, page_id=page.id, title=page.title, body=page.body, author=author.name, agent_id=author.agent_id, reason=reason[:300], created_at=now))
    page.updated_at = now
    await touch(session, page.site_id)
    await session.commit()
    await publish_page(session, page)
    return page


async def confirm_page(session: AsyncSession, page: DocPage, author: Author, reason: str) -> DocPage:
    """Looked at and still right: the mark that it may be outdated goes, without a new version."""
    page.drift, page.flagged_at, page.flag_reason, page.reminded_at = 0, None, "", None
    page.edited_at = utcnow()
    await session.commit()
    await publish_page(session, page)
    return page


async def delete_page(session: AsyncSession, page: DocPage) -> None:
    """With the pages below it and all their versions and translations."""
    project_id, page_id, site_id, parent_id, position = page.project_id, page.id, page.site_id, page.parent_id, page.position
    await session.delete(page)
    await session.flush()
    await session.execute(update(DocPage).where(DocPage.site_id == site_id, _under(parent_id), DocPage.position > position).values(position=DocPage.position - 1))
    await touch(session, site_id)
    await session.commit()
    bus.publish(project_id, "doc_page.deleted", {"id": page_id, "site_id": site_id})


# --- versions -------------------------------------------------------------------------------------------------


async def versions(session: AsyncSession, page_id: int, limit: int = 100) -> list[DocPageVersion]:
    q = select(DocPageVersion).where(DocPageVersion.page_id == page_id).order_by(DocPageVersion.id.desc()).limit(limit)
    return list(await session.scalars(q))


async def count_versions(session: AsyncSession, page_id: int) -> int:
    return int(await session.scalar(select(func.count()).select_from(DocPageVersion).where(DocPageVersion.page_id == page_id)) or 0)


async def get_version(session: AsyncSession, page: DocPage, version_id: int) -> DocPageVersion:
    version = await session.get(DocPageVersion, version_id)
    if version is None or version.page_id != page.id:
        raise NotFound("Version not found")
    return version


async def restore(session: AsyncSession, page: DocPage, version_id: int, author: Author) -> DocPage:
    version = await get_version(session, page, version_id)
    when = f"{version.created_at:%Y-%m-%d %H:%M}"
    return await update_page(session, page, author, title=version.title, body=version.body, reason=f"restored the version of {when} by {version.author}")


# --- files ----------------------------------------------------------------------------------------------------


async def add_file(session: AsyncSession, site: DocSite, filename: str, content_type: str, data: bytes | BinaryIO, *, agent_id: int | None = None) -> Attachment:
    attachment = await board_service.store_attachment(session, site.project_id, filename, content_type, data, doc_site_id=site.id, agent_id=agent_id)
    bus.publish(site.project_id, "attachment.created", AttachmentOut.model_validate(attachment))
    return attachment


async def files(session: AsyncSession, site_id: int, offset: int = 0, limit: int | None = None) -> list[Attachment]:
    q = select(Attachment).where(Attachment.doc_site_id == site_id).order_by(Attachment.id).offset(offset)
    return list(await session.scalars(q.limit(limit) if limit else q))


async def get_file(session: AsyncSession, site: DocSite, attachment_id: int) -> Attachment:
    attachment = await session.get(Attachment, attachment_id)
    if attachment is None or attachment.doc_site_id != site.id:
        raise NotFound("File not found")
    return attachment


def file_markdown(attachment: Attachment) -> str:
    image = attachment.content_type.startswith("image/")
    return f"{'!' if image else ''}[{attachment.filename}](doc-file:{attachment.id})"


# --- moving a project (services/transfer.py) --------------------------------------------------------------------


def _relink(text: str, files: dict[int, int], pages: dict[int, int]) -> str:
    text = FILE_REF.sub(lambda m: f"doc-file:{files.get(int(m.group(1)), int(m.group(1)))}", text)
    return PAGE_REF.sub(lambda m: f"doc-page:{pages.get(int(m.group(1)), int(m.group(1)))}", text)


def _relink_theme(theme: dict, files: dict[int, int]) -> dict:
    return {**theme, **{k: files.get(theme[k], theme[k]) for k in ("logo_light", "logo_dark", "favicon") if theme.get(k)}}


async def after_import(session: AsyncSession, new: dict[str, dict[int, int]]) -> None:
    """Imported pages name their files and pages by the ids of the other side: by the new ones from now on."""
    files, page_ids = new.get("attachments", {}), new.get("doc_pages", {})
    from . import docs_custom

    for custom in await session.scalars(select(DocCustomPage).where(DocCustomPage.id.in_(list(new.get("doc_custom_pages", {}).values())))):
        docs_custom.relink(custom, files, page_ids)
    for site in await session.scalars(select(DocSite).where(DocSite.id.in_(list(new.get("doc_sites", {}).values())))):
        if site.theme:  # its logos and favicon are files of the site
            site.theme = _relink_theme(site.theme, files)
    from ..models import DocThemeVersion

    for version in await session.scalars(select(DocThemeVersion).where(DocThemeVersion.site_id.in_(list(new.get("doc_sites", {}).values())))):
        if version.theme:
            version.theme = _relink_theme(version.theme, files)
    if not page_ids:
        return
    ids = list(page_ids.values())
    for model in (DocPage, DocPageVersion, DocTranslation):
        column = model.id if model is DocPage else model.page_id
        for row in await session.scalars(select(model).where(column.in_(ids), or_(model.body.contains("doc-file:"), model.body.contains("doc-page:")))):
            row.body = _relink(row.body, files, page_ids)
