"""A project's doc sites in the app (#118, services/docs.py). Viewers read, members write, owners set the domain and
the translators' keys (services/access.py). Nothing returns a whole site: the tree comes page by page.
Live updates: ``doc_site.upserted`` (DocSiteOut), ``doc_site.deleted`` ({id}), ``doc_page.upserted`` (DocPageItem),
``doc_page.deleted`` ({id, site_id}), ``attachment.created`` for files. A site's own pages for its footer and its
theme (#121): services/docs_custom.py, services/docs_theme.py."""

from __future__ import annotations

from fastapi import APIRouter, File, Query, Request, UploadFile

from ..schemas import (
    AttachmentOut, DocCloudflareIn, DocCustomPageCreate, DocCustomPageOut, DocCustomPageUpdate, DocTheme, DocThemePreviewOut, DocCrumb, DocDnsOut, DocDomainIn, DocDomainOut, DocKeysIn, DocPageCreate, DocPageItem, DocPageOut, DocPagesOut,
    DocPageUpdate, DocSiteCreate, DocSiteOut, DocSiteUpdate, DocTranslationIn, DocTranslationItem, DocTranslationOut, DocVerifyOut, DocVersionOut,
)  # fmt: skip
from ..services import docs as docs_service
from ..services import docs_custom, docs_domains, docs_theme, docs_translate
from ..services import projects as project_service
from ..services.errors import NotFound
from .deps import Session, Who
from .guard import _hosts

router = APIRouter(prefix="/api/projects/{project_id}/docs", tags=["docs"])


def _author(who) -> docs_service.Author:  # noqa: ANN001
    return docs_service.Author(who.name or "someone")


def _target(request: Request) -> str:
    """The host this PlainBoard was reached at: what a domain's CNAME points to."""
    hosts = sorted(_hosts(request.headers))
    forwarded = request.headers.get("x-forwarded-host", "").split(",")[0].strip().lower()
    return (forwarded or (hosts[0] if hosts else "")).split(":", 1)[0]


def _translate_soon(request: Request) -> None:
    watcher = getattr(request.app.state, "docs_watcher", None)
    if watcher is not None:
        watcher.translate_soon()


async def _page_out(session, page) -> DocPageOut:  # noqa: ANN001
    counts = await docs_service.child_counts(session, [page.id])
    site = await session.get(docs_service.DocSite, page.site_id)
    return DocPageOut(
        **docs_service.item(page, counts.get(page.id, 0)).model_dump(), body=page.body, versions=await docs_service.count_versions(session, page.id),
        ancestors=[DocCrumb(id=p.id, title=p.title, path=p.path) for p in await docs_service.ancestors(session, page)],
        translations=await docs_translate.of_page(session, site, page),
    )  # fmt: skip


async def _site_out(session, site) -> DocSiteOut:  # noqa: ANN001
    return (await docs_service.site_outs(session, [site]))[0]


@router.get("/sites", response_model=list[DocSiteOut])
async def list_sites(project_id: int, session: Session) -> list[DocSiteOut]:
    await project_service.get(session, project_id)
    return await docs_service.site_outs(session, await docs_service.sites(session, project_id))


@router.post("/sites", response_model=DocSiteOut, status_code=201)
async def create_site(project_id: int, body: DocSiteCreate, session: Session) -> DocSiteOut:
    await project_service.get(session, project_id)
    return await _site_out(session, await docs_service.create_site(session, project_id, **body.model_dump()))


@router.get("/sites/{site_id}", response_model=DocSiteOut)
async def get_site(project_id: int, site_id: int, session: Session) -> DocSiteOut:
    return await _site_out(session, await docs_service.get_site(session, project_id, site_id))


@router.patch("/sites/{site_id}", response_model=DocSiteOut)
async def update_site(project_id: int, site_id: int, body: DocSiteUpdate, session: Session, request: Request, who: Who) -> DocSiteOut:
    site = await docs_service.get_site(session, project_id, site_id)
    site = await docs_service.update_site(session, site, author=_author(who), reason="in the settings", **body.model_dump(exclude_unset=True))
    if site.languages and site.translator:
        _translate_soon(request)
    return await _site_out(session, site)


