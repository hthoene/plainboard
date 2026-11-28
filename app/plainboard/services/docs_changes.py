"""What a change may have made wrong in the docs, found without a model (#190).

A change (a landing's commits, a new tag of a repository, a decision, a ticket's file) is cut into small pieces:
- its changed literals: the diff's hunks give old -> new pairs of notable tokens (versions, numbers, config keys,
  flags, identifiers, file names); a tag gives the previous tag -> the new one, with the variants docs write
  (1.2.0, v1.2.0, "beta 2"). Every page of the working version is searched for the OLD value (``mentions``): the line
  and its section are what the doc agent gets, with the reason;
- its texts (commit messages, hunks, a decision): searched by meaning among the doc pages' sections (``near``).
The doc agent's own tools are here too: ``grep`` (exact or regex over all pages), ``sections`` (by meaning, per
section), ``changes_since`` (commits, tags and decisions since a page was confirmed) and ``releases``.
"""

from __future__ import annotations

import fnmatch
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Decision, DecisionStatus, DocPage, DocSite, Landing, LandingStatus, Project, RepoTag, Repository, SearchDocument, Task
from .errors import ServiceError
from .search.documents import _ATX, _FENCE_LINE, clean
from .util import truncate

TOKENS = 40  # old values searched per change
HUNKS = 8  # hunks of a change searched by meaning
HUNK_CHARS = 900
DELETE_LINES = 10  # a hunk that removes more is moved or dropped code: its removed tokens are not searched
SCANNED = 5000  # pages read by one grep
LINE_CHARS = 200

_TOKEN = re.compile(r"--?[A-Za-z][\w-]*|\w[\w.+\-/@]*")
VERSION = re.compile(r"^[vV]?\d+(?:\.\d+)+(?:-[0-9A-Za-z.\-]+)?(?:\+[0-9A-Za-z.\-]+)?$")
_HASH = re.compile(r"^[0-9a-f]{7,64}$")
_PRE = re.compile(r"^([A-Za-z]+)[.\-]?(\d+)$")
_TICKET = re.compile(r"#(\d{1,7})\b")


def matches(path: str, patterns: list[str]) -> bool:
    """A path under a prefix or matching a glob (a doc page's code paths)."""
    for pattern in patterns:
        pattern = pattern.strip().lstrip("./")
        if not pattern:
            continue
        if any(ch in pattern for ch in "*?["):
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.rstrip("/") + "/*"):
                return True
        elif path == pattern or path.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


# --- changed literals ----------------------------------------------------------------------------------------------


@dataclass
class Hunk:
    path: str
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted_file: bool = False

    def text(self) -> str:
        lines = [f"- {line.strip()}" for line in self.removed if line.strip()] + [f"+ {line.strip()}" for line in self.added if line.strip()]
        return truncate(f"{self.path}\n" + "\n".join(lines), HUNK_CHARS)


def hunks(diff: str) -> list[Hunk]:
    """The hunks of a unified diff (-U0)."""
    out: list[Hunk] = []
    path, deleted, current, in_hunk = "", False, None, False
    for line in (diff or "").split("\n"):
        if line.startswith("diff --git "):
            in_hunk, current, deleted = False, None, False
            path = line.rsplit(" b/", 1)[-1] if " b/" in line else ""
        elif not in_hunk and line.startswith("deleted file"):
            deleted = True
        elif not in_hunk and line.startswith("+++ "):
            target = line[4:].strip()
            if target != "/dev/null":
                path = target.removeprefix("b/")
        elif line.startswith("@@"):
            in_hunk = True
            current = Hunk(path, deleted_file=deleted)
            out.append(current)
        elif in_hunk and current is not None:
            if line.startswith("-"):
                current.removed.append(line[1:])
            elif line.startswith("+"):
                current.added.append(line[1:])
    return [h for h in out if h.removed or h.added]


