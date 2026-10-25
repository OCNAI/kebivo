"""Add feedback groups, provenance and a securely deleted FTS5 index."""

import sqlite3
from uuid import uuid4

from alembic import op
from sqlalchemy.exc import DBAPIError

revision = "0008_feedback_management"
down_revision = "0007_routing_destinations"
branch_labels = None
depends_on = None

GROUP_TABLES = (
    "feedback_groups",
    "feedback_group_members",
    "feedback_group_notes",
    "feedback_group_history",
)

GROUP_SCHEMA = (
    """CREATE TABLE feedback_groups (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        project_id VARCHAR(36) NOT NULL,
        event_type VARCHAR(32) NOT NULL,
        representative_event_id VARCHAR(36) NOT NULL,
        status VARCHAR(20) DEFAULT 'open' NOT NULL,
        status_changed_at DATETIME NOT NULL,
        resolved_at DATETIME,
        occurrence_count INTEGER NOT NULL,
        first_seen_at DATETIME NOT NULL,
        last_seen_at DATETIME NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        CONSTRAINT ck_feedback_groups_type CHECK (event_type IN ('knowledge_gap','knowledge_correction')),
        CONSTRAINT ck_feedback_groups_status CHECK (status IN ('open','in_progress','resolved','dismissed')),
        CONSTRAINT ck_feedback_groups_count CHECK (occurrence_count >= 1),
        FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
        FOREIGN KEY(representative_event_id) REFERENCES feedback_events (id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE feedback_group_members (
        event_id VARCHAR(36) NOT NULL PRIMARY KEY,
        group_id VARCHAR(36) NOT NULL,
        match_method VARCHAR(20) NOT NULL,
        added_at DATETIME NOT NULL,
        CONSTRAINT ck_feedback_group_members_method CHECK
            (match_method IN ('initial','manual_merge','manual_split','exact','semantic')),
        FOREIGN KEY(event_id) REFERENCES feedback_events (id) ON DELETE CASCADE,
        FOREIGN KEY(group_id) REFERENCES feedback_groups (id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE feedback_group_notes (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        group_id VARCHAR(36) NOT NULL,
        author_admin_id VARCHAR(36),
        body TEXT NOT NULL,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_feedback_group_notes_body CHECK (length(body) BETWEEN 1 AND 4000),
        FOREIGN KEY(group_id) REFERENCES feedback_groups (id) ON DELETE CASCADE,
        FOREIGN KEY(author_admin_id) REFERENCES admin_users (id) ON DELETE SET NULL
    )""",
    """CREATE TABLE feedback_group_history (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        group_id VARCHAR(36) NOT NULL,
        origin_group_id VARCHAR(36),
        action VARCHAR(32) NOT NULL,
        actor_type VARCHAR(10) NOT NULL,
        actor_admin_id VARCHAR(36),
        from_status VARCHAR(20),
        to_status VARCHAR(20),
        details_json TEXT DEFAULT '{}' NOT NULL,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_feedback_group_history_action CHECK
            (action IN ('created','status_changed','representative_changed','tags_changed',
                        'note_added','note_deleted','merged','split_out','split_in','event_deleted')),
        CONSTRAINT ck_feedback_group_history_actor CHECK (actor_type IN ('system','admin')),
        CONSTRAINT ck_feedback_group_history_details CHECK
            (length(CAST(details_json AS BLOB)) <= 4096),
        FOREIGN KEY(group_id) REFERENCES feedback_groups (id) ON DELETE CASCADE,
        FOREIGN KEY(actor_admin_id) REFERENCES admin_users (id) ON DELETE SET NULL
    )""",
)

GROUP_INDEXES = (
    "CREATE INDEX ix_feedback_groups_project_status_last ON feedback_groups (project_id, status, last_seen_at, id)",
    "CREATE INDEX ix_feedback_groups_last ON feedback_groups (last_seen_at, id)",
    "CREATE INDEX ix_feedback_groups_status_last ON feedback_groups (status, last_seen_at, id)",
    "CREATE INDEX ix_feedback_groups_count ON feedback_groups (occurrence_count, id)",
    "CREATE INDEX ix_feedback_groups_first ON feedback_groups (first_seen_at, id)",
    "CREATE INDEX ix_feedback_group_members_group_event ON feedback_group_members (group_id, event_id)",
    "CREATE INDEX ix_feedback_group_notes_group_created ON feedback_group_notes (group_id, created_at, id)",
    "CREATE INDEX ix_feedback_group_history_group_created ON feedback_group_history (group_id, created_at, id)",
)


def _preflight(bind: object) -> None:
    if sqlite3.sqlite_version_info < (3, 42, 0):
        raise RuntimeError("Feedback migration 0008 requires SQLite 3.42 or newer")
    try:
        bind.exec_driver_sql("CREATE VIRTUAL TABLE temp.feedback_fts5_probe USING fts5(value)")
    except DBAPIError as exc:
        raise RuntimeError("Feedback migration 0008 requires SQLite FTS5") from exc
    finally:
        bind.exec_driver_sql("DROP TABLE IF EXISTS temp.feedback_fts5_probe")


