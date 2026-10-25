"""Add Phase 4 routing storage. Restore from backup instead of downgrading."""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0007_routing_destinations"
down_revision = "0006_feedback_ingestion"
branch_labels = None
depends_on = None


def ident() -> sa.Column:
    return sa.Column("id", sa.String(36), primary_key=True)


def when(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    bind = op.get_bind()
    for table in ("deliveries", "records"):
        if bind.exec_driver_sql(f'SELECT EXISTS(SELECT 1 FROM "{table}")').scalar():
            raise RuntimeError(f"Cannot migrate {table}: table contains rows; restore from backup")
    op.drop_table("deliveries")
    op.create_table(
        "destinations",
        ident(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("secrets_ciphertext", sa.Text()),
        sa.Column("secrets_key_id", sa.String(8)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("delivery_mode", sa.String(30), nullable=False, server_default="new_groups_only"),
        sa.Column(
            "allow_private_network", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_by_admin_id",
            sa.String(36),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
        ),
        when("created_at"),
        when("updated_at"),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 120", name="ck_destinations_name"),
        sa.CheckConstraint("type IN ('webhook','smtp','inbox')", name="ck_destinations_type"),
        sa.CheckConstraint(
            "length(CAST(config_json AS BLOB)) <= 16384", name="ck_destinations_config_length"
        ),
        sa.CheckConstraint(
            "delivery_mode IN ('new_groups_only','every_occurrence')",
            name="ck_destinations_delivery_mode",
        ),
        sa.CheckConstraint(
            "(secrets_ciphertext IS NULL) = (secrets_key_id IS NULL)",
            name="ck_destinations_secrets_pair",
        ),
    )
    op.create_index(
        "uq_destinations_name_nocase", "destinations", [sa.text("name COLLATE NOCASE")], unique=True
    )
    op.execute(
        sa.text(
            "INSERT INTO destinations "
            "(id,name,type,config_json,enabled,delivery_mode,allow_private_network,"
            "is_system,created_at,updated_at) VALUES "
            "(:id,'Built-in inbox','inbox','{}',1,'new_groups_only',0,1,"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        ).bindparams(id=str(uuid4()))
    )
    op.execute(
        "ALTER TABLE projects ADD COLUMN fallback_destination_id VARCHAR(36) "
        "REFERENCES destinations(id) ON DELETE RESTRICT"
    )
    op.create_table(
        "routing_rules",
        ident(),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("event_types_json", sa.Text(), nullable=False),
        sa.Column("match_mode", sa.String(3), nullable=False),
        sa.Column("match_behavior", sa.String(8), nullable=False),
        when("created_at"),
        when("updated_at"),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 120", name="ck_routing_rules_name"),
        sa.CheckConstraint("priority BETWEEN 0 AND 1000000", name="ck_routing_rules_priority"),
        sa.CheckConstraint(
            "event_types_json IN ("
            "'[\"knowledge_gap\"]',"
            "'[\"knowledge_correction\"]',"
            "'[\"knowledge_gap\",\"knowledge_correction\"]',"
            "'[\"knowledge_correction\",\"knowledge_gap\"]')",
            name="ck_routing_rules_event_types",
        ),
        sa.CheckConstraint("match_mode IN ('any','all')", name="ck_routing_rules_match_mode"),
        sa.CheckConstraint(
            "match_behavior IN ('continue','stop')", name="ck_routing_rules_match_behavior"
        ),
    )
    op.create_index(
        "ix_routing_rules_project_order",
        "routing_rules",
        ["project_id", "priority", "created_at", "id"],
    )
    op.create_table(
        "routing_rule_tags",
        sa.Column(
            "rule_id",
            sa.String(36),
            sa.ForeignKey("routing_rules.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id", sa.String(36), sa.ForeignKey("tags.id", ondelete="RESTRICT"), primary_key=True
        ),
    )
    op.create_table(
        "routing_rule_destinations",
        sa.Column(
            "rule_id",
            sa.String(36),
            sa.ForeignKey("routing_rules.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "destination_id",
            sa.String(36),
            sa.ForeignKey("destinations.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
    )
    op.create_table(
        "routing_matches",
        ident(),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("feedback_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rule_id", sa.String(36), sa.ForeignKey("routing_rules.id", ondelete="SET NULL")),
        sa.Column("rule_name", sa.String(120)),
        sa.Column("rule_order", sa.Integer()),
        sa.Column("via", sa.String(8), nullable=False),
        sa.Column(
            "destination_id", sa.String(36), sa.ForeignKey("destinations.id", ondelete="RESTRICT")
        ),
        sa.Column("outcome", sa.String(32), nullable=False),
        when("created_at"),
        sa.CheckConstraint("via IN ('rule','fallback')", name="ck_routing_matches_via"),
        sa.CheckConstraint(
            "outcome IN ('delivery_created','duplicate_destination',"
            "'destination_disabled','fallback_skipped_disabled')",
            name="ck_routing_matches_outcome",
        ),
    )
    op.create_index("ix_routing_matches_event", "routing_matches", ["event_id"])
    op.create_table(
        "deliveries",
        ident(),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("feedback_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "destination_id",
            sa.String(36),
            sa.ForeignKey("destinations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("manual_retry_count", sa.Integer(), nullable=False, server_default="0"),
        when("next_attempt_at", True),
        sa.Column("lease_owner", sa.String(128)),
        when("lease_until", True),
        when("last_attempt_at", True),
        sa.Column("last_error_category", sa.String(120)),
        sa.Column("last_error_message", sa.String(500)),
        sa.Column("last_http_status", sa.Integer()),
        when("delivered_at", True),
        when("created_at"),
        when("updated_at"),
        sa.UniqueConstraint("event_id", "destination_id", name="uq_deliveries_event_destination"),
        sa.CheckConstraint(
            "status IN ('pending','processing','delivered','retry_scheduled','failed')",
            name="ck_deliveries_status",
        ),
        sa.CheckConstraint(
            "last_error_message IS NULL OR length(last_error_message) <= 500",
            name="ck_deliveries_error_length",
        ),
    )
    op.create_index(
        "ix_deliveries_status_next_attempt", "deliveries", ["status", "next_attempt_at"]
    )
    op.create_index("ix_deliveries_destination_status", "deliveries", ["destination_id", "status"])
    op.create_index(
        "ix_deliveries_project_created", "deliveries", ["project_id", "created_at", "id"]
    )
    op.create_table(
        "delivery_attempts",
        ident(),
        sa.Column(
            "delivery_id",
            sa.String(36),
            sa.ForeignKey("deliveries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(8), nullable=False),
        when("started_at"),
        when("completed_at", True),
        sa.Column("result", sa.String(24)),
        sa.Column("http_status", sa.Integer()),
        sa.Column("error_category", sa.String(120)),
        sa.Column("sanitized_error", sa.String(500)),
        sa.Column("duration_ms", sa.Integer()),
        sa.UniqueConstraint("delivery_id", "attempt_number", name="uq_delivery_attempts_number"),
        sa.CheckConstraint("trigger IN ('auto','manual')", name="ck_delivery_attempts_trigger"),
        sa.CheckConstraint(
            "result IS NULL OR result IN ('success','retryable_failure',"
            "'permanent_failure','interrupted')",
            name="ck_delivery_attempts_result",
        ),
        sa.CheckConstraint(
            "sanitized_error IS NULL OR length(sanitized_error) <= 500",
            name="ck_delivery_attempts_error_length",
        ),
    )


def downgrade() -> None:
    for table in (
        "delivery_attempts",
        "deliveries",
        "routing_matches",
        "routing_rule_destinations",
        "routing_rule_tags",
        "routing_rules",
    ):
        op.drop_table(table)
    op.drop_column("projects", "fallback_destination_id")
    op.drop_table("destinations")