def notable(token: str) -> bool:
    """A token docs may name and that is not everywhere: a version, a longer number, a flag, a key or identifier
    with separators or inner capitals, a file name. Plain words are left to the search by meaning."""
    if not 3 <= len(token) <= 120 or _HASH.match(token):
        return False
    if token.startswith("-"):
        return len(token) >= 4
    if VERSION.match(token):
        return True
    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    if token.isdigit():
        return len(token) >= 3
    if any(c in token for c in "_-./"):
        return len(token) >= 4 and (has_alpha or has_digit)
    if re.search(r"[a-z][A-Z]", token):
        return len(token) >= 5
    return has_digit and has_alpha


def _tokens(lines: list[str]) -> list[str]:
    return [t.rstrip(".-/@+") for line in lines for t in _TOKEN.findall(line)]


def changed_tokens(parts: list[Hunk]) -> list[tuple[str, str | None]]:
    """(old, new) of the notable tokens these hunks changed (new None: removed), the replaced ones first. A token
    still written anywhere in the added lines is not changed (code that moved)."""
    added_all = {t for h in parts for t in _tokens(h.added)}
    pairs: list[tuple[str, str | None]] = []
    removed: list[tuple[str, None]] = []
    for h in parts:
        if h.deleted_file:
            continue
        old, new = _tokens(h.removed), _tokens(h.added)
        for op, i1, i2, j1, j2 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if op == "replace":
                for k, token in enumerate(old[i1:i2]):
                    other = new[j1 + k] if j1 + k < j2 else None
                    if token in added_all or not notable(token):
                        continue
                    (pairs.append((token, other)) if other is not None else removed.append((token, None)))
            elif op == "delete" and len(h.removed) <= DELETE_LINES:
                removed += [(t, None) for t in old[i1:i2] if t not in added_all and notable(t)]
    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []
    for token, other in [*pairs, *removed]:
        if token not in seen:
            seen.add(token)
            out.append((token, other))
    return out[:TOKENS]


@dataclass
class Probe:
    """One old value searched for in the pages."""

    pattern: re.Pattern[str]
    literal: str  # lower case, in every match: the database narrows the pages with it
    old: str
    new: str | None
    where: str  # the change it comes from
    new_pattern: re.Pattern[str] | None = None  # a line that names the new value too is right already

    def says(self) -> str:
        return f'mentions "{self.old}"' + (f', now "{self.new}"' if self.new else ", which is gone") + f" ({self.where})"


def _exact(token: str) -> re.Pattern[str]:
    v = "[vV]?" if token[:1].isdigit() else ""  # 1.2.0 is also written v1.2.0
    return re.compile(r"(?<![\w.\-])" + v + re.escape(token) + r"(?![\w]|[.\-]\w)")


def _pre(label: str, number: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w])" + re.escape(label) + r"[ .\-]?" + number + r"(?![\w]|\.\d)", re.I)


def _version_parts(v: str) -> tuple[str, str, str]:
    """(release, prerelease, core without build) of a version: 0.1.0-beta.2+mc26.3 -> (0.1.0, beta.2, 0.1.0-beta.2)."""
    core = v[1:] if v[:1] in "vV" and v[1:2].isdigit() else v
    plain = core.split("+", 1)[0]
    release, _, pre = plain.partition("-")
    return release, pre, plain


def probes(pairs: list[tuple[str, str | None]], where: str) -> list[Probe]:
    """What to search for: each old value exactly; of a version also how docs write it (without the v, without the
    build, its release, its prerelease like "Beta 2"), where that part changed."""
    out: list[Probe] = []
    for old, new in pairs:
        out.append(Probe(_exact(old), old.lower(), old, new, where, _exact(new) if new else None))
        if not VERSION.match(old) or (new and not VERSION.match(new)):
            continue
        o_rel, o_pre, o_plain = _version_parts(old)
        n_rel, n_pre, n_plain = _version_parts(new) if new else ("", "", "")
        for o, n in ((o_plain, n_plain), (o_rel, n_rel)):
            if o and o != old and o != n and "." in o:
                out.append(Probe(_exact(o), o.lower(), o, n or None, where, _exact(n) if n else None))
        if o_pre and o_pre != n_pre and (m := _PRE.match(o_pre)):
            nm = _PRE.match(n_pre) if n_pre else None
            label = f"{m.group(1)} {m.group(2)}"
            out.append(Probe(_pre(m.group(1), m.group(2)), m.group(1).lower(), label, f"{nm.group(1)} {nm.group(2)}" if nm else n_pre or None, where, _pre(nm.group(1), nm.group(2)) if nm else None))
    unique: dict[str, Probe] = {}
    for p in out:
        unique.setdefault(p.pattern.pattern, p)
    return list(unique.values())