def upgrade() -> None:
    bind = op.get_bind()
    _preflight(bind)
    bind.exec_driver_sql("PRAGMA secure_delete=ON")
    bind.exec_driver_sql("PRAGMA foreign_keys=ON")
    # Rebuild with a fixed FK order; batch reflection can permute FK SQL.
    bind.exec_driver_sql(
        "CREATE TABLE feedback_event_tags_new ("
        "event_id VARCHAR(36) NOT NULL, tag_id VARCHAR(36) NOT NULL, "
        "from_default BOOLEAN DEFAULT 0 NOT NULL, "
        "from_submission BOOLEAN DEFAULT 0 NOT NULL, "
        "from_admin BOOLEAN DEFAULT 0 NOT NULL, "
        "PRIMARY KEY (event_id, tag_id), "
        "CONSTRAINT ck_feedback_event_tags_source CHECK "
        "(from_default = 1 OR from_submission = 1 OR from_admin = 1), "
        "FOREIGN KEY(event_id) REFERENCES feedback_events (id) ON DELETE CASCADE, "
        "FOREIGN KEY(tag_id) REFERENCES tags (id) ON DELETE RESTRICT)"
    )
    bind.exec_driver_sql(
        "INSERT INTO feedback_event_tags_new "
        "(event_id,tag_id,from_default,from_submission) "
        "SELECT event_id,tag_id,from_default,from_submission FROM feedback_event_tags"
    )
    bind.exec_driver_sql("DROP TABLE feedback_event_tags")
    bind.exec_driver_sql("ALTER TABLE feedback_event_tags_new RENAME TO feedback_event_tags")
    bind.exec_driver_sql(
        "CREATE INDEX ix_feedback_event_tags_tag_event ON feedback_event_tags(tag_id,event_id)"
    )
    for statement in (*GROUP_SCHEMA, *GROUP_INDEXES):
        bind.exec_driver_sql(statement)
    bind.exec_driver_sql(
        "CREATE VIRTUAL TABLE feedback_search USING fts5("
        "event_id UNINDEXED, question, correction, previous_answer, context, "
        "tokenize='unicode61 remove_diacritics 2')"
    )
    bind.exec_driver_sql("INSERT INTO feedback_search(feedback_search, rank) VALUES('secure-delete', 1)")
    bind.exec_driver_sql(
        "CREATE TRIGGER feedback_search_insert AFTER INSERT ON feedback_events BEGIN "
        "INSERT INTO feedback_search(event_id, question, correction, previous_answer, context) "
        "VALUES (new.id, new.question, new.correction, new.previous_answer, new.context); END"
    )
    bind.exec_driver_sql(
        "CREATE TRIGGER feedback_search_delete AFTER DELETE ON feedback_events BEGIN "
        "DELETE FROM feedback_search WHERE event_id = old.id; END"
    )
    # Existing event IDs and timestamps stay unchanged, as do deliveries and attempts.
    events = bind.exec_driver_sql(
        "SELECT id, project_id, event_type, created_at FROM feedback_events ORDER BY id"
    ).all()
    for event_id, project_id, event_type, created_at in events:
        group_id = str(uuid4())
        bind.exec_driver_sql(
            "INSERT INTO feedback_groups "
            "(id,project_id,event_type,representative_event_id,status,status_changed_at,"
            "occurrence_count,first_seen_at,last_seen_at,created_at,updated_at) "
            "VALUES (?,?,?,?,'open',?,1,?,?,?,?)",
            (group_id, project_id, event_type, event_id, created_at, created_at,
             created_at, created_at, created_at),
        )
        bind.exec_driver_sql(
            "INSERT INTO feedback_group_members(event_id,group_id,match_method,added_at) "
            "VALUES (?,?,'initial',?)", (event_id, group_id, created_at),
        )
        bind.exec_driver_sql(
            "INSERT INTO feedback_group_history"
            "(id,group_id,action,actor_type,details_json,created_at) "
            "VALUES (?,?,'created','system','{}',?)", (str(uuid4()), group_id, created_at),
        )
    bind.exec_driver_sql(
        "INSERT INTO feedback_search(event_id,question,correction,previous_answer,context) "
        "SELECT id,question,correction,previous_answer,context FROM feedback_events"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("PRAGMA secure_delete=ON")
    bind.exec_driver_sql("PRAGMA foreign_keys=ON")
    bind.exec_driver_sql("DROP TRIGGER feedback_search_delete")
    bind.exec_driver_sql("DROP TRIGGER feedback_search_insert")
    bind.exec_driver_sql("DROP TABLE feedback_search")
    for name in reversed(GROUP_TABLES):
        bind.exec_driver_sql(f"DROP TABLE {name}")
    bind.exec_driver_sql(
        "CREATE TABLE feedback_event_tags_new ("
        "event_id VARCHAR(36) NOT NULL, tag_id VARCHAR(36) NOT NULL, "
        "from_default BOOLEAN DEFAULT 0 NOT NULL, "
        "from_submission BOOLEAN DEFAULT 0 NOT NULL, "
        "PRIMARY KEY (event_id, tag_id), "
        "CONSTRAINT ck_feedback_event_tags_source CHECK "
        "(from_default = 1 OR from_submission = 1), "
        "FOREIGN KEY(event_id) REFERENCES feedback_events (id) ON DELETE CASCADE, "
        "FOREIGN KEY(tag_id) REFERENCES tags (id) ON DELETE RESTRICT)"
    )
    bind.exec_driver_sql(
        "INSERT INTO feedback_event_tags_new "
        "SELECT event_id,tag_id,from_default,from_submission FROM feedback_event_tags "
        "WHERE from_default = 1 OR from_submission = 1"
    )
    bind.exec_driver_sql("DROP TABLE feedback_event_tags")
    bind.exec_driver_sql("ALTER TABLE feedback_event_tags_new RENAME TO feedback_event_tags")
    bind.exec_driver_sql(
        "CREATE INDEX ix_feedback_event_tags_tag_event ON feedback_event_tags(tag_id,event_id)"
    )
