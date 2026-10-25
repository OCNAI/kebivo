"""Enable version two exact matching and occurrence provenance."""

import json

import re

import sqlalchemy as sa
from alembic import op

from kebivo.dedup.normalize import question_hash

revision = "0009_exact_dedup"
down_revision = "0008_feedback_management"
branch_labels = None
depends_on = None


def _rebuild_checks(upgrade: bool) -> None:
    bind = op.get_bind()
    for table, old, new in (
        ("feedback_group_history", "'event_deleted'", "'event_deleted','occurrence_added'"),
        (
            "routing_matches",
            "'fallback_skipped_disabled'",
            "'fallback_skipped_disabled','suppressed_duplicate'",
        ),
    ):
        sql = bind.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).scalar_one()
        source, target = (old, new) if upgrade else (new, old)
        assert source in sql
        replacement = sql.replace(source, target, 1)
        replacement = re.sub(
            rf'^CREATE TABLE "?{table}"?(?=\s*\()', f"CREATE TABLE {table}_new", replacement
        )
        indexes = (
            bind.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (table,),
            )
            .scalars()
            .all()
        )
        columns = [row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")]
        names = ",".join(columns)
        bind.exec_driver_sql(replacement)
        bind.exec_driver_sql(f"INSERT INTO {table}_new ({names}) SELECT {names} FROM {table}")
        bind.exec_driver_sql(f"DROP TABLE {table}")
        bind.exec_driver_sql(f"ALTER TABLE {table}_new RENAME TO {table}")
        for statement in indexes:
            bind.exec_driver_sql(statement)


def _data(version: int) -> None:
    bind = op.get_bind()
    for event_id, project_id, event_type, question in bind.exec_driver_sql(
        "SELECT id,project_id,event_type,question FROM feedback_events"
    ):
        bind.exec_driver_sql(
            "UPDATE feedback_events SET normalized_question_hash=?,question_hash_version=? WHERE id=?",
            (question_hash(project_id, event_type, question, version), version, event_id),
        )
    for project_id, raw in bind.exec_driver_sql("SELECT id,settings_json FROM projects"):
        settings = json.loads(raw)
        dedup = settings.get("deduplication", {})
        if version == 2:
            settings["deduplication"] = {
                "mode": "exact" if dedup.get("enabled", False) else "disabled"
            }
        else:
            settings["deduplication"] = {"enabled": dedup.get("mode") in {"exact", "semantic"}}
        bind.exec_driver_sql(
            "UPDATE projects SET settings_json=? WHERE id=?",
            (json.dumps(settings, separators=(",", ":")), project_id),
        )


def upgrade() -> None:
    with op.batch_alter_table("feedback_group_members") as batch:
        batch.add_column(
            sa.Column("triggered_reopen", sa.Boolean(), nullable=False, server_default="0")
        )
    _rebuild_checks(True)
    _data(2)


def downgrade() -> None:
    _data(1)
    op.get_bind().exec_driver_sql(
        "DELETE FROM feedback_group_history WHERE action='occurrence_added'"
    )
    op.get_bind().exec_driver_sql(
        "DELETE FROM routing_matches WHERE outcome='suppressed_duplicate'"
    )
    _rebuild_checks(False)
    with op.batch_alter_table("feedback_group_members") as batch:
        batch.drop_column("triggered_reopen")