# --- the pages ----------------------------------------------------------------------------------------------------


def _section_lines(body: str):  # noqa: ANN202
    """(line number from 1, the heading path above it, the line) of a Markdown text."""
    trail: list[str] = []
    fenced = False
    for n, line in enumerate((body or "").replace("\r\n", "\n").split("\n"), 1):
        if _FENCE_LINE.match(line):
            fenced = not fenced
        heading = None if fenced else _ATX.match(line)
        if heading is not None:
            level = len(heading.group(1))
            trail = [*trail[: level - 1], clean(heading.group(2)).replace("\n", " ")[:120]]
        yield n, " › ".join(trail), line


@dataclass
class Mention:
    page: DocPage
    line: int
    section: str
    text: str
    probe: Probe | None = None

    def place(self) -> str:
        where = f' in "{self.section}"' if self.section else ""
        return f"line {self.line}{where}: {truncate(self.text.strip(), LINE_CHARS)}"


def working():  # noqa: ANN201
    return DocPage.until_seq.is_(None)


async def mentions(s: AsyncSession, project_id: int, found: list[Probe], per_page: int = 3) -> dict[int, list[Mention]]:
    """The lines of the working version's pages (not those written by people only) that name an old value, by page."""
    if not found:
        return {}
    literals = sorted({p.literal for p in found})
    lowered = func.lower(DocPage.body)
    query = select(DocPage).where(DocPage.project_id == project_id, working(), DocPage.human_only.is_(False), or_(*[lowered.contains(lit, autoescape=True) for lit in literals]))
    out: dict[int, list[Mention]] = {}
    for page in await s.scalars(query.order_by(DocPage.id).limit(SCANNED)):
        low = page.body.lower()
        mine = [p for p in found if p.literal in low]
        for n, section, line in _section_lines(page.body):
            for probe in mine:
                if probe.pattern.search(line) and not (probe.new_pattern and probe.new_pattern.search(line)):
                    out.setdefault(page.id, []).append(Mention(page, n, section, line, probe))
                    break
            if len(out.get(page.id, [])) >= per_page:
                break
    return out


async def grep(s: AsyncSession, project_id: int, site_id: int | None, pattern: str, regex: bool = False, case: bool = False, limit: int = 30) -> tuple[list[Mention], int]:
    """Lines of the working version's pages that match: exact text, or a regular expression. With how many lines
    matched in all (the first ``limit`` are returned)."""
    try:
        compiled = re.compile(pattern if regex else re.escape(pattern), 0 if case else re.I)
    except re.error as exc:
        raise ServiceError(f"Not a regular expression: {exc}") from exc
    query = select(DocPage).where(DocPage.project_id == project_id, working())
    if site_id is not None:
        query = query.where(DocPage.site_id == site_id)
    if not regex:
        query = query.where((DocPage.body if case else func.lower(DocPage.body)).contains(pattern if case else pattern.lower(), autoescape=True))
    found: list[Mention] = []
    total = 0
    for page in await s.scalars(query.order_by(DocPage.site_id, DocPage.path).limit(SCANNED)):
        for n, section, line in _section_lines(page.body):
            if compiled.search(line):
                total += 1
                if len(found) < limit:
                    found.append(Mention(page, n, section, line))
    return found, total


# --- by meaning ---------------------------------------------------------------------------------------------------


@dataclass
class Near:
    page: DocPage
    section: str  # the start of the section's chunk: its heading path, then its text
    similarity: float