@router.delete("/sites/{site_id}", status_code=204)
async def delete_site(project_id: int, site_id: int, session: Session) -> None:
    await docs_service.delete_site(session, await docs_service.get_site(session, project_id, site_id))


@router.put("/sites/{site_id}/translator-keys", response_model=DocSiteOut)
async def set_keys(project_id: int, site_id: int, body: DocKeysIn, session: Session, request: Request) -> DocSiteOut:
    """DeepL and Google Translate API keys: stored encrypted, never shown again ("" removes one)."""
    site = await docs_service.get_site(session, project_id, site_id)
    await docs_translate.set_keys(session, site, body.deepl, body.google)
    _translate_soon(request)
    return await _site_out(session, site)


@router.post("/sites/{site_id}/translate", status_code=202)
async def translate_now(project_id: int, site_id: int, session: Session, request: Request) -> dict:
    """Translate what is missing or outdated now (in bounded batches, in the background)."""
    await docs_service.get_site(session, project_id, site_id)
    _translate_soon(request)
    return {"translating": True}


# --- domain ------------------------------------------------------------------------------------------------------


@router.get("/sites/{site_id}/domain", response_model=DocDomainOut | None)
async def domain_setup(project_id: int, site_id: int, session: Session, request: Request) -> DocDomainOut | None:
    """What to set up at the DNS (or the tunnel) for the site's domain, and whether it is verified."""
    return docs_domains.setup(await docs_service.get_site(session, project_id, site_id), _target(request))


@router.put("/sites/{site_id}/domain", response_model=DocDomainOut | None)
async def set_domain(project_id: int, site_id: int, body: DocDomainIn, session: Session, request: Request) -> DocDomainOut | None:
    """The site's own domain ("" removes it); a new one has to be verified before the site is served there."""
    site = await docs_service.get_site(session, project_id, site_id)
    if body.domain.strip():
        await docs_domains.check_free(session, docs_service.clean_domain(body.domain), site.id, _hosts(request.headers))
    site = await docs_service.update_site(session, site, domain=body.domain)
    return docs_domains.setup(site, _target(request))


@router.post("/sites/{site_id}/domain/verify", response_model=DocVerifyOut)
async def verify_domain(project_id: int, site_id: int, session: Session) -> DocVerifyOut:
    return await docs_domains.verify(session, await docs_service.get_site(session, project_id, site_id))


@router.post("/sites/{site_id}/domain/cloudflare", response_model=DocDnsOut)
async def cloudflare_dns(project_id: int, site_id: int, body: DocCloudflareIn, session: Session, request: Request) -> DocDnsOut:
    """Creates (or changes) the proxied CNAME in Cloudflare; ``token`` is kept encrypted for the site, never shown."""
    site = await docs_service.get_site(session, project_id, site_id)
    if body.token is not None:
        await docs_domains.set_cloudflare_token(session, site, body.token)
    return await docs_domains.create_dns(session, site, body.target or _target(request), proxied=body.proxied)


@router.delete("/sites/{site_id}/domain/cloudflare", status_code=204)
async def forget_cloudflare(project_id: int, site_id: int, session: Session) -> None:
    await docs_domains.set_cloudflare_token(session, await docs_service.get_site(session, project_id, site_id), "")


# --- pages -------------------------------------------------------------------------------------------------------


@router.get("/sites/{site_id}/pages", response_model=DocPagesOut)
async def list_pages(
    project_id: int, site_id: int, session: Session, parent: int | None = None, offset: int = Query(default=0, ge=0),
    limit: int = Query(default=docs_service.PAGE_LIMIT, ge=1, le=docs_service.PAGE_LIMIT),
) -> DocPagesOut:  # fmt: skip
    """The children of a page (none: the top), in order, page by page, each with how many children it has."""
    await docs_service.get_site(session, project_id, site_id)
    rows, counts, total = await docs_service.children(session, site_id, parent, offset, limit)
    return DocPagesOut(items=[docs_service.item(p, counts.get(p.id, 0)) for p in rows], total=total, offset=offset)


