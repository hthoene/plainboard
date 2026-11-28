"""The search index in PostgreSQL: chunks and their words (tsvector), their vectors per model (pgvector)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, exists, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import SearchDocument, SearchVector
from .documents import Doc, prepare

LINKS = ("task_id", "assignment_id", "decision_id", "comment_id", "note_id", "repository_id", "doc_page_id", "attachment_id")  # SearchDocument's sources

# Reciprocal rank fusion: a hit's score is the sum of 1 / (K + its rank) in each list. K = 60 is the usual
# value: the top places count, but a hit that both lists rank fairly high beats one that only one ranks first.
RRF_K = 60
CANDIDATES = 50  # from each list
# Of two hits about equally relevant, the more recent first: at most this much more score for an item that
# changed just now, half of it after RECENCY_HALF_DAYS. A few places, never past a clearly better hit.
RECENCY_BOOST = 0.05
RECENCY_HALF_DAYS = 60

# Words that every text has, in the languages people and agents write here: they find nothing.
STOPWORDS = frozenset(
    "a an and are as at be but by can do does for from has have how i if in is it its not of on or so that the this to was we what when where which who why will with you "
    "aber als am an auch auf aus bei bin bis das dass dem den der des die doch du ein eine einem einen einer es für hat ich im in ist ja kann mit nach nicht noch nur "
    "oder sich sie sind so über um und uns von vor war was wie wir wird zu zum zur".split()
)


async def put(s: AsyncSession, project_id: int, project_name: str, kind: str, items: list[tuple[str, Doc | None]]) -> int:
    """Bring the chunks of these items up to date; returns how many chunks changed (they wait for their vectors)."""
    if not items:
        return 0
    ids = [source_id for source_id, _ in items]
    rows = list(await s.scalars(select(SearchDocument).where(SearchDocument.project_id == project_id, SearchDocument.kind == kind, SearchDocument.source_id.in_(ids))))
    have: dict[tuple[str, int], SearchDocument] = {(r.source_id, r.chunk): r for r in rows}
    changed, stale = 0, []
    for source_id, doc in items:
        chunks = prepare(doc, project_name) if doc else []
        for c in chunks:
            row = have.pop((source_id, c.chunk), None)
            if row is not None and row.content_hash == c.hash:
                row.changed_at = doc.changed or doc.at  # newer, not different: nothing to embed
                continue
            changed += 1
            values = dict(
                ref=doc.ref, title=doc.title[:300], header=c.header, body=c.body, at=doc.at, changed_at=doc.changed or doc.at,
                ticket_number=doc.ticket_number, repo=doc.repo, paths=doc.paths, content_hash=c.hash, **{k: doc.links.get(k) for k in LINKS},
            )  # fmt: skip
            if row is None:
                s.add(SearchDocument(project_id=project_id, kind=kind, source_id=source_id, chunk=c.chunk, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
                stale.append(row.id)
    if stale:  # their text changed: the vectors of every model go, and are made again
        await s.execute(delete(SearchVector).where(SearchVector.document_id.in_(stale)))
    for row in have.values():  # chunks an item no longer has, and items with nothing to find any more
        await s.delete(row)
    return changed


async def remove_missing(s: AsyncSession, project_id: int, kind: str, existing) -> None:
    await s.execute(delete(SearchDocument).where(SearchDocument.project_id == project_id, SearchDocument.kind == kind, SearchDocument.source_id.not_in(existing)))


def _without(model: str):
    return ~exists().where(SearchVector.document_id == SearchDocument.id, SearchVector.model == model)


async def pending(s: AsyncSession, model: str, project_ids: list[int], limit: int, *, kind: str | None = None, source_ids: list[str] | None = None) -> list[tuple[int, int, str, str]]:
    """Chunks without a vector of this model, the newest first: they are the likeliest to be searched for
    (``kind``/``source_ids``: only of these items)."""
    query = select(SearchDocument.id, SearchDocument.project_id, SearchDocument.header, SearchDocument.body, SearchDocument.content_hash)
    query = query.where(SearchDocument.project_id.in_(project_ids), _without(model))
    if kind is not None:
        query = query.where(SearchDocument.kind == kind)
    if source_ids is not None:
        query = query.where(SearchDocument.source_id.in_(source_ids))
    query = query.order_by(SearchDocument.changed_at.desc()).limit(limit)
    return [(r.id, r.project_id, f"{r.header}\n{r.body}", r.content_hash) for r in await s.execute(query)]


async def waiting(s: AsyncSession, model: str, project_ids: list[int]) -> int:
    return int(await s.scalar(select(func.count()).select_from(SearchDocument).where(SearchDocument.project_id.in_(project_ids), _without(model))) or 0)


async def commit_shas(s: AsyncSession, project_id: int, repository_id: int) -> list[str]:
    rows = await s.scalars(select(SearchDocument.source_id).where(SearchDocument.project_id == project_id, SearchDocument.repository_id == repository_id).distinct())
    return [r.partition(":")[2] for r in rows]


async def remove_commits(s: AsyncSession, project_id: int, repository_id: int, shas: list[str]) -> None:
    ids = [f"{repository_id}:{sha}" for sha in shas]
    await s.execute(delete(SearchDocument).where(SearchDocument.project_id == project_id, SearchDocument.kind == "commit", SearchDocument.source_id.in_(ids)))


async def store_vectors(s: AsyncSession, model: str, rows: list[tuple[int, int, str, str]], vectors: list[list[float]]) -> None:
    # Only if the text is still the one embedded: a chunk changed meanwhile waits for its new vector.
    current = set(await s.scalars(select(SearchDocument.id).where(SearchDocument.id.in_([r[0] for r in rows]), SearchDocument.content_hash.in_([r[3] for r in rows]))))
    values = [{"document_id": id_, "model": model, "project_id": pid, "embedding": vector} for (id_, pid, _, _), vector in zip(rows, vectors, strict=True) if id_ in current]
    if values:
        await s.execute(insert(SearchVector), values)


async def keep_only(s: AsyncSession, model: str) -> None:
    """The vectors of every other model go: after a switch, and when a switch is called off."""
    await s.execute(delete(SearchVector).where(SearchVector.model != model))


async def drop_model(s: AsyncSession, model: str) -> None:
    await s.execute(delete(SearchVector).where(SearchVector.model == model))


async def counts(s: AsyncSession, models: list[str]) -> dict:
    """Per kind: the items and chunks indexed and the chunks with a vector of the current model; per model all its vectors."""
    rows = (await s.execute(select(SearchDocument.kind, func.count(func.distinct(SearchDocument.source_id)), func.count()).group_by(SearchDocument.kind))).all()
    embedded = dict(
        (await s.execute(select(SearchDocument.kind, func.count()).join(SearchVector, SearchVector.document_id == SearchDocument.id).where(SearchVector.model == models[0]).group_by(SearchDocument.kind))).all()
    )
    vectors = dict((await s.execute(select(SearchVector.model, func.count()).where(SearchVector.model.in_(models)).group_by(SearchVector.model))).all())
    by_kind = {kind: {"items": int(items), "chunks": int(chunks), "embedded": int(embedded.get(kind, 0))} for kind, items, chunks in rows}
    return {"by_kind": by_kind, "documents": sum(k["chunks"] for k in by_kind.values()), "vectors": {m: int(vectors.get(m, 0)) for m in models}}


def keyword_query(query: str) -> str:
    """A tsquery that finds any of the search's words, also as the start of a longer word (prefix)."""
    words = []
    for word in re.findall(r"[^\W_]+", query.lower()):
        if len(word) >= 2 and word not in STOPWORDS and word not in words:
            words.append(word)
    return " | ".join(f"{w}:*" for w in words[:16])