async def near(s: AsyncSession, project_id: int, text: str, limit: int, threshold: float = -1.0, site_id: int | None = None) -> list[Near]:
    """The doc page sections closest in meaning to a text (working version; with search only, else none)."""
    from .search import embedder, store

    model = await embedder(s)
    if model.state != "ready" or not text.strip():
        return []
    chunks = await store.nearest_chunks(s, project_id, await model.query(text), model.name, "doc", limit * 3)
    ids = {int(c.source_id) for c in chunks if c.source_id.isdigit() and c.similarity >= threshold}
    if not ids:
        return []
    query = select(DocPage).where(DocPage.id.in_(ids), DocPage.project_id == project_id, working())
    if site_id is not None:
        query = query.where(DocPage.site_id == site_id)
    pages = {p.id: p for p in await s.scalars(query)}
    out = []
    for c in chunks:
        page = pages.get(int(c.source_id)) if c.source_id.isdigit() else None
        if page is not None and c.similarity >= threshold:
            out.append(Near(page, truncate(" ".join(c.body.split()), 140), c.similarity))
    return out[:limit]


# --- what changed since -------------------------------------------------------------------------------------------


async def releases(s: AsyncSession, project_id: int, repo: str | None = None, since: datetime | None = None, limit: int = 20) -> list[tuple[str, RepoTag]]:
    """The repositories' tags as last read, newest first: (repository name, tag)."""
    query = select(Repository.name, RepoTag).join(Repository, Repository.id == RepoTag.repository_id).where(RepoTag.project_id == project_id)
    if repo:
        query = query.where(Repository.name == repo)
    if since is not None:
        query = query.where(RepoTag.tagged_at >= since)
    rows = await s.execute(query.order_by(RepoTag.tagged_at.desc().nulls_last(), RepoTag.id.desc()).limit(limit))
    return [(name, tag) for name, tag in rows.all()]


def release_line(repo: str, tag: RepoTag) -> str:
    when = f"{tag.tagged_at:%Y-%m-%d}" if tag.tagged_at else "?"
    return f"- {repo} {tag.name} ({when}, {tag.sha[:10]})" + (f": {truncate(tag.subject, 120)}" if tag.subject else "")


async def changes_since(s: AsyncSession, project: Project, paths: list[str], since: datetime, limit: int = 20) -> str:
    """Commits that touched these paths (all, without paths), tags, and decisions on the tickets those commits name."""
    from .search import available

    lines: list[str] = []
    tickets: set[int] = set()
    if await available(s):
        rows = (
            await s.execute(
                select(SearchDocument.ref, SearchDocument.title, SearchDocument.body, SearchDocument.at, SearchDocument.repo, SearchDocument.paths)
                .where(SearchDocument.project_id == project.id, SearchDocument.kind == "commit", SearchDocument.chunk == 0, SearchDocument.at >= since)
                .order_by(SearchDocument.at.desc())
                .limit(2000)
            )
        ).all()
        hits = [r for r in rows if not paths or any(matches(f, paths) for f in r.paths or [])]
        for r in hits:
            tickets |= {int(n) for n in _TICKET.findall(f"{r.title}\n{r.body}")}
        scope = f" touching {', '.join(paths[:6])}" if paths else ""
        lines.append(f"Commits{scope} since {since:%Y-%m-%d}: {len(hits)}" + ("" if hits else " (none)"))
        lines += [f"- {r.at:%Y-%m-%d} {r.repo} {r.ref} {truncate(r.title, 120)}" for r in hits[:limit]]
        if len(hits) > limit:
            lines.append(f"(and {len(hits) - limit} older)")
    else:
        landed = (
            await s.execute(
                select(Landing.branch, Landing.merged_at, Task.number, Task.title)
                .outerjoin(Task, Task.id == Landing.task_id)
                .where(Landing.project_id == project.id, Landing.status == LandingStatus.MERGED, Landing.merged_at >= since)
                .order_by(Landing.merged_at.desc())
                .limit(limit)
            )
        ).all()
        lines.append(f"Landed since {since:%Y-%m-%d} (commits by path need the search index, not here): {len(landed)}" + ("" if landed else " (none)"))
        lines += [f"- {r.merged_at:%Y-%m-%d} {r.branch}" + (f" #{r.number} {truncate(r.title, 100)}" if r.number else "") for r in landed]
    tags = await releases(s, project.id, since=since, limit=limit)
    lines.append(f"Releases (tags) since {since:%Y-%m-%d}: {len(tags)}" + ("" if tags else " (none)"))
    lines += [release_line(repo, tag) for repo, tag in tags]
    if tickets:
        decided = (
            await s.execute(
                select(Task.number, Decision.question, Decision.answer_text, Decision.options, Decision.answer_option)
                .join(Task, Task.id == Decision.task_id)
                .where(Decision.project_id == project.id, Task.number.in_(sorted(tickets)[:200]), Decision.status == DecisionStatus.ANSWERED, Decision.answered_at >= since)
                .order_by(Decision.answered_at.desc())
                .limit(10)
            )
        ).all()
        if decided:
            lines.append("Decisions on their tickets:")
            lines += [f"- #{r.number} {truncate(r.question, 120)} -> {truncate(decision_answer(r.options, r.answer_option, r.answer_text), 160)}" for r in decided]
    return "\n".join(lines)


