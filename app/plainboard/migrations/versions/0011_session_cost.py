"""conversation.session_cost_usd; repair costs summed from running totals

The provider's cost per run was a running total for the session, so totals were counted many times.

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("session_cost_usd", sa.Float(), nullable=False, server_default="0"))

    db = op.get_bind()
    for agent_id, conv_id, total in db.execute(sa.text("SELECT agent_id, id, total_cost_usd FROM conversations")).fetchall():
        runs = db.execute(sa.text("SELECT id, cost_usd FROM runs WHERE agent_id = :a AND cost_usd > 0 ORDER BY id"), {"a": agent_id}).fetchall()
        previous = 0.0
        for run_id, cost in runs:
            # A lower value than before means a new session started.
            real = cost - previous if cost >= previous else cost
            db.execute(sa.text("UPDATE runs SET cost_usd = :c WHERE id = :i"), {"c": real, "i": run_id})
            total -= cost - real
            previous = cost
        db.execute(
            sa.text("UPDATE conversations SET total_cost_usd = :t, session_cost_usd = :s WHERE id = :i"),
            {"t": max(total, 0.0), "s": previous, "i": conv_id},
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("session_cost_usd")
