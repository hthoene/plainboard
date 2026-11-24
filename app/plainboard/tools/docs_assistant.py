"""The tools of a doc site's assistant for visitors (#119, orchestration/docs_assistant.py): role ``VISITOR``, which no
agent has, and a visitor's turn has nothing else. The site, the language and the address base come from the turn
(``RunContext.doc_chat``), never from the model. ``read_code``/``search_code`` run in the runner (runner code_read.py)
and, like ``board_status``, are only given to a turn of a site with ``assistant_code`` on."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import DocRelease, DocSite
from ..services import docs_assistant as assistant
from .registry import VISITOR, ToolContext, ToolError, local_tool, tool

DOCS_TOOLS = ("search_pages", "read_page")
CODE_TOOLS = ("search_code", "read_code", "board_status")


async def _site(ctx: ToolContext) -> tuple[DocSite, object, DocRelease | None]:
    """The site, the conversation and the version the visitor reads (None: the working one)."""
    chat = ctx.run.doc_chat
    site = await ctx.session.get(DocSite, chat.site_id) if chat else None
    if site is None or not site.published or site.project_id != ctx.project.id:
        raise ToolError("This documentation is not available.")
    rel = await ctx.session.get(DocRelease, chat.release_id) if getattr(chat, "release_id", None) else None
    if rel is not None and rel.site_id != site.id:
        raise ToolError("This documentation is not available.")
    return site, chat, rel


class SearchDocsArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200)


@tool("search_pages", "Search this documentation's pages by words and meaning: title, url, path, snippet.", SearchDocsArgs, roles=(VISITOR,))
async def search_pages(ctx: ToolContext, args: SearchDocsArgs) -> str:
    site, chat, rel = await _site(ctx)
    text, hits = await assistant.search_text(ctx.session, site, args.query, chat.lang, chat.base, rel=rel)
    chat.seen.update({h["url"]: h["title"] for h in hits})
    return text


class ReadDocPageArgs(BaseModel):
    path: str = Field(default="", max_length=2000, description="The page's path from search_pages; empty: the home page")


@tool("read_page", "Read a whole page of this documentation.", ReadDocPageArgs, roles=(VISITOR,))
async def read_page(ctx: ToolContext, args: ReadDocPageArgs) -> str:
    site, chat, rel = await _site(ctx)
    found = await assistant.page_text(ctx.session, site, args.path, chat.lang, chat.base, rel)
    if found is None:
        raise ToolError("No such page. Search with search_pages.")
    title, url, text = found
    chat.seen[url] = title
    more = "\n(cut: the page goes on)" if len(text) > assistant.READ_CHARS else ""
    return f"# [{title}]({url})\n{text[: assistant.READ_CHARS]}{more}"


class BoardStatusArgs(BaseModel):
    query: str = Field(default="", max_length=200, description="Words to filter by; empty: all")


@tool("board_status", "Public status of the project's planned and finished work (titles, descriptions, status).", BoardStatusArgs, roles=(VISITOR,))
async def board_status(ctx: ToolContext, args: BoardStatusArgs) -> str:
    site, chat, _ = await _site(ctx)
    if not site.assistant_code:
        raise ToolError("Not available.")
    return await assistant.board_text(ctx.session, ctx.project, args.query, chat.lang)


class SearchCodeArgs(BaseModel):
    pattern: str = Field(default="", max_length=200, description="Regex (case-insensitive); empty: list the files")
    glob: str = Field(default="", max_length=200, description="e.g. *.py or src/*")


class ReadCodeArgs(BaseModel):
    path: str = Field(min_length=1, max_length=500, description="Relative to the code's root, as search_code shows it")
    offset: int = Field(default=1, ge=1, description="First line")
    limit: int = Field(default=200, ge=1, le=400)


local_tool("search_code", "Search the project's source code: matching lines as path:line: text.", SearchCodeArgs, roles=(VISITOR,))
local_tool("read_code", "Read a file of the project's source code (numbered lines).", ReadCodeArgs, roles=(VISITOR,))


def tools_for(code: bool) -> tuple[str, ...]:
    return DOCS_TOOLS + (CODE_TOOLS if code else ())