@router.get("/sites/{site_id}/pages/find", response_model=list[DocPageItem])
async def find_pages(project_id: int, site_id: int, session: Session, q: str = Query(min_length=1, max_length=200), limit: int = Query(default=20, ge=1, le=50)) -> list[DocPageItem]:
    """Pages whose title or address has these words (the tree's filter)."""
    await docs_service.get_site(session, project_id, site_id)
    rows = await docs_service.search_titles(session, site_id, q, limit)
    counts = await docs_service.child_counts(session, [p.id for p in rows])
    return [docs_service.item(p, counts.get(p.id, 0)) for p in rows]


@router.get("/sites/{site_id}/pages/by-path", response_model=DocPageOut)
async def page_by_path(project_id: int, site_id: int, session: Session, path: str = Query(default="", max_length=2000)) -> DocPageOut:
    await docs_service.get_site(session, project_id, site_id)
    page = await docs_service.by_path(session, site_id, path)
    if page is None:
        raise NotFound("Doc page not found")
    return await _page_out(session, page)


@router.post("/sites/{site_id}/pages", response_model=DocPageOut, status_code=201)
async def create_page(project_id: int, site_id: int, body: DocPageCreate, session: Session, who: Who, request: Request) -> DocPageOut:
    site = await docs_service.get_site(session, project_id, site_id)
    page = await docs_service.create_page(session, site, _author(who), **body.model_dump())
    if site.languages and site.translator:
        _translate_soon(request)
    return await _page_out(session, page)


@router.get("/sites/{site_id}/files", response_model=list[AttachmentOut])
async def list_files(project_id: int, site_id: int, session: Session, offset: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=500)) -> list[AttachmentOut]:
    """Download and delete them as every attachment (/api/attachments/{id}); in a page: doc-file:<id>."""
    await docs_service.get_site(session, project_id, site_id)
    return [AttachmentOut.model_validate(a) for a in await docs_service.files(session, site_id, offset, limit)]


@router.post("/sites/{site_id}/files", response_model=list[AttachmentOut], status_code=201)
async def upload_files(project_id: int, site_id: int, session: Session, files: list[UploadFile] = File(...)) -> list[AttachmentOut]:
    site = await docs_service.get_site(session, project_id, site_id)
    out = []
    for file in files:
        out.append(AttachmentOut.model_validate(await docs_service.add_file(session, site, file.filename or "file", file.content_type or "", file.file)))
    return out


@router.get("/pages/{page_id}", response_model=DocPageOut)
async def get_page(project_id: int, page_id: int, session: Session) -> DocPageOut:
    return await _page_out(session, await docs_service.get_page(session, project_id, page_id))


@router.patch("/pages/{page_id}", response_model=DocPageOut)
async def update_page(project_id: int, page_id: int, body: DocPageUpdate, session: Session, who: Who, request: Request) -> DocPageOut:
    page = await docs_service.get_page(session, project_id, page_id)
    before = page.source_hash
    page = await docs_service.update_page(session, page, _author(who), **body.model_dump(exclude_unset=True))
    if page.source_hash != before:
        _translate_soon(request)
    return await _page_out(session, page)


@router.delete("/pages/{page_id}", status_code=204)
async def delete_page(project_id: int, page_id: int, session: Session) -> None:
    await docs_service.delete_page(session, await docs_service.get_page(session, project_id, page_id))


@router.get("/pages/{page_id}/versions", response_model=list[DocVersionOut])
async def list_versions(project_id: int, page_id: int, session: Session) -> list[DocVersionOut]:
    """Newest first (at most 100), without their texts; the first is the page as it is."""
    page = await docs_service.get_page(session, project_id, page_id)
    return [DocVersionOut.model_validate(v).model_copy(update={"body": None}) for v in await docs_service.versions(session, page.id)]


@router.get("/pages/{page_id}/versions/{version_id}", response_model=DocVersionOut)
async def get_version(project_id: int, page_id: int, version_id: int, session: Session) -> DocVersionOut:
    page = await docs_service.get_page(session, project_id, page_id)
    return DocVersionOut.model_validate(await docs_service.get_version(session, page, version_id))


@router.post("/pages/{page_id}/versions/{version_id}/restore", response_model=DocPageOut)
async def restore_version(project_id: int, page_id: int, version_id: int, session: Session, who: Who, request: Request) -> DocPageOut:
    page = await docs_service.get_page(session, project_id, page_id)
    page = await docs_service.restore(session, page, version_id, _author(who))
    _translate_soon(request)
    return await _page_out(session, page)