def decision_answer(options: list, option: int | None, text: str) -> str:
    label = options[option].get("label", "") if option is not None and 0 <= option < len(options or []) and isinstance(options[option], dict) else ""
    return " ".join(x for x in (label, text) if x).strip() or "answered"


# --- the daily rolling check --------------------------------------------------------------------------------------

MIN_AGE = timedelta(days=14)  # a page confirmed or edited more recently is not checked
PAGE_OVERHEAD = 50  # tokens of the line naming it and the tool call
CHECKED = 8  # pages a day at most


@dataclass
class Risky:
    page_id: int
    tokens: int
    score: float
    why: str


async def risky(s: AsyncSession, site: DocSite, now: datetime, due_before: datetime) -> list[Risky]:
    """The pages most likely outdated, within the site's daily budget: the longest unconfirmed, the more so the more
    commits changed their code since (drift) and releases came since. Not those marked already (they are named
    anyway), reminded lately, written by people only, or confirmed within ``MIN_AGE``."""
    budget = site.check_budget or 0
    if budget <= 0:
        return []
    rows = (
        await s.execute(
            select(DocPage.id, DocPage.edited_at, DocPage.drift, func.length(DocPage.body).label("size")).where(
                DocPage.site_id == site.id, working(), DocPage.human_only.is_(False), DocPage.flagged_at.is_(None),
                (DocPage.reminded_at.is_(None)) | (DocPage.reminded_at < due_before), DocPage.edited_at < now - MIN_AGE,
            )
        )
    ).all()  # fmt: skip
    if not rows:
        return []
    tags = sorted(t for t in await s.scalars(select(RepoTag.tagged_at).where(RepoTag.project_id == site.project_id, RepoTag.tagged_at.is_not(None))))
    scored = []
    for r in rows:
        released = len(tags) - bisect_right(tags, r.edited_at)
        days = (now - r.edited_at).total_seconds() / 86400
        why = [f"unconfirmed since {r.edited_at:%Y-%m-%d} ({int(days)} days)"]
        if r.drift:
            why.append(f"{r.drift} commit{'s' if r.drift != 1 else ''} changed its code since")
        if released:
            why.append(f"{released} release{'s' if released != 1 else ''} since")
        scored.append(Risky(r.id, (r.size or 0) // 4 + PAGE_OVERHEAD, days * (1 + r.drift) * (1 + released), ", ".join(why)))
    scored.sort(key=lambda x: (-x.score, x.page_id))
    picked, left = [], budget
    for r in scored:
        if r.tokens <= left:
            picked.append(r)
            left -= r.tokens
        if len(picked) >= CHECKED or left < PAGE_OVERHEAD * 2:
            break
    return picked
