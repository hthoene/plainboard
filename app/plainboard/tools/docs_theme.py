"""A doc site's theme by agents (#128, services/docs_theme.py): the manager and the site's doc agent read it, change it
(the same checks as the settings), go back to an earlier version; they look at the result with preview_doc_site
(screenshots taken by the runner's browser, returned as pictures) and start from the product's website with
theme_from_website (services/docs_website.py).

Both of the latter are local: the runner calls the app's half first (this module: which pages, with what theme;
the website's HTML and CSS, the logos stored), then does what needs its browser.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from pydantic.json_schema import SkipJsonSchema

from ..models import DocSite
from ..schemas import DocTheme
from ..services import docs as docs_service
from ..services import docs_public, docs_theme, docs_website
from ..services.util import truncate
from .registry import DOC_AGENT, DOCS, MANAGER, ToolContext, ToolError, tool

ROLES = (MANAGER, DOC_AGENT)
MAX_SHOTS = 8
HISTORY = 15


async def _site(ctx: ToolContext, ref: str) -> DocSite:
    site = await docs_service.find_site(ctx.session, ctx.project.id, ref)
    if not ctx.agent.is_manager and ctx.agent.doc_site_id != site.id:
        raise ToolError(f"Only the manager and the doc agent of {site.slug} change its theme.")
    return site


def _theme(data: dict) -> DocTheme:
    try:
        return DocTheme.model_validate(data)
    except ValidationError as exc:
        raise ToolError("Invalid theme: " + "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())) from exc


def _shown(theme: dict) -> str:
    if not theme:
        return "none (the default look)"
    default = DocTheme().model_dump()
    return json.dumps({k: v for k, v in theme.items() if v != default.get(k)}, ensure_ascii=False, separators=(",", ":"))


class DocThemeArgs(BaseModel):
    site: str = Field(description="Site slug/id")
    action: Literal["show", "set", "reset", "undo", "history", "restore"] = Field(
        default="show", description="set: theme over the current one; reset: the default look; undo: back to before the last change; restore: a version"
    )
    theme: dict[str, Any] | None = Field(
        default=None,
        description=(
            "set: fields to change (null = default). logo_light, logo_dark, favicon: doc file ids (images); show_name; light/dark: {accent, page, surface, text} "
            "as #hex; font_family (CSS list); font_url (https stylesheet); radius (0-28 px); links: [{label, url}] (max 6); css (custom, last)"
        ),
    )
    replace: bool = Field(default=False, description="set: the whole theme instead of a change")
    version: int | None = Field(default=None, description="restore: version id (history)")
    reason: str = Field(default="", max_length=300, description="Why, short (kept with the version)")


@tool(
    "doc_theme",
    "A doc site's theme (logos, colours light/dark, font, corners, header links, CSS): show, set, reset, undo, history, restore. Every change is a version. "
    "Check changes with preview_doc_site.",
    DocThemeArgs,
    roles=ROLES,
    feature=DOCS,
)
async def doc_theme(ctx: ToolContext, args: DocThemeArgs) -> str:
    site = await _site(ctx, args.site)
    current = site.theme or {}
    if args.action == "show":
        return f"Theme of {site.slug}: {_shown(current)}"
    if args.action == "history":
        rows = await docs_theme.versions(ctx.session, site.id, HISTORY)
        if not rows:
            return f"Theme of {site.slug}: no versions yet."
        lines = [f"Theme versions of {site.slug}, newest first (the first is the current one):"]
        lines += [f"- {v.id} {v.created_at:%Y-%m-%d %H:%M} by {v.author or '?'}: {v.reason} | {truncate(_shown(v.theme), 200)}" for v in rows]
        return "\n".join(lines)
    if args.action == "set":
        if args.theme is None:
            raise ToolError("set needs theme.")
        new = args.theme if args.replace else docs_theme.patch(current, args.theme)
    elif args.action == "reset":
        new = {}
    elif args.action == "undo":
        new = await docs_theme.previous(ctx.session, site)
    else:
        if args.version is None:
            raise ToolError("restore needs version (see action=history).")
        new = (await docs_theme.version(ctx.session, site, args.version)).theme
    reason = args.reason or {"undo": "undo", "restore": f"restored version {args.version}", "reset": "reset"}.get(args.action, "changed")
    site = await docs_service.update_site(ctx.session, site, theme=_theme(new).model_dump(), author=docs_service.Author(ctx.agent.name, ctx.agent.id), reason=reason)
    if (site.theme or {}) == current:
        return f"Theme of {site.slug} unchanged: {_shown(current)}"
    return f"Theme of {site.slug} saved as a new version: {_shown(site.theme or {})}"


class PreviewDocSiteArgs(BaseModel):
    site: str = Field(description="Site slug/id")
    pages: list[str] = Field(default_factory=lambda: [""], max_length=4, description="Page paths ('' = home, e.g. 'guide/setup'); also unpublished/private sites")
    modes: list[Literal["light", "dark"]] = Field(default_factory=lambda: ["light", "dark"], min_length=1, max_length=2)
    devices: list[Literal["desktop", "phone"]] = Field(default_factory=lambda: ["desktop", "phone"], min_length=1, max_length=2, description="desktop 1440x900, phone 390x844")
    draft: dict[str, Any] | None = Field(default=None, description="A theme change to look at without saving it (as doc_theme set)")
    replace: bool = Field(default=False, description="draft is the whole theme")
    task_number: int | None = Field(default=None, description="Also attach the screenshots to this ticket")


@tool(
    "preview_doc_site",
    f"Screenshots of a doc site as it is served (or with an unsaved draft theme), light/dark, desktop/phone, returned as images (max {MAX_SHOTS}).",
    PreviewDocSiteArgs,
    roles=ROLES,
    local=True,
    feature=DOCS,
)
async def preview_doc_site(ctx: ToolContext, args: PreviewDocSiteArgs) -> str:
    """The app's half: what the runner's browser opens and with which grant (a cookie)."""
    site = await _site(ctx, args.site)
    pages = list(dict.fromkeys(p.strip().strip("/") for p in args.pages)) or [""]
    modes, devices = list(dict.fromkeys(args.modes)), list(dict.fromkeys(args.devices))
    if len(pages) * len(modes) * len(devices) > MAX_SHOTS:
        raise ToolError(f"At most {MAX_SHOTS} screenshots per call: fewer pages, modes or devices.")
    theme = None
    if args.draft is not None:
        draft = args.draft if args.replace else docs_theme.patch(site.theme or {}, args.draft)
        theme = await docs_theme.clean(ctx.session, site, _theme(draft))
    token = docs_public.grant_preview(site.id, theme)
    return json.dumps(
        {"site": site.slug, "paths": [f"/docs/{site.slug}/{p}" for p in pages], "modes": modes, "devices": devices, "cookie": docs_public.PREVIEW_COOKIE, "token": token,
         "draft": theme is not None, "task_number": args.task_number}
    )  # fmt: skip


class ThemeFromWebsiteArgs(BaseModel):
    site: str = Field(description="Site slug/id: logos found are stored as its files")
    url: str = Field(min_length=4, max_length=500, description="The product's public website")
    seen: SkipJsonSchema[docs_website.Seen | None] = None  # what the runner's browser saw, filled in by the runner


@tool(
    "theme_from_website",
    "Read a product website's brand: colours (theme-color, CSS variables, rendered), fonts, logos (stored as doc files) and a screenshot, as theme suggestions.",
    ThemeFromWebsiteArgs,
    roles=ROLES,
    local=True,
    feature=DOCS,
)
async def theme_from_website(ctx: ToolContext, args: ThemeFromWebsiteArgs) -> str:
    site = await _site(ctx, args.site)
    return await docs_website.suggest(ctx.session, site, args.url, args.seen, ctx.agent.id)
