"""Frozen destination table schema for the Slack constraint migration."""

import sqlalchemy as sa

destinations = sa.Table(
    "destinations",
    sa.MetaData(),
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("type", sa.String(20), nullable=False),
    sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
    sa.Column("secrets_ciphertext", sa.Text()),
    sa.Column("secrets_key_id", sa.String(8)),
    sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
    sa.Column("delivery_mode", sa.String(30), nullable=False, server_default="new_groups_only"),
    sa.Column("allow_private_network", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    sa.Column(
        "created_by_admin_id", sa.String(36), sa.ForeignKey("admin_users.id", ondelete="SET NULL")
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("length(name) BETWEEN 1 AND 120", name="ck_destinations_name"),
    sa.CheckConstraint("type IN ('webhook','smtp','inbox','slack')", name="ck_destinations_type"),
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
sa.Index(
    "uq_destinations_name_nocase", sa.text("name COLLATE NOCASE"), unique=True, _table=destinations
)
