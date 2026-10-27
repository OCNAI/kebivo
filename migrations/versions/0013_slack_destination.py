"""Permit Slack destinations in the existing delivery table."""

import sqlalchemy as sa
from alembic import op

from migrations.slack_schema import destinations

revision = "0013_slack_destination"
down_revision = "0012_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _replace_type_check("type IN ('webhook','smtp','inbox','slack')")


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM destinations WHERE type='slack' LIMIT 1"))
        .first()
    ):
        raise RuntimeError("Remove Slack destinations before downgrading")
    _replace_type_check("type IN ('webhook','smtp','inbox')")


def _replace_type_check(expression: str) -> None:
    bind = op.get_bind()
    # SQLite requires FK toggles outside its transaction; keep Alembic's transaction open.
    bind.connection.driver_connection.commit()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table(
            "destinations", recreate="always", copy_from=destinations
        ) as batch:
            batch.drop_constraint("ck_destinations_type", type_="check")
            batch.create_check_constraint("ck_destinations_type", expression)
    finally:
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")
    if bind.exec_driver_sql("PRAGMA foreign_key_check").first():
        raise RuntimeError("Destination migration broke a foreign key")
