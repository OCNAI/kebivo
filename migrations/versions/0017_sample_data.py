"""Mark removable sample projects and destinations."""

import sqlalchemy as sa
from alembic import op

revision = "0017_sample_data"
down_revision = "0016_embedding_space_revision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("is_sample", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "destinations",
        sa.Column("is_sample", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("is_sample")
    with op.batch_alter_table("destinations") as batch:
        batch.drop_column("is_sample")
