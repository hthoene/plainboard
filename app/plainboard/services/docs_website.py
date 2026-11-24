"""A doc site's theme from its product's website (#128): what the website's HTML and CSS say about its brand (the
theme-color, CSS variables, frequent colours, fonts, logo and icon candidates), as suggestions for the agent that
builds the theme. What the runner's browser saw on the rendered page (``Seen``) comes along. Logo candidates are
stored as files of the site (kept like every doc file), so the theme can name them.

Only public http(s) addresses (plainboard_protocol.netguard: no localhost, private networks, metadata services),
each redirect checked again, every response bounded in size and time.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx
from plainboard_protocol.netguard import NotPublic, check_url
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DocSite
from . import docs as docs_service
from .errors import ServiceError

TIMEOUT = 10.0
REDIRECTS = 5
HTML_BYTES = 2 * 1024 * 1024
CSS_BYTES = 1024 * 1024
IMAGE_BYTES = 1024 * 1024
STYLESHEETS = 5
LOGOS = 4
COLOR = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b|(?:rgba?|hsla?|oklch|oklab)\([^)]{3,60}\)")
VAR = re.compile(r"(--[\w-]{1,60})\s*:\s*([^;}{]{1,120})")
FAMILY = re.compile(r"font-family\s*:\s*([^;}{]{1,200})", re.IGNORECASE)
FONT_FACE = re.compile(r"@font-face\s*{[^}]*?font-family\s*:\s*([^;}]+)", re.IGNORECASE)
IMPORT = re.compile(r"@import\s+(?:url\()?\s*['\"]?([^'\")\s;]+)", re.IGNORECASE)
BRAND_WORDS = ("brand", "primary", "accent", "main", "theme", "secondary", "highlight", "link", "background", "bg", "foreground", "fg", "text", "surface", "radius", "font")
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/svg+xml": ".svg", "image/webp": ".webp", "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico", "image/gif": ".gif"}


class Seen(BaseModel):
    """What the runner's browser saw on the rendered page (filled in by the runner, never by the agent)."""

    light: dict[str, str] = Field(default_factory=dict, max_length=40)
    dark: dict[str, str] = Field(default_factory=dict, max_length=40)
    logos: list[str] = Field(default_factory=list, max_length=10)
    error: str = Field(default="", max_length=300)


@dataclass
class Found:
    url: str
    title: str = ""
    theme_colors: list[str] = field(default_factory=list)  # meta theme-color (with its media)
    stylesheets: list[str] = field(default_factory=list)
    font_links: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)  # inline <style>
    logos: list[tuple[str, str]] = field(default_factory=list)  # (address, what it is)


class _Page(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.found = Found(url=base)
        self._in: str | None = None

    def _add_logo(self, url: str | None, what: str) -> None:
        if url and not url.startswith("data:") and all(u != url for u, _ in self.found.logos):
            self.found.logos.append((urljoin(self.found.url, url), what))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): v or "" for k, v in attrs}
        if tag == "base" and a.get("href"):
            self.found.url = urljoin(self.found.url, a["href"])
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name == "theme-color" and a.get("content"):
                self.found.theme_colors.append(a["content"] + (f" ({a['media']})" if a.get("media") else ""))
            elif name in ("og:image", "twitter:image"):
                self._add_logo(a.get("content"), name)
        elif tag == "link":
            rel = (a.get("rel") or "").lower().split()
            href = a.get("href")
            if not href:
                return
            if "stylesheet" in rel:
                absolute = urljoin(self.found.url, href)
                (self.found.font_links if _font_link(absolute) else self.found.stylesheets).append(absolute)
            elif {"icon", "apple-touch-icon", "mask-icon"} & set(rel):
                self._add_logo(href, " ".join(rel) + (f" {a['sizes']}" if a.get("sizes") else ""))
        elif tag == "img":
            words = " ".join(a.get(k, "") for k in ("src", "alt", "class", "id")).lower()
            if "logo" in words or "brand" in words:
                self._add_logo(a.get("src"), f"img alt={a.get('alt', '')[:40]!r}")
        elif tag in ("style", "title"):
            self._in = tag

    def handle_endtag(self, tag: str) -> None:
        if tag == self._in:
            self._in = None

    def handle_data(self, data: str) -> None:
        if self._in == "style":
            self.found.styles.append(data[:200_000])
        elif self._in == "title" and not self.found.title:
            self.found.title = " ".join(data.split())[:120]


def _font_link(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host in ("fonts.googleapis.com", "fonts.bunny.net", "use.typekit.net") or "/css2?family=" in url


async def _checked(url: str) -> None:
    try:
        await asyncio.to_thread(check_url, url)
    except NotPublic as exc:
        raise ServiceError(f"{url}: {exc}. Only public http(s) websites.") from exc


async def fetch(client: httpx.AsyncClient, url: str, limit: int) -> tuple[str, str, bytes]:
    """(final address, content type, body) of a public address; redirects followed and checked each, ``limit`` bytes at most."""
    for _ in range(REDIRECTS + 1):
        await _checked(url)
        async with client.stream("GET", url) as resp:
            if resp.is_redirect and (location := resp.headers.get("location")):
                url = urljoin(url, location)
                continue
            if resp.status_code >= 400:
                raise ServiceError(f"{url} answered {resp.status_code}.")
            if int(resp.headers.get("content-length") or 0) > limit:
                raise ServiceError(f"{url} is larger than {limit // 1024} KB.")
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > limit:
                    raise ServiceError(f"{url} is larger than {limit // 1024} KB.")
            return url, resp.headers.get("content-type", "").split(";")[0].strip().lower(), body
    raise ServiceError(f"{url}: too many redirects.")


def client() -> httpx.AsyncClient:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PlainBoard docs theme)", "Accept": "text/html,text/css,image/*;q=0.9,*/*;q=0.5"}
    return httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT), follow_redirects=False, headers=headers, trust_env=False)