@router.post("/pages/{page_id}/confirm", response_model=DocPageOut)
async def confirm_page(project_id: int, page_id: int, session: Session, who: Who) -> DocPageOut:
    """Still right after what changed: the mark that it may be outdated goes, without a new version."""
    page = await docs_service.get_page(session, project_id, page_id)
    return await _page_out(session, await docs_service.confirm_page(session, page, _author(who), "confirmed"))


@router.get("/pages/{page_id}/translations", response_model=list[DocTranslationItem])
async def list_translations(project_id: int, page_id: int, session: Session) -> list[DocTranslationItem]:
    """One per further language of its site: current, outdated (its page changed since), missing or failed."""
    page = await docs_service.get_page(session, project_id, page_id)
    return await docs_translate.of_page(session, await session.get(docs_service.DocSite, page.site_id), page)


@router.get("/pages/{page_id}/translations/{lang}", response_model=DocTranslationOut)
async def get_translation(project_id: int, page_id: int, lang: str, session: Session) -> DocTranslationOut:
    page = await docs_service.get_page(session, project_id, page_id)
    return await docs_translate.get(session, await session.get(docs_service.DocSite, page.site_id), page, lang)


@router.put("/pages/{page_id}/translations/{lang}", response_model=DocTranslationOut)
async def put_translation(project_id: int, page_id: int, lang: str, body: DocTranslationIn, session: Session, who: Who) -> DocTranslationOut:
    """A translation by hand: current until its page changes again (then it is translated anew)."""
    page = await docs_service.get_page(session, project_id, page_id)
    site = await session.get(docs_service.DocSite, page.site_id)
    await docs_translate.save(session, site, page, lang, body.title, body.body, who.name or "someone")
    return await docs_translate.get(session, site, page, lang)


# --- the site's own pages and its theme (#121) ------------------------------------------------------------------


@router.get("/sites/{site_id}/custom-pages", response_model=list[DocCustomPageOut])
async def list_custom_pages(project_id: int, site_id: int, session: Session) -> list[DocCustomPageOut]:
    """Its pages outside the docs tree (imprint, privacy), in the footer's order, with their texts."""
    await docs_service.get_site(session, project_id, site_id)
    return [docs_custom.out(p) for p in await docs_custom.pages(session, site_id)]


@router.post("/sites/{site_id}/custom-pages", response_model=DocCustomPageOut, status_code=201)
async def create_custom_page(project_id: int, site_id: int, body: DocCustomPageCreate, session: Session, who: Who) -> DocCustomPageOut:
    site = await docs_service.get_site(session, project_id, site_id)
    return docs_custom.out(await docs_custom.create(session, site, who.name or "someone", **body.model_dump(exclude={"texts"}), texts=body.texts))


@router.patch("/custom-pages/{page_id}", response_model=DocCustomPageOut)
async def update_custom_page(project_id: int, page_id: int, body: DocCustomPageUpdate, session: Session, who: Who) -> DocCustomPageOut:
    page = await docs_custom.get(session, project_id, page_id)
    site = await docs_service.get_site(session, project_id, page.site_id)
    changes = body.model_dump(exclude_unset=True, exclude={"texts"})
    page = await docs_custom.update(session, site, page, who.name or "someone", **changes, texts=body.texts)
    return docs_custom.out(page)


@router.delete("/custom-pages/{page_id}", status_code=204)
async def delete_custom_page(project_id: int, page_id: int, session: Session) -> None:
    page = await docs_custom.get(session, project_id, page_id)
    await docs_custom.delete(session, await docs_service.get_site(session, project_id, page.site_id), page)


@router.post("/sites/{site_id}/theme/preview", response_model=DocThemePreviewOut)
async def preview_theme(project_id: int, site_id: int, body: DocTheme, session: Session) -> DocThemePreviewOut:
    """The CSS of a theme not saved yet, for the preview in the settings (scoped to .doc-tp[data-design]); an error as on saving."""
    site = await docs_service.get_site(session, project_id, site_id)
    return DocThemePreviewOut(css=docs_theme.css(await docs_theme.clean(session, site, body), docs_theme.PREVIEW_SCOPE))
