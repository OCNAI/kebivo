"""Initial SQLite schema for Phase 0 persistence support."""

from alembic import op
from sqlalchemy import Column, DateTime, ForeignKey, Index, MetaData, String, Table, Text

from migrations.legacy_models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep this historical migration pinned to Phase 0 tables. New models belong
    # in a new revision; metadata grows as the application evolves.
    phase_zero_tables = [
        Base.metadata.tables[name]
        for name in (
            "users",
            "sources",
            "records",
            "processing_jobs",
            "deliveries",
            "idempotency_keys",
        )
    ]
    Base.metadata.create_all(bind=op.get_bind(), tables=phase_zero_tables)
    legacy_metadata = MetaData()
    Table("users", legacy_metadata, Column("id", String(36), primary_key=True))
    audit_events = Table(
        "audit_events",
        legacy_metadata,
        Column("id", String(36), primary_key=True),
        Column("actor_id", String(36), ForeignKey("users.id", ondelete="SET NULL")),
        Column("action", String(120), nullable=False),
        Column("subject_type", String(80), nullable=False),
        Column("subject_id", String(36)),
        Column("details_json", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    audit_events.create(op.get_bind())
    Index("ix_audit_events_subject", audit_events.c.subject_type, audit_events.c.subject_id).create(
        op.get_bind()
    )


def downgrade() -> None:
    phase_zero_tables = [
        Base.metadata.tables[name]
        for name in (
            "users",
            "sources",
            "records",
            "processing_jobs",
            "deliveries",
            "idempotency_keys",
        )
    ]
    Base.metadata.drop_all(bind=op.get_bind(), tables=phase_zero_tables)
    op.drop_table("audit_events")
