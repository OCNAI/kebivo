"""Isolate stored vectors by the provider configuration that produced them."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0016_embedding_space_revision"
down_revision = "0015_embedding_backfills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_provider_configs",
        sa.Column("embedding_revision", sa.String(36), nullable=False, server_default=""),
    )
    db = op.get_bind()
    db.execute(
        sa.text("UPDATE llm_provider_configs SET embedding_revision=lower(hex(randomblob(16)))")
    )
    op.add_column("question_embeddings", sa.Column("embedding_revision", sa.String(36)))
    op.add_column("embedding_backfills", sa.Column("embedding_revision", sa.String(36)))
    # Legacy vectors have no trustworthy configuration revision; rebuild active projects.
    for project_id, settings_json in db.execute(sa.text("SELECT id, settings_json FROM projects")):
        dedup = json.loads(settings_json).get("deduplication", {})
        semantic = dedup.get("semantic", {})
        if dedup.get("mode") != "semantic" or semantic.get("method") != "embeddings":
            continue
        provider_id = semantic.get("provider_id")
        provider_revision = db.scalar(
            sa.text("SELECT embedding_revision FROM llm_provider_configs WHERE id=:id"),
            {"id": provider_id},
        )
        if provider_revision is None:
            continue
        db.execute(
            sa.text("""
                INSERT INTO embedding_backfills
                  (project_id, provider_id, embedding_revision, model, dims, remaining)
                VALUES (:project, :provider, :revision, :model, NULL, :remaining)
                ON CONFLICT(project_id) DO UPDATE SET
                  provider_id=excluded.provider_id,
                  embedding_revision=excluded.embedding_revision,
                  model=excluded.model,
                  dims=NULL,
                  remaining=excluded.remaining,
                  cursor_seen_at=NULL,
                  cursor_group_id=NULL
            """),
            {
                "project": project_id,
                "provider": provider_id,
                "revision": provider_revision,
                "model": semantic["embedding_model"],
                "remaining": semantic.get("max_scan", 2000),
            },
        )


def downgrade() -> None:
    for table in ("embedding_backfills", "question_embeddings", "llm_provider_configs"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("embedding_revision")
