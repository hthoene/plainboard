"""accounts (several subscriptions or API keys per provider)

Revision ID: 0018
Revises: 0017

Moves the single Claude and Codex sign-in from the settings into one account each, and the stored
plan limits under these accounts.
"""
import json
import os
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

LEGACY_KEYS = ("claude_auth_mode", "claude_oauth_token", "anthropic_api_key", "openai_api_key", "codex_auth_mode")
CLAUDE_KEY, CODEX_KEY = "claude", "default"  # Codex's "default" keeps the runner's existing sign-in


def upgrade() -> None:
    accounts = op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("key", sa.String(40), nullable=False, unique=True),
        sa.Column("auth_mode", sa.String(20), nullable=False),
        sa.Column("secret_enc", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("account_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_agents_account_id", "accounts", ["account_id"], ["id"], ondelete="SET NULL")
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("account_key", sa.String(40), nullable=True))

    conn = op.get_bind()
    rows = dict(conn.execute(sa.text("SELECT key, value FROM settings")).all())

    def plain(key: str):
        return json.loads(rows[key]) if rows.get(key) else None

    claude_mode = plain("claude_auth_mode") or (
        "api_key"
        if rows.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
        else "oauth_token"
        if rows.get("claude_oauth_token") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        else "runner_login"
    )
    codex_mode = "api_key" if plain("codex_auth_mode") == "api_key" else "chatgpt"
    now = datetime.now(UTC).replace(tzinfo=None)
    op.bulk_insert(
        accounts,
        [
            {
                "provider": "claude_code",
                "label": "Claude",
                "key": CLAUDE_KEY,
                "auth_mode": claude_mode,
                "secret_enc": rows.get({"api_key": "anthropic_api_key", "oauth_token": "claude_oauth_token"}.get(claude_mode, "")),
                "enabled": True,
                "created_at": now,
                "updated_at": now,
            },
            {
                "provider": "codex",
                "label": "Codex",
                "key": CODEX_KEY,
                "auth_mode": codex_mode,
                "secret_enc": rows.get("openai_api_key") if codex_mode == "api_key" else None,
                "enabled": True,
                "created_at": now,
                "updated_at": now,
            },
        ],
    )

    if rows.get("provider_limits"):
        stored = json.loads(rows["provider_limits"])
        split: dict = {}
        for part in ("windows", "growth"):
            for name, value in (stored.get(part) or {}).items():
                key = CODEX_KEY if name.startswith("codex_") else CLAUDE_KEY
                split.setdefault(key, {"windows": {}, "growth": {}, "updated_at": stored.get("updated_at")})[part][name] = value
        conn.execute(sa.text("UPDATE settings SET value = :value WHERE key = 'provider_limits'"), {"value": json.dumps({"accounts": split})})
    conn.execute(sa.text("DELETE FROM settings WHERE key IN :keys").bindparams(sa.bindparam("keys", expanding=True)), {"keys": list(LEGACY_KEYS)})


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("account_key")
    with op.batch_alter_table("agents") as batch:
        batch.drop_constraint("fk_agents_account_id", type_="foreignkey")
        batch.drop_column("account_id")
    op.drop_table("accounts")
    op.execute("DELETE FROM settings WHERE key = 'provider_limits'")
