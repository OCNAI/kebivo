"""Durable cursor for representative embedding backfills."""

import sqlalchemy as sa
from alembic import op

revision = "0015_embedding_backfills"
down_revision = "0014_question_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "embedding_backfills",
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("provider_id", sa.String(36), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("dims", sa.Integer()),
        sa.Column("remaining", sa.Integer(), nullable=False),
        sa.Column("cursor_seen_at", sa.DateTime(timezone=True)),
        sa.Column("cursor_group_id", sa.String(36)),
    )


def downgrade() -> None:
    op.drop_table("embedding_backfills")
