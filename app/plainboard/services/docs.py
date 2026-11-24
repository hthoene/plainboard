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

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Agent, Attachment, DocCustomPage, DocPage, DocPageVersion, DocRelease, DocSite, DocTranslation, utcnow
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


def site_out(site: DocSite, pages: int = 0, stale: int = 0, agent: Agent | None = None, releases: list | None = None) -> DocSiteOut:
    return DocSiteOut(
        releases=releases or [], versioned=site.versioned,
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
        select(DocPage.site_id, func.count(), func.count(DocPage.flagged_at)).where(DocPage.site_id.in_(ids), DocPage.until_seq.is_(None)).group_by(DocPage.site_id)
    )  # the working version's
    counts = {site_id: (int(n), int(stale)) for site_id, n, stale in rows.all()}
    agents = {a.doc_site_id: a for a in await session.scalars(select(Agent).where(Agent.doc_site_id.in_(ids)))}
    from . import docs_releases

    releases = await docs_releases.outs_by_site(session, ids)
    return [site_out(s, *counts.get(s.id, (0, 0)), agents.get(s.id), releases.get(s.id)) for s in sites]


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
    agent_effort: str | None = None, versioned: bool = False,
) -> DocSite:  # fmt: skip
    """With its doc agent, the one agent that writes it (``ensure_agent``)."""
    if slug and await session.scalar(select(DocSite.id).where(DocSite.slug == slug)) is not None:
        raise ServiceError(f"The address /docs/{slug}/ is taken: choose another slug.", 409)
    if not LANGUAGE.match(language):
        raise ServiceError(f"Not a language code: {language!r}")
    site = DocSite(
        project_id=project_id, name=name.strip(), slug=slug or await unique_site_slug(session, name), description=description.strip(), spec=spec.strip(),
        published=published, public=public, language=language, languages=clean_languages(languages or [], language), domain_token=secrets.token_urlsafe(24),
        versioned=versioned,
    )  # fmt: skip
    session.add(site)
    await session.flush()
    await working(session, site.id)
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
- Versions (list_docs shows the site's): every tool works on the working version unless you name one (version), and
  update jobs after landings are about the working version. A released version describes the product as it was
  released: change it only to correct an error that is wrong there too, never for new features or later changes.
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
SITE_FIELDS = ("name", "description", "spec", "published", "public", "versioned", "translator", "translate_provider", "translate_model", *ASSISTANT_FIELDS)
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


# --- versions of a site (#126) ----------------------------------------------------------------------------------
# A row of doc_pages holds a page in a range of the site's versions (``since_seq`` .. ``until_seq``, None: up to the
# working one); every page is one row as long as nothing changes it, whatever the number of versions. Cutting a
# version moves the working version's ``seq`` on and copies nothing; a change of a page in one version splits its row
# first (``isolate``: copy on write), so the others keep what they had. The row with the highest range keeps the id,
# ``page_key``: what parent_id, links and the API name a page by in every version. Release management:
# services/docs_releases.py.

Span = tuple[int, int | None]  # the versions a change goes to: (seq, seq) or, for the working one, (its seq, None)


def visible(rel: DocRelease | None):
    """Rows of pages in this version (None: the working one)."""
    if rel is None or rel.working:
        return DocPage.until_seq.is_(None)
    return and_(DocPage.since_seq <= rel.seq, or_(DocPage.until_seq.is_(None), DocPage.until_seq >= rel.seq))


async def working(session: AsyncSession, site_id: int) -> DocRelease:
    """The site's working version (made if it has none)."""
    rel = await session.scalar(select(DocRelease).where(DocRelease.site_id == site_id, DocRelease.working.is_(True)).limit(1))
    if rel is None:
        site = await session.get(DocSite, site_id)
        assert site is not None
        seq = int(await session.scalar(select(func.max(DocRelease.seq)).where(DocRelease.site_id == site_id)) or 0) + 1
        rel = DocRelease(project_id=site.project_id, site_id=site_id, seq=seq, working=True, name="next", slug="next", notes="")
        session.add(rel)
        await session.flush()
    return rel


async def span_of(session: AsyncSession, site_id: int, rel: DocRelease | None) -> Span:
    if rel is None or rel.working:
        return (rel or await working(session, site_id)).seq, None
    return rel.seq, rel.seq


_COPIED = (
    "project_id", "site_id", "parent_id", "position", "page_key", "title", "slug", "path", "depth", "body", "source_hash", "paths", "edited_by",
    "edited_at", "drift", "flagged_at", "flag_reason", "reminded_at", "created_at",
)  # fmt: skip


async def _copy(session: AsyncSession, row: DocPage, since: int, until: int | None) -> DocPage:
    """The row's page, with its translations, for these versions."""
    twin = DocPage(**{k: getattr(row, k) for k in _COPIED}, since_seq=since, until_seq=until)
    twin.paths = list(row.paths or [])
    session.add(twin)
    await session.flush()
    for t in list(await session.scalars(select(DocTranslation).where(DocTranslation.page_id == row.id))):
        session.add(DocTranslation(
            project_id=t.project_id, site_id=t.site_id, page_id=twin.id, lang=t.lang, title=t.title, body=t.body, source_hash=t.source_hash,
            translated_by=t.translated_by, translated_at=t.translated_at, error=t.error, attempted_at=t.attempted_at,
        ))  # fmt: skip
    return twin


def _parts(row: DocPage, span: Span) -> tuple[Span | None, Span | None]:
    """The row's versions below and above the span."""
    lo, hi = span
    below = (row.since_seq, lo - 1) if row.since_seq < lo else None
    above = (hi + 1, row.until_seq) if hi is not None and (row.until_seq is None or row.until_seq > hi) else None
    return below, above


async def isolate(session: AsyncSession, row: DocPage, span: Span) -> DocPage:
    """The row of the page for these versions only, split off before a change (copy on write)."""
    below, above = _parts(row, span)
    if below is None and above is None:
        return row
    if above is not None:
        target = await _copy(session, row, *span)
        row.since_seq, row.until_seq = above
    else:
        target = row
        row.since_seq, row.until_seq = span
    if below is not None:
        await _copy(session, row, *below)
    return target


async def drop(session: AsyncSession, row: DocPage, span: Span) -> None:
    """The page's row leaves these versions; the other versions keep it."""
    below, above = _parts(row, span)
    if above is not None:
        if below is not None:
            await _copy(session, row, *below)
        row.since_seq, row.until_seq = above
    elif below is not None:
        row.since_seq, row.until_seq = below
    else:
        await remove_row(session, row)


async def remove_row(session: AsyncSession, row: DocPage) -> None:
    """A row gone from every version. A page's key row first takes over its highest other row (its id stays the
    page's), so that no other version loses the page or what points at it."""
    if row.id == row.page_key:
        other = await session.scalar(select(DocPage).where(DocPage.page_key == row.id, DocPage.id != row.id).order_by(DocPage.since_seq.desc()).limit(1))
        if other is not None:
            for key in (*_COPIED, "since_seq", "until_seq"):
                setattr(row, key, getattr(other, key))
            await session.execute(delete(DocTranslation).where(DocTranslation.page_id == row.id))
            await session.execute(update(DocTranslation).where(DocTranslation.page_id == other.id).values(page_id=row.id).execution_options(synchronize_session=False))
            await session.execute(update(DocPageVersion).where(DocPageVersion.page_id == other.id).values(page_id=row.id).execution_options(synchronize_session=False))
            await session.delete(other)
            await session.flush()
            return
    await session.delete(row)
    await session.flush()


# --- the tree -------------------------------------------------------------------------------------------------


def _under(parent_id: int | None):
    return DocPage.parent_id.is_(None) if parent_id is None else DocPage.parent_id == parent_id


ORDER = (DocPage.position, DocPage.page_key)


async def children(
    session: AsyncSession, site_id: int, parent_id: int | None, offset: int = 0, limit: int = PAGE_LIMIT, rel: DocRelease | None = None
) -> tuple[list[DocPage], dict[int, int], int]:
    """One page of a page's children (None: the top), with how many children each has (by page key), and how many there are."""
    where = (DocPage.site_id == site_id, _under(parent_id), visible(rel))
    rows = list(await session.scalars(select(DocPage).where(*where).order_by(*ORDER).offset(offset).limit(min(limit, PAGE_LIMIT))))
    total = int(await session.scalar(select(func.count()).select_from(DocPage).where(*where)) or 0)
    return rows, await child_counts(session, [p.page_key for p in rows], rel), total


async def levels(session: AsyncSession, site_id: int, parent_ids: list[int | None], rel: DocRelease | None = None) -> dict[int | None, tuple[list[DocPage], int]]:
    """The children of several pages at once (None: the top), at most ``PAGE_LIMIT`` each, with how many there are:
    the navigation opened down to a page, in two queries whatever its depth."""
    if not parent_ids:
        return {}
    ids = [p for p in parent_ids if p is not None]
    where = or_(DocPage.parent_id.in_(ids), DocPage.parent_id.is_(None)) if None in parent_ids else DocPage.parent_id.in_(ids)
    numbered = (
        select(DocPage.id, func.row_number().over(partition_by=DocPage.parent_id, order_by=ORDER).label("n"))
        .where(DocPage.site_id == site_id, where, visible(rel))
        .subquery()
    )
    rows = list(await session.scalars(select(DocPage).join(numbered, numbered.c.id == DocPage.id).where(numbered.c.n <= PAGE_LIMIT).order_by(*ORDER)))
    totals = dict((await session.execute(select(DocPage.parent_id, func.count()).where(DocPage.site_id == site_id, where, visible(rel)).group_by(DocPage.parent_id))).all())
    out: dict[int | None, tuple[list[DocPage], int]] = {p: ([], int(totals.get(p, 0))) for p in parent_ids}
    for page in rows:
        out[page.parent_id][0].append(page)
    return out


async def child_counts(session: AsyncSession, keys: list[int], rel: DocRelease | None = None) -> dict[int, int]:
    """By page key: how many pages are directly below it in this version."""
    if not keys:
        return {}
    rows = await session.execute(select(DocPage.parent_id, func.count()).where(DocPage.parent_id.in_(keys), visible(rel)).group_by(DocPage.parent_id))
    return {pid: int(n) for pid, n in rows.all()}


async def row_of(session: AsyncSession, key: int, rel: DocRelease | None = None) -> DocPage | None:
    """The page in this version."""
    return await session.scalar(select(DocPage).where(DocPage.page_key == key, visible(rel)).limit(1))


async def home(session: AsyncSession, site_id: int, rel: DocRelease | None = None) -> DocPage | None:
    """The first page at the top: the site's start page."""
    return await session.scalar(select(DocPage).where(DocPage.site_id == site_id, DocPage.parent_id.is_(None), visible(rel)).order_by(*ORDER).limit(1))


async def by_path(session: AsyncSession, site_id: int, path: str, rel: DocRelease | None = None) -> DocPage | None:
    path = path.strip("/")
    if not path:
        return await home(session, site_id, rel)
    return await session.scalar(select(DocPage).where(DocPage.site_id == site_id, DocPage.path == path, visible(rel)).limit(1))


def prefixes(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts) - 1)]


