"""Add encrypted LLM provider configuration."""

import sqlalchemy as sa
from alembic import op

revision = "0010_llm_providers"
down_revision = "0009_exact_dedup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_provider_configs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("provider_type", sa.String(30), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("model", sa.String(200)),
        sa.Column("deployment", sa.String(200)),
        sa.Column("api_version", sa.String(100)),
        sa.Column("structured_output", sa.String(20), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("allow_private_network", sa.Boolean(), nullable=False),
        sa.Column("api_key_env", sa.String(80)),
        sa.Column("headers_json", sa.Text(), nullable=False),
        sa.Column("secrets_ciphertext", sa.Text()),
        sa.Column("secrets_key_id", sa.String(8)),
        sa.Column("last_test_at", sa.DateTime(timezone=True)),
        sa.Column("last_test_ok", sa.Boolean()),
        sa.Column(
            "created_by_admin_id",
            sa.String(36),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider_type IN ('openai_compatible','azure_openai','local')",
            name="ck_llm_provider_type",
        ),
        sa.CheckConstraint(
            "structured_output IN ('json_schema','json_object','prompt_only')",
            name="ck_llm_structured_output",
        ),
        sa.CheckConstraint("timeout_seconds BETWEEN 1 AND 60", name="ck_llm_timeout"),
        sa.CheckConstraint(
            "(secrets_ciphertext IS NULL) = (secrets_key_id IS NULL)", name="ck_llm_secrets_pair"
        ),
    )
    op.create_index(
        "uq_llm_provider_name_nocase",
        "llm_provider_configs",
        [sa.text("name COLLATE NOCASE")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_llm_provider_name_nocase", table_name="llm_provider_configs")
    op.drop_table("llm_provider_configs")
