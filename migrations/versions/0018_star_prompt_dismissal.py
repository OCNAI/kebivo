"""Persist administrator star prompt dismissal."""

import sqlalchemy as sa
from alembic import op

revision = "0018_star_prompt_dismissal"
down_revision = "0017_sample_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("admin_users", sa.Column("star_prompt_dismissed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("admin_users") as batch:
        batch.drop_column("star_prompt_dismissed_at")