async def ancestors(session: AsyncSession, page: DocPage, rel: DocRelease | None = None) -> list[DocPage]:
    """From the top down to its parent: one query by the prefixes of its address."""
    if not page.parent_id:
        return []
    q = select(DocPage).where(DocPage.site_id == page.site_id, DocPage.path.in_(prefixes(page.path)), visible(rel))
    rows = {p.path: p for p in await session.scalars(q)}
    return [rows[p] for p in prefixes(page.path) if p in rows]


async def _sibling(session: AsyncSession, page: DocPage, after: bool, rel: DocRelease | None) -> DocPage | None:
    q = select(DocPage).where(DocPage.site_id == page.site_id, _under(page.parent_id), DocPage.page_key != page.page_key, visible(rel))
    if after:
        q = q.where((DocPage.position > page.position) | ((DocPage.position == page.position) & (DocPage.page_key > page.page_key))).order_by(*ORDER)
    else:
        q = q.where((DocPage.position < page.position) | ((DocPage.position == page.position) & (DocPage.page_key < page.page_key)))
        q = q.order_by(DocPage.position.desc(), DocPage.page_key.desc())
    return await session.scalar(q.limit(1))


async def neighbours(session: AsyncSession, page: DocPage, rel: DocRelease | None = None, up: list[DocPage] | None = None) -> tuple[DocPage | None, DocPage | None]:
    """The page before and after it in reading order (depth first), in a few queries along the tree; ``up``: its
    ancestors where they are read already."""
    known = {p.page_key: p for p in up or []}

    async def parent_of(row: DocPage) -> DocPage | None:
        return known.get(row.parent_id) or await row_of(session, row.parent_id, rel) if row.parent_id else None

    prev = await _sibling(session, page, False, rel)
    if prev is not None:  # the last page under the sibling before it
        for _ in range(MAX_DEPTH):
            q = select(DocPage).where(DocPage.parent_id == prev.page_key, visible(rel)).order_by(DocPage.position.desc(), DocPage.page_key.desc())
            last = await session.scalar(q.limit(1))
            if last is None:
                break
            prev = last
    elif page.parent_id:
        prev = await parent_of(page)
    nxt = await session.scalar(select(DocPage).where(DocPage.parent_id == page.page_key, visible(rel)).order_by(*ORDER).limit(1))
    at: DocPage | None = page
    for _ in range(MAX_DEPTH):
        if nxt is not None or at is None:
            break
        nxt = await _sibling(session, at, True, rel)
        if nxt is not None or not at.parent_id:
            break
        at = await parent_of(at)
    return prev, nxt


