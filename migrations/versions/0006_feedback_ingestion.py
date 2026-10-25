"""Add durable feedback storage and retire unused placeholder tables.

Downgrade only removes Phase 2 tables. Restore a production database from backup.
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_feedback_ingestion"
down_revision = "0005_project_token_scopes"
branch_labels = None
depends_on = None


def _id() -> sa.Column:
    return sa.Column("id", sa.String(36), primary_key=True)


def _time(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    connection = op.get_bind()
    for table in ("idempotency_keys", "project_tags", "processing_jobs"):
        if connection.exec_driver_sql(f'SELECT EXISTS(SELECT 1 FROM "{table}")').scalar():
            raise RuntimeError(
                f"Cannot migrate {table}: table contains rows; preserve or remove them first"
            )
    if connection.exec_driver_sql(
        "SELECT EXISTS(SELECT 1 FROM tags WHERE project_id IS NULL)"
    ).scalar():
        raise RuntimeError("Cannot migrate tags: project_id contains NULL rows")

    op.drop_table("processing_jobs")
    op.drop_table("project_tags")
    op.drop_table("idempotency_keys")
    with op.batch_alter_table("tags") as batch:
        batch.alter_column("project_id", existing_type=sa.String(36), nullable=False)
    connection.exec_driver_sql("""UPDATE tags SET is_default=0 WHERE is_default=1 AND
        (name='Bug' AND slug='bug' OR name='Feature request' AND slug='feature-request'
        OR name='Question' AND slug='question' OR name='Other' AND slug='other')""")

    op.create_table(
        "feedback_events",
        _id(),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "api_token_id", sa.String(36), sa.ForeignKey("api_tokens.id", ondelete="SET NULL")
        ),
        sa.Column("submitted_via", sa.String(16), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("correction", sa.Text()),
        sa.Column("previous_answer", sa.Text()),
        sa.Column("context", sa.Text()),
        sa.Column("source_reference", sa.Text()),
        sa.Column("agent_id", sa.String(128)),
        sa.Column("conversation_id", sa.String(256)),
        sa.Column("source_event_id", sa.String(256)),
        sa.Column("user_reference", sa.String(256)),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("normalized_question_hash", sa.String(64), nullable=False),
        sa.Column("question_hash_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("idempotency_key", sa.String(255)),
        sa.Column("processing_status", sa.String(20), nullable=False, server_default="pending"),
        _time("created_at"),
        _time("updated_at"),
        sa.CheckConstraint(
            "submitted_via IN ('rest','mcp')", name="ck_feedback_events_submitted_via"
        ),
        sa.CheckConstraint(
            "event_type IN ('knowledge_gap','knowledge_correction')", name="ck_feedback_events_type"
        ),
        sa.CheckConstraint(
            "(event_type = 'knowledge_correction') = (correction IS NOT NULL)",
            name="ck_feedback_events_correction",
        ),
        sa.CheckConstraint(
            "processing_status IN ('pending','processing','completed','failed')",
            name="ck_feedback_events_status",
        ),
        sa.CheckConstraint(
            "length(question) BETWEEN 1 AND 10000", name="ck_feedback_events_question_length"
        ),
        sa.CheckConstraint(
            "correction IS NULL OR length(correction) BETWEEN 1 AND 20000",
            name="ck_feedback_events_correction_length",
        ),
        sa.CheckConstraint(
            "previous_answer IS NULL OR length(previous_answer) <= 10000",
            name="ck_feedback_events_previous_answer_length",
        ),
        sa.CheckConstraint(
            "context IS NULL OR length(context) <= 4000", name="ck_feedback_events_context_length"
        ),
        sa.CheckConstraint(
            "source_reference IS NULL OR length(source_reference) <= 2000",
            name="ck_feedback_events_source_reference_length",
        ),
        sa.CheckConstraint(
            "agent_id IS NULL OR length(agent_id) <= 128", name="ck_feedback_events_agent_id_length"
        ),
        sa.CheckConstraint(
            "conversation_id IS NULL OR length(conversation_id) <= 256",
            name="ck_feedback_events_conversation_id_length",
        ),
        sa.CheckConstraint(
            "source_event_id IS NULL OR length(source_event_id) <= 256",
            name="ck_feedback_events_source_event_id_length",
        ),
        sa.CheckConstraint(
            "user_reference IS NULL OR length(user_reference) <= 256",
            name="ck_feedback_events_user_reference_length",
        ),
        sa.CheckConstraint(
            "length(CAST(metadata_json AS BLOB)) <= 8192", name="ck_feedback_events_metadata_length"
        ),
    )
    op.create_index(
        "ix_feedback_events_project_created", "feedback_events", ["project_id", "created_at", "id"]
    )
    op.create_index("ix_feedback_events_created", "feedback_events", ["created_at", "id"])
    op.create_index(
        "ix_feedback_events_question_hash",
        "feedback_events",
        ["project_id", "event_type", "normalized_question_hash"],
    )
    op.create_index(
        "ix_feedback_events_status_created", "feedback_events", ["processing_status", "created_at"]
    )
    op.create_table(
        "feedback_event_tags",
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("feedback_events.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id", sa.String(36), sa.ForeignKey("tags.id", ondelete="RESTRICT"), primary_key=True
        ),
        sa.Column("from_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("from_submission", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint(
            "from_default = 1 OR from_submission = 1", name="ck_feedback_event_tags_source"
        ),
    )
    op.create_index(
        "ix_feedback_event_tags_tag_event", "feedback_event_tags", ["tag_id", "event_id"]
    )
    op.create_table(
        "idempotency_records",
        _id(),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("feedback_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _time("created_at"),
        _time("expires_at"),
        sa.UniqueConstraint("project_id", "key", name="uq_idempotency_records_project_key"),
    )
    op.create_index("ix_idempotency_records_expires", "idempotency_records", ["expires_at"])
    op.create_table(
        "processing_jobs",
        _id(),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("feedback_events.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        _time("next_attempt_at", True),
        sa.Column("lease_owner", sa.String(128)),
        _time("lease_until", True),
        sa.Column("error_code", sa.String(120)),
        _time("created_at"),
        _time("updated_at"),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="ck_processing_jobs_status",
        ),
    )
    op.create_index(
        "ix_processing_jobs_status_next_attempt", "processing_jobs", ["status", "next_attempt_at"]
    )


def downgrade() -> None:
    for table in (
        "processing_jobs",
        "idempotency_records",
        "feedback_event_tags",
        "feedback_events",
    ):
        op.drop_table(table)
    # Legacy placeholders cannot be reconstructed safely from production data.
