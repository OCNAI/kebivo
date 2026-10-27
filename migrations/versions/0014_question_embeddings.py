"""Store derived question vectors and identify evaluation methods."""

import sqlalchemy as sa
from alembic import op

revision = "0014_question_embeddings"
down_revision = "0013_slack_destination"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "question_embeddings",
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("feedback_events.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column(
            "provider_id",
            sa.String(36),
            sa.ForeignKey("llm_provider_configs.id", ondelete="SET NULL"),
        ),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("dims", sa.Integer(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_question_embeddings_lookup",
        "question_embeddings",
        ["project_id", "event_type", "model"],
    )
    op.add_column(
        "dedup_evaluations",
        sa.Column("method", sa.String(20), nullable=False, server_default="judge"),
    )


def downgrade() -> None:
    op.drop_column("dedup_evaluations", "method")
    op.drop_index("ix_question_embeddings_lookup", table_name="question_embeddings")
    op.drop_table("question_embeddings")