async def search_titles(session: AsyncSession, site_id: int, query: str, limit: int = 20, rel: DocRelease | None = None) -> list[DocPage]:
    """Pages whose title or address has all these words."""
    q = select(DocPage).where(DocPage.site_id == site_id, visible(rel))
    for word in query.lower().split()[:6]:
        like = f"%{word}%"
        q = q.where(or_(func.lower(DocPage.title).like(like), DocPage.path.like(like)))
    return list(await session.scalars(q.order_by(DocPage.depth, DocPage.position).limit(limit)))


def item(page: DocPage, children_count: int = 0, rel: DocRelease | None = None) -> DocPageItem:
    return DocPageItem(
        id=page.page_key or page.id, site_id=page.site_id, release_id=rel.id if rel is not None and not rel.working else None, parent_id=page.parent_id,
        position=page.position, title=page.title, slug=page.slug, path=page.path, depth=page.depth, children=children_count, paths=list(page.paths or []),
        edited_by=page.edited_by, edited_at=page.edited_at, drift=page.drift, flagged_at=page.flagged_at, flag_reason=page.flag_reason, updated_at=page.updated_at,
    )  # fmt: skip


async def get_page(session: AsyncSession, project_id: int, page_id: int, rel: DocRelease | None = None) -> DocPage:
    """A page (by its id: its key) in a version (None: the working one)."""
    page = await row_of(session, page_id, rel)
    if page is None or page.project_id != project_id or (rel is not None and rel.site_id != page.site_id):
        raise NotFound(f"Doc page {page_id} not found" + (f" in version {rel.name}" if rel is not None else ""))
    return page


