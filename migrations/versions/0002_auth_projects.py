"""Add admin authentication and project/token schema."""

from alembic import op
from sqlalchemy import Column, String

from migrations.legacy_models import Base

revision = "0002_auth_projects"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

NEW_TABLES = (
    "admin_users",
    "admin_sessions",
    "login_attempts",
    "projects",
    "tags",
    "project_tags",
    "api_tokens",
)


def upgrade() -> None:
    # Add nullable/defaulted columns so existing Phase 0 audit rows remain valid.
    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.add_column(Column("actor_type", String(32), nullable=False, server_default="user"))
        batch_op.add_column(Column("actor_ref", String(128)))

    Base.metadata.create_all(
        bind=op.get_bind(), tables=[Base.metadata.tables[name] for name in NEW_TABLES]
    )


def downgrade() -> None:
    Base.metadata.drop_all(
        bind=op.get_bind(), tables=[Base.metadata.tables[name] for name in reversed(NEW_TABLES)]
    )
    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.drop_column("actor_ref")
        batch_op.drop_column("actor_type")
