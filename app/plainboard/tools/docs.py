"""Doc sites (#118, services/docs.py): every agent reads them, each site's own doc agent alone writes it.

The manager makes and configures sites (with their doc agent), keeps the doc spec with the user and orders the doc
agent work (order_docs). The doc spec says what the user wants in a site: it is the doc agent's standing instructions,
and the manager sees it with the first look at a site in a turn.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..models import Assignment, DocPage, DocSite
from ..services import board as board_service
from ..services import docs as docs_service
from ..services import docs_public, docs_releases
from ..services.util import truncate
from .registry import ALL, DOC_AGENT, DOCS, MANAGER, ToolContext, ToolError, local_tool, tool

_spec_seen: OrderedDict[tuple[int, int], None] = OrderedDict()  # (run, site): the spec was in a result of this turn


def _saw(run_id: int, site_id: int) -> bool:
    key = (run_id, site_id)
    seen = key in _spec_seen
    _spec_seen[key] = None
    while len(_spec_seen) > 2000:
        _spec_seen.popitem(last=False)
    return seen


def _spec(site: DocSite) -> str:
    if not site.spec.strip():
        return f"Doc spec of {site.slug}: none yet (the manager writes it with the user)."
    return f"Doc spec of {site.slug} (follow it):\n{site.spec.strip()}"


def _author(ctx: ToolContext) -> docs_service.Author:
    return docs_service.Author(ctx.agent.name, ctx.agent.id)


def _own(ctx: ToolContext, site: DocSite) -> None:
    if ctx.agent.doc_site_id != site.id:
        raise ToolError(f"Only the doc agent of {site.slug} writes it.")


def _site_line(site, pages: int, stale: int) -> str:  # noqa: ANN001
    where = f"/docs/{site.slug}/" + (f", {site.domain}" + ("" if site.domain_verified_at else " (not verified)") if site.domain else "")
    served = ("public" if site.public else "private") if site.published else "draft"
    langs = f"; languages {site.language}+{','.join(site.languages)}" if site.languages else ""
    agent = f"; doc agent {site.agent.slug}" if getattr(site, "agent", None) else ""
    released = [r for r in getattr(site, "releases", []) if not r.working] if getattr(site, "versioned", False) else []
    versions = "; versions: working + " + ", ".join(r.name + (" (deprecated)" if r.deprecated else "") + (" (hidden)" if r.hidden else "") for r in released[:10]) if released else ""
    return f"- {site.slug}: {site.name} [{served}; {where}; {pages} pages" + (f", {stale} marked outdated" if stale else "") + f"{langs}{versions}{agent}]"


LISTED = 50  # pages per call
MARKED = 15
FOUND = 10  # pages per search


def _page_line(p: DocPage, children: int) -> str:
    mark = f" [marked outdated: {truncate(p.flag_reason, 160)}]" if p.flagged_at else ""
    linked = f" paths: {', '.join(p.paths)}" if p.paths else ""
    below = f" ({children} below)" if children else ""
    return f"- {p.page_key} /{p.path} \"{p.title}\"{below} (edited {p.edited_at:%Y-%m-%d} by {p.edited_by}){linked}{mark}"


VERSION = "A released version of the site (name, e.g. 1.0); empty = the working version"


async def _version(ctx: ToolContext, site: DocSite, ref: str | None):  # noqa: ANN202
    return await docs_releases.find(ctx.session, site, ref) if ref else None


async def _page_version(ctx: ToolContext, page_key: int, ref: str | None):  # noqa: ANN202
    """The version named for a page (of the page's site); empty: the working one (None)."""
    if not ref:
        return None
    row = await ctx.session.scalar(select(DocPage).where(DocPage.page_key == page_key, DocPage.project_id == ctx.project.id).limit(1))
    if row is None:
        raise ToolError(f"Doc page {page_key} not found")
    site = await ctx.session.get(DocSite, row.site_id)
    assert site is not None
    return await _version(ctx, site, ref)


def _in(rel) -> str:  # noqa: ANN001
    return f" (version {rel.name})" if rel is not None else ""


class ListDocsArgs(BaseModel):
    site: str | None = Field(default=None, description="Site slug/id: its doc spec and pages; empty = all sites")
    parent: int | None = Field(default=None, description="Pages below this page id; empty = the top level")
    query: str | None = Field(default=None, max_length=200, description="Search the site's pages by meaning and words instead (best 10, with a snippet)")
    marked: bool = Field(default=False, description="Only pages marked as possibly outdated")
    version: str | None = Field(default=None, max_length=80, description=VERSION)
    offset: int = Field(default=0, ge=0)


@tool(
    "list_docs",
    "The project's doc sites, or one site's doc spec and pages: one level of the tree (parent), a search (query) or the marked ones, 50 at a time.",
    ListDocsArgs,
    roles=ALL,
    feature=DOCS,
)
async def list_docs(ctx: ToolContext, args: ListDocsArgs) -> str:
    if not args.site:
        outs = await docs_service.site_outs(ctx.session, await docs_service.sites(ctx.session, ctx.project.id))
        if not outs:
            return "No doc sites yet."
        return "\n".join(_site_line(s, s.pages, s.stale_pages) for s in outs)
    site = await docs_service.find_site(ctx.session, ctx.project.id, args.site)
    rel = await _version(ctx, site, args.version)
    head = [_spec(site), ""] if ctx.agent.is_manager and not _saw(ctx.run.run_id, site.id) else []
    name = site.slug + _in(rel)
    if args.query:
        hits, _ = await docs_public.search_hits(ctx.session, site, args.query, f"/docs/{site.slug}", site.language, FOUND, budget=False, rel=rel)
        if not hits:
            return "\n".join(head + [f"No pages of {name} found for {args.query!r}."])
        q = select(DocPage).where(DocPage.page_key.in_([h["id"] for h in hits]), docs_service.visible(rel))
        pages = {p.page_key: p for p in await ctx.session.scalars(q)}
        found = [f"{_page_line(pages[h['id']], 0)}\n  {h['snippet']}" for h in hits if h["id"] in pages]
        return "\n".join(head + [f"Pages of {name} found for {args.query!r} (by meaning and words, best first):", *found])
    elif args.marked:
        q = select(DocPage).where(DocPage.site_id == site.id, DocPage.flagged_at.is_not(None), docs_service.visible(rel))
        total = int(await ctx.session.scalar(select(func.count()).select_from(q.subquery())) or 0)
        rows = list(await ctx.session.scalars(q.order_by(DocPage.flagged_at).offset(args.offset).limit(MARKED)))
        title = f"Pages of {name} marked as possibly outdated ({total}):"
    else:
        rows, _, total = await docs_service.children(ctx.session, site.id, args.parent, args.offset, LISTED, rel)
        where = f"below page {args.parent}" if args.parent else "at the top"
        title = f"Pages of {name} {where} ({args.offset + 1}-{args.offset + len(rows)} of {total}):" if rows else f"No pages {where}{_in(rel)}."
    counts = await docs_service.child_counts(ctx.session, [p.page_key for p in rows], rel)
    lines = head + [title] + [_page_line(p, counts.get(p.page_key, 0)) for p in rows]
    if args.offset + len(rows) < total:
        lines.append(f"(more: offset={args.offset + len(rows)})")
    return "\n".join(lines)


class ReadDocPageArgs(BaseModel):
    page: int = Field(description="Page id")
    offset: int = Field(default=0, ge=0, description="Characters to skip of a long page")
    version: str | None = Field(default=None, max_length=80, description=VERSION)


READ_CHARS = 20000


@tool("read_doc_page", "A doc page's Markdown (long pages in parts).", ReadDocPageArgs, roles=ALL, feature=DOCS)
async def read_doc_page(ctx: ToolContext, args: ReadDocPageArgs) -> str:
    rel = await _page_version(ctx, args.page, args.version)
    page = await docs_service.get_page(ctx.session, ctx.project.id, args.page, rel)
    site = await ctx.session.get(DocSite, page.site_id)
    assert site is not None
    facts = [f"site {site.slug}{_in(rel)}", f"/{page.path}", f"edited {page.edited_at:%Y-%m-%d} by {page.edited_by}"]
    if page.paths:
        facts.append(f"paths: {', '.join(page.paths)}")
    mark = f"\nMarked as possibly outdated: {page.flag_reason}" if page.flagged_at else ""
    body = page.body or "(empty)"
    part = body[args.offset : args.offset + READ_CHARS]
    more = f"\n\n(characters {args.offset}-{args.offset + len(part)} of {len(body)}; more: offset={args.offset + len(part)})" if args.offset or len(body) > args.offset + READ_CHARS else ""
    return f"Doc page {page.page_key} \"{page.title}\" ({'; '.join(facts)}){mark}\n\n{part}{more}"


class WriteDocPageArgs(BaseModel):
    reason: str = Field(min_length=3, max_length=300, description="Why, in one short sentence (kept with the version)")
    page: int | None = Field(default=None, description="Page id to change; empty = a new page")
    site: str | None = Field(default=None, description="New page: your site's slug/id")
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, description="Whole Markdown text. Files: ![alt](doc-file:ID), links to pages: [text](doc-page:ID)")
    parent: int | None = Field(default=None, description="Parent page id (with move: 0 = top level)")
    position: int | None = Field(default=None, ge=0, description="Place among its siblings, 0 = first")
    move: bool = Field(default=False, description="Move the page to parent/position")
    slug: str | None = Field(default=None, max_length=80, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    paths: list[str] | None = Field(default=None, max_length=50, description="Code paths it describes (prefix or glob): changes there mark it for update")
    still_current: bool = Field(default=False, description="Checked after a change: still right, clear its outdated mark")
    version: str | None = Field(default=None, max_length=80, description="A released version to correct (name); empty = the working version")


@tool(
    "write_doc_page",
    "Create, change or move a page of your doc site; every change of title or text is kept as a version. Follow the doc spec.",
    WriteDocPageArgs,
    roles=(DOC_AGENT,),
)
async def write_doc_page(ctx: ToolContext, args: WriteDocPageArgs) -> str:
    author = _author(ctx)
    if args.page is None:
        if not args.site or not args.title:
            raise ToolError("A new page needs site and title.")
        site = await docs_service.find_site(ctx.session, ctx.project.id, args.site)
        _own(ctx, site)
        rel = await _version(ctx, site, args.version)
        page = await docs_service.create_page(
            ctx.session, site, author, title=args.title, body=args.body or "", parent_id=args.parent or None, position=args.position,
            slug=args.slug, paths=args.paths, reason=args.reason, rel=rel,
        )  # fmt: skip
        done = f"Created doc page {page.page_key} /{page.path} in {site.slug}{_in(rel)}."
    else:
        rel = await _page_version(ctx, args.page, args.version)
        page = await docs_service.get_page(ctx.session, ctx.project.id, args.page, rel)
        site = await ctx.session.get(DocSite, page.site_id)
        assert site is not None
        _own(ctx, site)
        if args.still_current and args.body is None and args.title is None:
            await docs_service.confirm_page(ctx.session, page, author, args.reason, rel)
            return f"Doc page {page.page_key}{_in(rel)} confirmed as current."
        versions = await docs_service.count_versions(ctx.session, page.page_key)
        page = await docs_service.update_page(
            ctx.session, page, author, title=args.title, body=args.body, move=args.move, parent_id=args.parent or None, position=args.position,
            slug=args.slug, paths=args.paths, reason=args.reason, rel=rel,
        )  # fmt: skip
        changed = await docs_service.count_versions(ctx.session, page.page_key) > versions
        done = f"Doc page {page.page_key} /{page.path}{_in(rel)} {'saved as a new version' if changed else 'updated (text unchanged)'}."
    return done


class ManageDocSiteArgs(BaseModel):
    site: str | None = Field(default=None, description="Site slug/id to change; empty = a new site (with its own doc agent)")
    name: str | None = Field(default=None, max_length=200)
    slug: str | None = Field(default=None, max_length=80, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", description="Its address /docs/<slug>/")
    description: str | None = Field(default=None, max_length=300, description="One sentence for search engines")
    spec: str | None = Field(default=None, max_length=20000, description="Doc spec agreed with the user: content, structure, tone, audience (Markdown, whole text)")
    published: bool | None = Field(default=None, description="Served at all (else a draft); only when the user wants it")
    public: bool | None = Field(default=None, description="Served to everyone without login; else only to people of this project")
    languages: list[str] | None = Field(default=None, max_length=30, description="Further languages (codes), translated automatically")
    translator: Literal["", "llm", "deepl", "google"] | None = Field(default=None, description="How (deepl/google need the API key the user enters in the site's settings)")
    translate_model: str | None = Field(default=None, max_length=100, description="llm: model id; empty = the provider's small one")
    agent_provider: str | None = Field(default=None, max_length=50, description="Its doc agent's provider (list_models); empty = default")
    agent_model: str | None = Field(default=None, max_length=100, description="Its doc agent's model; empty = the default for members")
    agent_effort: Literal["", "low", "medium", "high", "xhigh", "max"] | None = None
    versioned: bool | None = Field(default=None, description="Use versions (manage_doc_version); off = always the current state, versions kept but not shown")
    domain_help: bool = Field(default=False, description="Also return how the user gives it an own domain")


DOMAIN_HELP = (
    "Own domain: the user enters it in the site's settings in PlainBoard (only the owner), then either gives a Cloudflare API token "
    "there (PlainBoard creates the DNS record) or sets a CNAME to this PlainBoard's host name themselves; behind a Cloudflare tunnel "
    "also a public hostname rule to PlainBoard. PlainBoard verifies the domain itself and serves the site there only after that."
)


def _agent_line(site: DocSite, agent) -> str:  # noqa: ANN001
    return f" Its doc agent: {agent.slug} ({agent.model}{', effort ' + agent.effort if agent.effort else ''})." if agent else ""


@tool(
    "manage_doc_site",
    "Create a doc site (it gets its own hidden doc agent, the only one that writes it) or change its settings, doc spec or doc agent's model.",
    ManageDocSiteArgs,
    roles=(MANAGER,),
)
async def manage_doc_site(ctx: ToolContext, args: ManageDocSiteArgs) -> str:
    agent_args = {"agent_provider": args.agent_provider, "agent_model": args.agent_model, "agent_effort": args.agent_effort}
    help_ = f"\n{DOMAIN_HELP}" if args.domain_help else ""
    if args.site is None:
        if not args.name:
            raise ToolError("A new site needs a name.")
        site = await docs_service.create_site(
            ctx.session, ctx.project.id, name=args.name, slug=args.slug, description=args.description or "", spec=args.spec or "", published=bool(args.published),
            public=bool(args.public), languages=args.languages or [], versioned=bool(args.versioned), **agent_args,
        )  # fmt: skip
        if args.translator or args.translate_model:
            site = await docs_service.update_site(ctx.session, site, translator=args.translator, translate_model=args.translate_model)
        hint = " Write its doc spec with the user, then order the first version with order_docs." if not site.spec else " Order the first version with order_docs."
        served = ("public" if site.public else "private") if site.published else "draft"
        agent = await docs_service.doc_agent(ctx.session, site.id)
        return f"Created doc site {site.slug} (id {site.id}, /docs/{site.slug}/, {served}).{_agent_line(site, agent)}{hint}{help_}"
    site = await docs_service.find_site(ctx.session, ctx.project.id, args.site)
    site = await docs_service.update_site(ctx.session, site, **args.model_dump(exclude={"site", "domain_help"}, exclude_none=True))
    served = ("public" if site.public else "private") if site.published else "draft"
    agent = await docs_service.doc_agent(ctx.session, site.id)
    langs = "; languages " + ", ".join(site.languages) if site.languages else ""
    return f"Doc site {site.slug} saved ({served}{langs}).{_agent_line(site, agent)}{help_}"


class ManageDocVersionArgs(BaseModel):
    site: str = Field(description="Site slug/id")
    version: str = Field(min_length=1, max_length=80, description="Version name, e.g. 1.0 (the working version: next)")
    cut: bool = Field(default=False, description="Cut this new version from the working version as it is now (when the user wants a release)")
    notes: str | None = Field(default=None, max_length=5000, description="What the version is (release notes)")
    rename: str | None = Field(default=None, max_length=80, description="New name")
    deprecated: bool | None = Field(default=None, description="Still served, marked as outdated")
    hidden: bool | None = Field(default=None, description="Not served (the working version: not public)")
    default: bool | None = Field(default=None, description="What visitors get at the site's address; false = the latest released one")
    delete: bool = Field(default=False, description="Delete this released version (only when the user asks)")


def _versions_line(outs) -> str:  # noqa: ANN001
    return "Versions: " + ", ".join(
        f"{r.name} ({', '.join(x for x in ('working' if r.working else '', 'default' if r.default else '', 'deprecated' if r.deprecated else '', 'hidden' if r.hidden else '') if x) or 'released'}; {r.url})"
        for r in outs
    )


@tool(
    "manage_doc_version",
    "Versions of a doc site: cut one from the working version (e.g. at a release), rename, deprecate, hide, make default or delete one.",
    ManageDocVersionArgs,
    roles=(MANAGER,),
    feature=DOCS,
)
async def manage_doc_version(ctx: ToolContext, args: ManageDocVersionArgs) -> str:
    site = await docs_service.find_site(ctx.session, ctx.project.id, args.site)
    changes = {"name": args.rename, "notes": args.notes, "deprecated": args.deprecated, "hidden": args.hidden, "default": args.default}
    if args.cut:
        rel = await docs_releases.cut(ctx.session, site, name=args.version, notes=args.notes or "")
        if any(v is not None for k, v in changes.items() if k != "notes"):
            await docs_releases.update(ctx.session, site, rel, **{k: v for k, v in changes.items() if k != "notes"})
        done = f"Cut version {rel.name} of {site.slug}: it keeps the pages as they are now; the doc agent goes on in the working version."
    else:
        rel = await docs_releases.find_any(ctx.session, site, args.version)
        if args.delete:
            name = rel.name
            await docs_releases.delete(ctx.session, site, rel)
            done = f"Deleted version {name} of {site.slug}."
        else:
            await docs_releases.update(ctx.session, site, rel, **changes)
            done = f"Version {rel.name} of {site.slug} saved."
    return f"{done}\n{_versions_line(await docs_releases.outs(ctx.session, site))}"


class OrderDocsArgs(BaseModel):
    site: str = Field(description="Site slug/id")
    title: str = Field(min_length=3, max_length=200, description="Short title of the work order")
    instructions: str = Field(min_length=10, max_length=20000, description="What to write or change, self-contained (the doc spec it has already)")
    ticket_number: int | None = Field(default=None, description="The ticket this is for, if any")
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


@tool(
    "order_docs",
    "Give a doc site's doc agent a work order (e.g. its first version, a new section). Its report comes back as an update.",
    OrderDocsArgs,
    roles=(MANAGER,),
    feature=DOCS,
)
async def order_docs(ctx: ToolContext, args: OrderDocsArgs) -> str:
    site = await docs_service.find_site(ctx.session, ctx.project.id, args.site)
    agent = await docs_service.ensure_agent(ctx.session, site)
    task = await board_service.get_task_by_number(ctx.session, ctx.project.id, args.ticket_number) if args.ticket_number else None
    open_work = await ctx.session.scalar(
        select(func.count()).select_from(Assignment).where(Assignment.agent_id == agent.id, Assignment.status.in_(board_service.OPEN_ASSIGNMENT_STATUSES))
    )
    assignment = await ctx.runs.delegate(ctx.session, ctx.agent, agent, args.title, args.instructions, task, effort=args.effort)
    after = f" It starts after its {open_work} open one(s)." if open_work else ""
    paused = " It is paused: it starts when enabled again." if not agent.enabled else ""
    return f"Assignment #{assignment.id} queued for the doc agent of {site.slug} ({agent.slug}).{after}{paused}"


class AttachDocFileArgs(BaseModel):
    site: str = Field(description="Your site's slug/id")
    path: str = Field(description="File in your workspace (absolute or relative)")


local_tool("attach_doc_file", "Add a file or image from your workspace to your doc site; kept for good. Returns how to use it in a page.", AttachDocFileArgs, roles=(DOC_AGENT,))