async def publish_page(session: AsyncSession, page: DocPage, rel: DocRelease | None = None) -> None:
    counts = await child_counts(session, [page.page_key], rel)
    bus.publish(page.project_id, "doc_page.upserted", item(page, counts.get(page.page_key, 0), rel))


async def _unique_page_slug(session: AsyncSession, site_id: int, parent_id: int | None, base: str, exclude_key: int | None, rel: DocRelease | None) -> str:
    q = select(DocPage.slug).where(DocPage.site_id == site_id, _under(parent_id), DocPage.slug.like(f"{base}%"), visible(rel))
    if exclude_key is not None:
        q = q.where(DocPage.page_key != exclude_key)
    taken = set(await session.scalars(q))
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


async def _shift(session: AsyncSession, site_id: int, parent_id: int | None, rel: DocRelease | None, span: Span, cond, by: int, exclude_key: int | None) -> None:  # noqa: ANN001
    """Siblings in this version that meet ``cond`` move ``by`` places (split off from other versions first)."""
    q = select(DocPage).where(DocPage.site_id == site_id, _under(parent_id), visible(rel), cond)
    if exclude_key is not None:
        q = q.where(DocPage.page_key != exclude_key)
    for row in list(await session.scalars(q)):
        row = await isolate(session, row, span)
        row.position += by
    await session.flush()


