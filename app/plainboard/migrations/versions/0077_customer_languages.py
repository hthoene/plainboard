"""Customer texts in several languages (#79): projects.customer_languages, tasks.customer_texts

The texts written so far are in the language of the project's people, so an existing project gets the
language that matches its own (``projects.language``, e.g. "German" -> de) as its only customer
language; without one, English. Its tickets' customer_title/description stay the default language's.

Revision ID: 0077
Revises: 0076
"""
import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None

# Names a project's language may be written in -> code. Frozen here: the app's list may change later.
NAMES = {
    "en": ("english", "englisch", "anglais", "inglés", "ingles", "inglese"),
    "de": ("german", "deutsch", "allemand", "alemán", "aleman", "tedesco"),
    "fr": ("french", "français", "francais", "französisch", "franzoesisch"),
    "es": ("spanish", "español", "espanol", "spanisch", "castellano"),
    "it": ("italian", "italiano", "italienisch"),
    "nl": ("dutch", "nederlands", "niederländisch", "niederlaendisch", "flemish"),
    "pl": ("polish", "polski", "polnisch"),
    "pt": ("portuguese", "português", "portugues", "portugiesisch"),
    "sv": ("swedish", "svenska", "schwedisch"),
    "da": ("danish", "dansk", "dänisch", "daenisch"),
    "no": ("norwegian", "norsk", "norwegisch"),
    "fi": ("finnish", "suomi", "finnisch"),
    "cs": ("czech", "čeština", "cestina", "tschechisch"),
    "sk": ("slovak", "slovenčina", "slowakisch"),
    "hu": ("hungarian", "magyar", "ungarisch"),
    "ro": ("romanian", "română", "romana", "rumänisch"),
    "el": ("greek", "ελληνικά", "griechisch"),
    "tr": ("turkish", "türkçe", "turkce", "türkisch"),
    "ru": ("russian", "русский", "russisch"),
    "uk": ("ukrainian", "українська", "ukrainisch"),
    "ar": ("arabic", "العربية", "arabisch"),
    "he": ("hebrew", "עברית", "hebräisch"),
    "hi": ("hindi",),
    "zh": ("chinese", "中文", "chinesisch", "mandarin"),
    "ja": ("japanese", "日本語", "japanisch"),
    "ko": ("korean", "한국어", "koreanisch"),
}


def code_for(language: str) -> str:
    words = (language or "").strip().lower()
    if not words:
        return "en"
    if words in NAMES:
        return words
    first = words.replace("(", " ").replace(",", " ").split()[0]  # "German (formal)", "Deutsch, du"
    for code, names in NAMES.items():
        if words in names or first in names:
            return code
    return "en"


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("customer_languages", sa.JSON(), nullable=False, server_default='["en"]'))
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("customer_texts", sa.JSON(), nullable=False, server_default="{}"))
    projects = sa.table("projects", sa.column("id", sa.Integer()), sa.column("language", sa.String()), sa.column("customer_languages", sa.JSON()))
    bind = op.get_bind()
    for project_id, language in bind.execute(sa.select(projects.c.id, projects.c.language)).all():
        code = code_for(language)
        if code != "en":
            bind.execute(projects.update().where(projects.c.id == project_id).values(customer_languages=[code]))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("customer_texts")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("customer_languages")
