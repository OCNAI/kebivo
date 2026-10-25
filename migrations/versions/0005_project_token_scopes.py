"""Persist exact project token scopes and enforce unique lookup prefixes."""

import sqlalchemy as sa
from alembic import op

revision = "0005_project_token_scopes"
down_revision = "0004_project_settings_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("api_tokens")}
    if "scopes_json" not in columns:
        op.add_column(
            "api_tokens",
            sa.Column("scopes_json", sa.Text(), nullable=False, server_default='["feedback:gap"]'),
        )
    indexes = {index["name"] for index in sa.inspect(connection).get_indexes("api_tokens")}
    if "ix_api_tokens_token_prefix" not in indexes:
        op.create_index("ix_api_tokens_token_prefix", "api_tokens", ["token_prefix"], unique=True)


def downgrade() -> None:
    connection = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(connection).get_indexes("api_tokens")}
    if "ix_api_tokens_token_prefix" in indexes:
        op.drop_index("ix_api_tokens_token_prefix", table_name="api_tokens")
    columns = {column["name"] for column in sa.inspect(connection).get_columns("api_tokens")}
    if "scopes_json" in columns:
        op.drop_column("api_tokens", "scopes_json")