async def _place(session: AsyncSession, page: DocPage, parent_id: int | None, position: int | None, rel: DocRelease | None, span: Span) -> DocPage | None:
    """Put the page among its new siblings at ``position`` (None: last). Returns the parent."""
    parent = None
    if parent_id is not None:
        parent = await row_of(session, parent_id, rel)
        if parent is None or parent.site_id != page.site_id:
            raise ServiceError("The parent page is not a page of this site" + (f" in version {rel.name}." if rel is not None and not rel.working else "."))
        if page.page_key is not None and (parent.page_key == page.page_key or parent.path.startswith(page.path + "/")):
            raise ServiceError("A page cannot be moved below itself.")
        if parent.depth + 1 >= MAX_DEPTH:
            raise ServiceError(f"Pages nest at most {MAX_DEPTH} deep.")
    known = page.page_key is not None
    same = known and page.parent_id == parent_id
    count = int(await session.scalar(select(func.count()).select_from(DocPage).where(DocPage.site_id == page.site_id, _under(parent_id), visible(rel))) or 0)
    if same:
        count -= 1
    at = count if position is None else min(position, count)
    if known:  # close the gap it leaves, then open one where it goes
        await _shift(session, page.site_id, page.parent_id, rel, span, DocPage.position > page.position, -1, page.page_key)
    if at < count:
        await _shift(session, page.site_id, parent_id, rel, span, DocPage.position >= at, 1, page.page_key)
    page.parent_id, page.position = parent_id, at
    return parent


async def _repath(session: AsyncSession, page: DocPage, parent: DocPage | None, rel: DocRelease | None, span: Span) -> None:
    """Its address and depth from its parent's, and those of every page below it in this version (level by level)."""
    old = page.path
    page.path = f"{parent.path}/{page.slug}" if parent else page.slug
    page.depth = parent.depth + 1 if parent else 0
    if not old or old == page.path or page.page_key is None:
        return
    level = [page]
    for _ in range(MAX_DEPTH):
        keys = {p.page_key: p for p in level}
        below = list(await session.scalars(select(DocPage).where(DocPage.parent_id.in_(list(keys)), visible(rel))))
        if not below:
            break
        moved = []
        for child in below:
            child = await isolate(session, child, span)
            up = keys[child.parent_id]
            child.path, child.depth = f"{up.path}/{child.slug}", up.depth + 1
            moved.append(child)
        level = moved


@dataclass
class Author:
    name: str
    agent_id: int | None = None


def _paths(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v.strip().lstrip("./").strip() for v in values if v.strip()))[:50]


async def create_page(
    session: AsyncSession, site: DocSite, author: Author, *, title: str, body: str = "", parent_id: int | None = None, position: int | None = None,
    slug: str | None = None, paths: list[str] | None = None, reason: str = "", rel: DocRelease | None = None,
) -> DocPage:  # fmt: skip
    """In one version (None: the working one, and so in every version cut from now on)."""
    now = utcnow()
    title = title.strip()[:200]
    span = await span_of(session, site.id, rel)
    page = DocPage(
        project_id=site.project_id, site_id=site.id, title=title, body=body, paths=_paths(paths or []), edited_by=author.name, edited_at=now,
        source_hash=source_hash(title, body), since_seq=span[0], until_seq=span[1],
    )  # fmt: skip
    parent = await _place(session, page, parent_id, position, rel, span)
    page.slug = await _unique_page_slug(session, site.id, parent_id, slug or slugify(title, "page"), None, rel)
    await _repath(session, page, parent, rel, span)
    session.add(page)
    await session.flush()
    page.page_key = page.id
    session.add(DocPageVersion(project_id=site.project_id, page_id=page.id, title=page.title, body=body, author=author.name, agent_id=author.agent_id, reason=(reason or "created")[:300], created_at=now))
    site.updated_at = now
    await session.commit()
    await publish_page(session, page, rel)
    return page


