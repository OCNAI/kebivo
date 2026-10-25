"""Add project settings and archive lifecycle state."""

from alembic import op
import sqlalchemy as sa

revision = "0004_project_settings_lifecycle"
down_revision = "0003_admin_session_revocation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0002 uses current metadata.create_all for the initial project tables, so a
    # fresh install may already have these columns before reaching this revision.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("projects")}
    if "settings_json" not in columns:
        op.add_column(
            "projects",
            sa.Column(
                "settings_json",
                sa.Text(),
                nullable=False,
                server_default='{"corrections":{"enabled":false},"deduplication":{"enabled":false}}',
            ),
        )
    if "archived_at" not in columns:
        op.add_column("projects", sa.Column("archived_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("projects")}
    if "archived_at" in columns:
        op.drop_column("projects", "archived_at")
    if "settings_json" in columns:
        op.drop_column("projects", "settings_json")
