"""Record explicit revocation for sessions created before the P1-2 model update."""

from alembic import op
from sqlalchemy import Column, DateTime, inspect

revision = "0003_admin_session_revocation"
down_revision = "0002_auth_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {column["name"] for column in inspect(connection).get_columns("admin_sessions")}
    if "revoked_at" not in columns:
        with op.batch_alter_table("admin_sessions") as batch_op:
            batch_op.add_column(Column("revoked_at", DateTime(timezone=True), nullable=True))
    admin_columns = {
        column["name"] for column in inspect(connection).get_columns("admin_users")
    }
    if "last_login_at" not in admin_columns:
        with op.batch_alter_table("admin_users") as batch_op:
            batch_op.add_column(Column("last_login_at", DateTime(timezone=True), nullable=True))
    indexes = {index["name"] for index in inspect(connection).get_indexes("login_attempts")}
    if "ix_login_attempts_attempted_at" not in indexes:
        op.create_index(
            "ix_login_attempts_attempted_at", "login_attempts", ["attempted_at"]
        )


def downgrade() -> None:
    connection = op.get_bind()
    indexes = {index["name"] for index in inspect(connection).get_indexes("login_attempts")}
    if "ix_login_attempts_attempted_at" in indexes:
        op.drop_index("ix_login_attempts_attempted_at", table_name="login_attempts")
    columns = {column["name"] for column in inspect(connection).get_columns("admin_sessions")}
    if "revoked_at" in columns:
        with op.batch_alter_table("admin_sessions") as batch_op:
            batch_op.drop_column("revoked_at")
    admin_columns = {
        column["name"] for column in inspect(connection).get_columns("admin_users")
    }
    if "last_login_at" in admin_columns:
        with op.batch_alter_table("admin_users") as batch_op:
            batch_op.drop_column("last_login_at")