def _is_color(value: str) -> bool:
    return bool(COLOR.fullmatch(value.strip())) or value.strip().lower() in ("white", "black")


def analyse(css: str) -> dict:
    """What a site's CSS says: its colour and font variables, its most frequent colours and fonts, web fonts."""
    variables: dict[str, str] = {}
    for name, value in VAR.findall(css):
        value, lower = value.strip(), name.lower()
        if name not in variables and not value.startswith("var(") and (_is_color(value) or lower.startswith("--font") or "radius" in lower):
            variables[name] = value
    branded = {k: v for k, v in variables.items() if any(w in k.lower() for w in BRAND_WORDS)}
    colors = Counter(c.lower() for c in COLOR.findall(css))
    families = Counter(re.sub(r"\s*!important$", "", " ".join(f.split())) for f in FAMILY.findall(css) if "var(" not in f and "inherit" not in f)
    faces = list(dict.fromkeys(f.strip().strip("'\"") for f in FONT_FACE.findall(css)))
    return {"variables": dict(list(branded.items())[:30]), "colors": colors.most_common(12), "families": families.most_common(6), "faces": faces[:10], "imports": IMPORT.findall(css)[:10]}


async def _store_logos(session: AsyncSession, site: DocSite, web: httpx.AsyncClient, candidates: list[tuple[str, str]], agent_id: int | None) -> list[str]:
    out: list[str] = []
    for url, what in candidates[: LOGOS * 2]:
        if len(out) >= LOGOS:
            break
        try:
            final, kind, body = await fetch(web, url, IMAGE_BYTES)
        except (ServiceError, httpx.HTTPError) as exc:
            out.append(f"- {url} ({what}): not loaded, {exc}")
            continue
        if kind not in IMAGE_TYPES or not body:
            continue
        name = PurePosixPath(urlsplit(final).path).name or "logo"
        if not name.lower().endswith(IMAGE_TYPES[kind]):
            name += IMAGE_TYPES[kind]
        attachment = await docs_service.add_file(session, site, name[:120], kind, body, agent_id=agent_id)
        out.append(f"- file {attachment.id} {attachment.filename} ({what}, {len(body) // 1024 or 1} KB)")
    return out


def _line(label: str, values: dict[str, str]) -> str:
    return f"{label}: " + ", ".join(f"{k} {v}" for k, v in values.items() if v) if any(values.values()) else ""


async def suggest(session: AsyncSession, site: DocSite, url: str, seen: Seen | None, agent_id: int | None = None) -> str:
    """The website's brand as text for the agent; its logo candidates stored as files of ``site``."""
    if not re.match(r"^https?://", url.strip(), re.IGNORECASE):
        url = "https://" + url.strip()
    async with client() as web:
        final, kind, body = await fetch(web, url, HTML_BYTES)
        if "html" not in kind:
            raise ServiceError(f"{final} is not a web page ({kind or 'unknown type'}).")
        parser = _Page(final)
        parser.feed(body.decode("utf-8", errors="replace"))
        found = parser.found
        css = list(found.styles)
        notes: list[str] = []
        for sheet in found.stylesheets[:STYLESHEETS]:
            try:
                css.append((await fetch(web, sheet, CSS_BYTES))[2].decode("utf-8", errors="replace"))
            except (ServiceError, httpx.HTTPError) as exc:
                notes.append(f"stylesheet {sheet} not loaded: {exc}")
        facts = analyse("\n".join(css))
        candidates = sorted(found.logos, key=lambda c: 0 if c[1].startswith("img") else 2 if "image" in c[1] else 1)  # a logo, an icon, a share picture
        for logo in (seen.logos if seen else []):
            if logo.startswith(("http://", "https://")) and all(u != logo for u, _ in candidates):
                candidates.insert(0, (logo, "rendered logo"))
        candidates.append((urljoin(final, "/favicon.ico"), "favicon.ico"))
        stored = await _store_logos(session, site, web, candidates, agent_id)
    fonts = [link for link in found.font_links + [i for i in facts["imports"] if _font_link(i)] if link.startswith("https://")]
    lines = [f"Website {final}" + (f' "{found.title}"' if found.title else "") + ": suggestions for the theme of " + site.slug]
    if found.theme_colors:
        lines.append("theme-color: " + "; ".join(found.theme_colors[:4]))
    if seen:
        for label, values in (("Rendered, light", seen.light), ("Rendered, dark (prefers-color-scheme)", seen.dark)):
            if line := _line(label, values):
                lines.append(line)
        if seen.error:
            lines.append(f"(browser: {seen.error})")
    if facts["variables"]:
        lines.append("CSS variables: " + "; ".join(f"{k}: {v}" for k, v in facts["variables"].items()))
    if facts["colors"]:
        lines.append("Frequent colours: " + ", ".join(f"{c} ({n}x)" for c, n in facts["colors"]))
    if facts["families"] or facts["faces"]:
        lines.append("Fonts: " + "; ".join(f"{f} ({n}x)" for f, n in facts["families"]) + (f" | @font-face: {', '.join(facts['faces'])}" if facts["faces"] else ""))
    if fonts:
        lines.append("Web font stylesheets (font_url): " + ", ".join(dict.fromkeys(fonts[:4])))
    lines.append("Logos and icons stored as files of the site (logo_light/logo_dark/favicon take the id):" if stored else "No logo or icon found.")
    lines += stored
    lines += notes[:3]
    lines.append("Pick colours that read well in light and dark, set the theme with doc_theme and check it with preview_doc_site.")
    return "\n".join(lines)
