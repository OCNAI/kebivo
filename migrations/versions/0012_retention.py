"""Persist retention runs and index housekeeping cutoffs."""

import sqlalchemy as sa
from alembic import op

revision = "0012_retention"
down_revision = "0011_semantic_dedup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("cutoffs_json", sa.Text(), nullable=False),
        sa.Column("counts_json", sa.Text(), nullable=False),
        sa.Column("error_category", sa.String(80)),
        sa.CheckConstraint("kind = 'retention'", name="ck_maintenance_runs_kind"),
        sa.CheckConstraint(
            "status IN ('running','completed','failed')", name="ck_maintenance_runs_status"
        ),
    )
    op.create_index("ix_maintenance_runs_kind_started", "maintenance_runs", ["kind", "started_at"])
    op.create_index("ix_delivery_attempts_completed", "delivery_attempts", ["completed_at"])
    op.create_index("ix_audit_events_created", "audit_events", ["created_at"])
    op.create_index("ix_admin_sessions_expires", "admin_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_sessions_expires", table_name="admin_sessions")
    op.drop_index("ix_audit_events_created", table_name="audit_events")
    op.drop_index("ix_delivery_attempts_completed", table_name="delivery_attempts")
    op.drop_index("ix_maintenance_runs_kind_started", table_name="maintenance_runs")
    op.drop_table("maintenance_runs")