@dataclass
class Hit:
    kind: str
    source_id: str
    ref: str
    title: str
    body: str
    at: datetime
    changed_at: datetime
    ticket_number: int | None
    repo: str | None
    paths: list[str] | None
    score: float
    similarity: float | None
    vector_rank: int | None = None  # its place by meaning, and by words (None: not among their candidates)
    word_rank: int | None = None


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.6g}" for x in vector) + "]"


_HIT = "d.kind, d.source_id, d.ref, d.title, d.body, d.at, d.changed_at, d.ticket_number, d.repo, d.paths"


def _hit(row, score: float) -> Hit:
    sim = None if row.sim is None else float(row.sim)
    paths = row.paths if isinstance(row.paths, list) or row.paths is None else None
    ranks = getattr(row, "mr", None), getattr(row, "wr", None)
    return Hit(row.kind, row.source_id, row.ref, row.title, row.body, row.at, row.changed_at, row.ticket_number, row.repo, paths, score, sim, *(None if r is None else int(r) for r in ranks))


async def search(
    s: AsyncSession,
    project_id: int,
    query: str,
    vector: list[float] | None,
    model: str,
    *,
    kinds: list[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    repo: str | None = None,
    source_ids: list[str] | None = None,
    limit: int = 8,
) -> list[Hit]:
    """Hybrid: the nearest chunks by meaning (cosine, exact within the project) and the best by words
    (full text, language-neutral 'simple' configuration: German and English alike), fused by rank, with a
    little more for what changed recently. One hit per item, its best chunk. Without a vector (the model is
    not ready yet) by words only. ``since``/``until`` filter by when the item last changed."""
    words = keyword_query(query)
    if vector is None and not words:
        return []
    where = ["d.project_id = :project"]
    params: dict = {
        "project": project_id, "n": CANDIDATES, "k": RRF_K, "model": model, "words": words or "x", "use_words": bool(words), "limit": limit * 4,
        "boost": RECENCY_BOOST, "half": RECENCY_HALF_DAYS * 86400.0,
    }  # fmt: skip
    if kinds:
        where.append("d.kind = ANY(:kinds)")
        params["kinds"] = list(kinds)
    if since:
        where.append("d.changed_at >= :since")
        params["since"] = since
    if until:
        where.append("d.at < :until")
        params["until"] = until
    if repo:
        where.append("d.repo = :repo")
        params["repo"] = repo
    if source_ids is not None:
        where.append("d.source_id = ANY(:sources)")
        params["sources"] = list(source_ids)
    scope = " AND ".join(where)
    if vector is not None:
        params["vector"] = vector_literal(vector)
        by_meaning = f"""SELECT d.id, row_number() OVER (ORDER BY v.embedding <=> CAST(:vector AS vector)) AS r, 1 - (v.embedding <=> CAST(:vector AS vector)) AS sim
            FROM search_documents d JOIN search_vectors v ON v.document_id = d.id AND v.model = :model WHERE {scope}
            ORDER BY v.embedding <=> CAST(:vector AS vector) LIMIT :n"""
    else:
        by_meaning = "SELECT 0 AS id, 0 AS r, 0.0 AS sim WHERE false"
    by_words = f"""SELECT d.id, row_number() OVER (ORDER BY ts_rank_cd(d.tsv, q) DESC) AS r
        FROM search_documents d, to_tsquery('simple', :words) q WHERE {scope} AND :use_words AND d.tsv @@ q
        ORDER BY ts_rank_cd(d.tsv, q) DESC LIMIT :n"""
    sql = f"""WITH m AS ({by_meaning}), w AS ({by_words})
        SELECT {_HIT}, m.sim, m.r AS mr, w.r AS wr,
               (COALESCE(1.0 / (:k + m.r), 0) + COALESCE(1.0 / (:k + w.r), 0))
               * (1 + :boost * power(0.5, GREATEST(EXTRACT(EPOCH FROM (now() AT TIME ZONE 'UTC') - d.changed_at), 0) / :half)) AS score
        FROM search_documents d LEFT JOIN m ON m.id = d.id LEFT JOIN w ON w.id = d.id
        WHERE m.id IS NOT NULL OR w.id IS NOT NULL
        ORDER BY score DESC LIMIT :limit"""
    hits: list[Hit] = []
    seen: set[tuple[str, str]] = set()
    for row in await s.execute(text(sql), params):
        if (row.kind, row.source_id) in seen:
            continue
        seen.add((row.kind, row.source_id))
        hits.append(_hit(row, float(row.score)))
        if len(hits) >= limit:
            break
    return hits


async def nearest(s: AsyncSession, project_id: int, vector: list[float], model: str, *, exclude_ticket: int | None, limit: int) -> list[Hit]:
    """By meaning only, for the hint at the start of an assignment."""
    params = {"project": project_id, "model": model, "vector": vector_literal(vector), "limit": limit * 3, "ticket": exclude_ticket or -1}
    sql = f"""SELECT {_HIT}, 1 - (v.embedding <=> CAST(:vector AS vector)) AS sim
        FROM search_documents d JOIN search_vectors v ON v.document_id = d.id AND v.model = :model
        WHERE d.project_id = :project AND (d.ticket_number IS NULL OR d.ticket_number <> :ticket)
        ORDER BY v.embedding <=> CAST(:vector AS vector) LIMIT :limit"""
    hits, seen = [], set()
    for row in await s.execute(text(sql), params):
        key = row.ticket_number if row.ticket_number is not None else (row.kind, row.source_id)
        if key in seen:
            continue
        seen.add(key)
        hits.append(_hit(row, float(row.sim)))
    return hits[:limit]


async def nearest_of_kind(s: AsyncSession, project_id: int, vector: list[float], model: str, kind: str, limit: int) -> list[Hit]:
    """By meaning only, among one kind (the doc pages a landed change may concern): one hit per item, its best chunk."""
    params = {"project": project_id, "model": model, "vector": vector_literal(vector), "limit": limit * 4, "kind": kind}
    sql = f"""SELECT {_HIT}, 1 - (v.embedding <=> CAST(:vector AS vector)) AS sim
        FROM search_documents d JOIN search_vectors v ON v.document_id = d.id AND v.model = :model
        WHERE d.project_id = :project AND d.kind = :kind
        ORDER BY v.embedding <=> CAST(:vector AS vector) LIMIT :limit"""
    hits, seen = [], set()
    for row in await s.execute(text(sql), params):
        if row.source_id in seen:
            continue
        seen.add(row.source_id)
        hits.append(_hit(row, float(row.sim)))
    return hits[:limit]


@dataclass
class Section:
    source_id: str
    chunk: int
    body: str
    similarity: float


async def nearest_chunks(s: AsyncSession, project_id: int, vector: list[float], model: str, kind: str, limit: int) -> list[Section]:
    """By meaning only, among one kind: the nearest chunks, several of one item too (the sections of doc pages, #190)."""
    params = {"project": project_id, "model": model, "vector": vector_literal(vector), "limit": limit, "kind": kind}
    sql = """SELECT d.source_id, d.chunk, d.body, 1 - (v.embedding <=> CAST(:vector AS vector)) AS sim
        FROM search_documents d JOIN search_vectors v ON v.document_id = d.id AND v.model = :model
        WHERE d.project_id = :project AND d.kind = :kind
        ORDER BY v.embedding <=> CAST(:vector AS vector) LIMIT :limit"""
    return [Section(row.source_id, row.chunk, row.body, float(row.sim)) for row in await s.execute(text(sql), params)]
