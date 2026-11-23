"""Customer texts in several languages (#79).

A project has a list of customer languages (``Project.customer_languages``, codes like "en"), the
first is its default. The texts of the default language are the ticket's ``customer_title`` and
``customer_description``; those of the others are in ``Task.customer_texts`` (code -> {title,
description}). Texts of a language that was removed from the list stay there and come back when the
language is added again. A ticket is public when it has a customer title in the default language.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Project, Task
from .errors import ServiceError

DEFAULT_LANGUAGES = ["en"]

# Codes offered in the settings, with their English names (prompts) and the names a project's language may be written in.
LANGUAGES: dict[str, str] = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
    "pt": "Portuguese",
    "sv": "Swedish",
    "da": "Danish",
    "no": "Norwegian",
    "fi": "Finnish",
    "cs": "Czech",
    "sk": "Slovak",
    "hu": "Hungarian",
    "ro": "Romanian",
    "el": "Greek",
    "tr": "Turkish",
    "ru": "Russian",
    "uk": "Ukrainian",
    "ar": "Arabic",
    "he": "Hebrew",
    "hi": "Hindi",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
}


def name(code: str) -> str:
    return LANGUAGES.get(code, code)


def languages(project: Project) -> list[str]:
    return list(project.customer_languages or DEFAULT_LANGUAGES)


def default(project: Project) -> str:
    return languages(project)[0]


def texts(task: Task, lang: str, project: Project) -> tuple[str, str]:
    """Title and description of a ticket in one language ("" where there is none)."""
    if lang == default(project):
        return task.customer_title, task.customer_description
    other = (task.customer_texts or {}).get(lang) or {}
    return other.get("title", ""), other.get("description", "")


def missing(task: Task, project: Project) -> list[str]:
    """The project's languages without a customer title on this ticket."""
    return [lang for lang in languages(project) if not texts(task, lang, project)[0].strip()]


def apply(task: Task, project: Project, given: dict[str, dict]) -> None:
    """Sets texts per language: only the fields given; a language left without title and description goes."""
    known = languages(project)
    if unknown := [lang for lang in given if lang not in known]:
        raise ServiceError(f"Not a customer language of this project: {', '.join(unknown)} (it has: {', '.join(known)})")
    others = dict(task.customer_texts or {})
    for lang, fields in given.items():
        fields = {k: v for k, v in fields.items() if v is not None}
        if lang == known[0]:
            if "title" in fields:
                task.customer_title = fields["title"]
            if "description" in fields:
                task.customer_description = fields["description"]
            continue
        entry = {"title": "", "description": "", **others.get(lang, {}), **fields}
        if entry["title"] or entry["description"]:
            others[lang] = entry
        else:
            others.pop(lang, None)
    task.customer_texts = others


async def switch_default(session: AsyncSession, project: Project, new: str) -> list[Task]:
    """Makes another language the default: on every ticket its texts become customer_title/description,
    and those of the old default go to customer_texts. Returns the tickets that changed."""
    old = default(project)
    changed = []
    for task in (await session.scalars(select(Task).where(Task.project_id == project.id))).all():
        others = dict(task.customer_texts or {})
        if not (task.customer_title or task.customer_description or new in others):
            continue
        if task.customer_title or task.customer_description:
            others[old] = {"title": task.customer_title, "description": task.customer_description}
        else:
            others.pop(old, None)
        moved = others.pop(new, {})
        task.customer_title = moved.get("title", "")
        task.customer_description = moved.get("description", "")
        task.customer_texts = others
        changed.append(task)
    return changed


async def counts(session: AsyncSession, project: Project) -> dict[str, int]:
    """How many tickets have texts per language, also in languages no longer in the list."""
    out: dict[str, int] = {}
    first = default(project)
    rows = await session.execute(select(Task.customer_title, Task.customer_description, Task.customer_texts).where(Task.project_id == project.id))
    for title, description, others in rows:
        if title or description:
            out[first] = out.get(first, 0) + 1
        for lang in others or {}:
            out[lang] = out.get(lang, 0) + 1
    return out
