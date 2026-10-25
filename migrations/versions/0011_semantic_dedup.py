"""Record bounded semantic matching evaluations."""

import sqlalchemy as sa
from alembic import op

revision = "0011_semantic_dedup"
down_revision = "0010_llm_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dedup_evaluations",
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("feedback_events.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_id",
            sa.String(36),
            sa.ForeignKey("llm_provider_configs.id", ondelete="SET NULL"),
        ),
        sa.Column("model", sa.String(200)),
        sa.Column("outcome", sa.String(30), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column(
            "matched_group_id",
            sa.String(36),
            sa.ForeignKey("feedback_groups.id", ondelete="SET NULL"),
        ),
        sa.Column("confidence", sa.Float()),
        sa.Column("reason", sa.String(500)),
        sa.Column("error_category", sa.String(40)),
        sa.Column("http_status", sa.Integer()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('matched','no_match','low_confidence','invalid_response',"
            "'invalid_candidate','no_candidates','provider_unavailable',"
            "'provider_error','timeout','skipped_group_changed')",
            name="ck_dedup_evaluations_outcome",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1",
            name="ck_dedup_evaluations_confidence",
        ),
        sa.CheckConstraint(
            "reason IS NULL OR length(reason) <= 500", name="ck_dedup_evaluations_reason"
        ),
    )
    op.create_index(
        "ix_dedup_evaluations_project_created", "dedup_evaluations", ["project_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_dedup_evaluations_project_created", table_name="dedup_evaluations")
    op.drop_table("dedup_evaluations")