async def update_page(
    session: AsyncSession, page: DocPage, author: Author, *, title: str | None = None, body: str | None = None, move: bool = False,
    parent_id: int | None = None, position: int | None = None, slug: str | None = None, paths: list[str] | None = None, reason: str = "",
    rel: DocRelease | None = None,
) -> DocPage:  # fmt: skip
    """A new title or text is a new version (and makes its translations outdated); a move, a new slug or new linked
    paths are none. ``page`` is its row in ``rel`` (None: the working version); a change of title, text or place goes
    to that version only, linked paths to the row (they matter in the working version only)."""
    now = utcnow()
    changed = (title is not None and title.strip() != page.title) or (body is not None and body != page.body)
    span = await span_of(session, page.site_id, rel)
    if changed or move or position is not None or (slug is not None and slug != page.slug):
        page = await isolate(session, page, span)
    repath = False
    parent = await row_of(session, page.parent_id, rel) if page.parent_id else None
    if move or position is not None:
        target = parent_id if move else page.parent_id
        parent = await _place(session, page, target, position, rel, span)
        if slug is None:  # its slug stays unless a sibling there has it
            page.slug = await _unique_page_slug(session, page.site_id, target, page.slug, page.page_key, rel)
        repath = True
    if slug is not None and slug != page.slug:
        page.slug = await _unique_page_slug(session, page.site_id, page.parent_id, slug, page.page_key, rel)
        repath = True
    if repath:
        await _repath(session, page, parent, rel, span)
    if paths is not None:
        page.paths = _paths(paths)
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
    await publish_page(session, page, rel)
    return page


async def confirm_page(session: AsyncSession, page: DocPage, author: Author, reason: str, rel: DocRelease | None = None) -> DocPage:
    """Looked at and still right: the mark that it may be outdated goes, without a new version."""
    page.drift, page.flagged_at, page.flag_reason, page.reminded_at = 0, None, "", None
    page.edited_at = utcnow()
    await session.commit()
    await publish_page(session, page, rel)
    return page


async def delete_page(session: AsyncSession, page: DocPage, rel: DocRelease | None = None) -> None:
    """With the pages below it, from this version (None: the working one); the other versions keep theirs. Gone from
    every version, a page goes with all its versions and translations."""
    project_id, key, site_id, parent_id, position = page.project_id, page.page_key, page.site_id, page.parent_id, page.position
    span = await span_of(session, site_id, rel)
    tree = [[page]]
    for _ in range(MAX_DEPTH):
        below = list(await session.scalars(select(DocPage).where(DocPage.parent_id.in_([p.page_key for p in tree[-1]]), visible(rel))))
        if not below:
            break
        tree.append(below)
    for level in reversed(tree):  # the deepest first: nothing goes by a cascade before its turn
        for row in level:
            await drop(session, row, span)
    await _shift(session, site_id, parent_id, rel, span, DocPage.position > position, -1, None)
    await touch(session, site_id)
    await session.commit()
    bus.publish(project_id, "doc_page.deleted", {"id": key, "site_id": site_id, "release_id": rel.id if rel is not None and not rel.working else None})


# --- versions -------------------------------------------------------------------------------------------------
# A page's saved states, in every version of the site: the history of all its rows.


def _rows(page_key: int):
    return select(DocPage.id).where(DocPage.page_key == page_key)


async def versions(session: AsyncSession, page_key: int, limit: int = 100) -> list[DocPageVersion]:
    q = select(DocPageVersion).where(DocPageVersion.page_id.in_(_rows(page_key))).order_by(DocPageVersion.id.desc()).limit(limit)
    return list(await session.scalars(q))


async def count_versions(session: AsyncSession, page_key: int) -> int:
    return int(await session.scalar(select(func.count()).select_from(DocPageVersion).where(DocPageVersion.page_id.in_(_rows(page_key)))) or 0)


async def get_version(session: AsyncSession, page: DocPage, version_id: int) -> DocPageVersion:
    version = await session.get(DocPageVersion, version_id)
    if version is None or (await session.get(DocPage, version.page_id) or page).page_key != page.page_key:
        raise NotFound("Version not found")
    return version


async def restore(session: AsyncSession, page: DocPage, version_id: int, author: Author, rel: DocRelease | None = None) -> DocPage:
    version = await get_version(session, page, version_id)
    when = f"{version.created_at:%Y-%m-%d %H:%M}"
    return await update_page(session, page, author, title=version.title, body=version.body, reason=f"restored the version of {when} by {version.author}", rel=rel)

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
