"""Weekly digest delivery mode and digest delivery rows."""

import sqlalchemy as sa
from alembic import op

revision = "0019_weekly_digest"
down_revision = "0018_star_prompt_dismissal"
branch_labels = None
depends_on = None

DESTINATION_COLUMNS = (
    "id, name, type, config_json, secrets_ciphertext, secrets_key_id, enabled, delivery_mode, "
    "allow_private_network, is_system, created_by_admin_id, created_at, updated_at, is_sample"
)
MATCH_COLUMNS = (
    "id, event_id, rule_id, rule_name, rule_order, via, destination_id, outcome, created_at"
)
DELIVERY_COLUMNS = (
    "id, event_id, project_id, destination_id, status, attempt_count, max_attempts, "
    "manual_retry_count, next_attempt_at, lease_owner, lease_until, last_attempt_at, "
    "last_error_category, last_error_message, last_http_status, delivered_at, created_at, "
    "updated_at"
)


def _destinations(digest: bool) -> str:
    modes = "'new_groups_only','every_occurrence'" + (",'weekly_digest'" if digest else "")
    extra = (
        "digest_weekday INTEGER, digest_hour INTEGER, digest_checked_through DATETIME,"
        if digest
        else ""
    )
    checks = (
        "CONSTRAINT ck_destinations_digest_weekday CHECK "
        "(digest_weekday IS NULL OR digest_weekday BETWEEN 0 AND 6),"
        "CONSTRAINT ck_destinations_digest_hour CHECK "
        "(digest_hour IS NULL OR digest_hour BETWEEN 0 AND 23),"
        if digest
        else ""
    )
    return f"""CREATE TABLE destinations (
        id VARCHAR(36) NOT NULL, name VARCHAR(120) NOT NULL, type VARCHAR(20) NOT NULL,
        config_json TEXT DEFAULT '{{}}' NOT NULL, secrets_ciphertext TEXT,
        secrets_key_id VARCHAR(8), enabled BOOLEAN DEFAULT 1 NOT NULL,
        delivery_mode VARCHAR(30) DEFAULT 'new_groups_only' NOT NULL,
        allow_private_network BOOLEAN DEFAULT 0 NOT NULL, is_system BOOLEAN DEFAULT 0 NOT NULL,
        created_by_admin_id VARCHAR(36), created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL, is_sample BOOLEAN DEFAULT 0 NOT NULL, {extra}
        PRIMARY KEY (id),
        CONSTRAINT ck_destinations_config_length
            CHECK (length(CAST(config_json AS BLOB)) <= 16384),
        CONSTRAINT ck_destinations_secrets_pair
            CHECK ((secrets_ciphertext IS NULL) = (secrets_key_id IS NULL)),
        CONSTRAINT ck_destinations_name CHECK (length(name) BETWEEN 1 AND 120),
        CONSTRAINT ck_destinations_delivery_mode CHECK (delivery_mode IN ({modes})),
        CONSTRAINT ck_destinations_type CHECK (type IN ('webhook','smtp','inbox','slack')),
        {checks}
        FOREIGN KEY(created_by_admin_id) REFERENCES admin_users (id) ON DELETE SET NULL
    )"""


def _matches(digest: bool) -> str:
    outcomes = (
        "'delivery_created','duplicate_destination','destination_disabled',"
        "'fallback_skipped_disabled','suppressed_duplicate'"
        + (",'digest_queued'" if digest else "")
    )
    return f"""CREATE TABLE routing_matches (
        id VARCHAR(36) NOT NULL, event_id VARCHAR(36) NOT NULL, rule_id VARCHAR(36),
        rule_name VARCHAR(120), rule_order INTEGER, via VARCHAR(8) NOT NULL,
        destination_id VARCHAR(36), outcome VARCHAR(32) NOT NULL, created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT ck_routing_matches_via CHECK (via IN ('rule','fallback')),
        CONSTRAINT ck_routing_matches_outcome CHECK (outcome IN ({outcomes})),
        FOREIGN KEY(event_id) REFERENCES feedback_events (id) ON DELETE CASCADE,
        FOREIGN KEY(rule_id) REFERENCES routing_rules (id) ON DELETE SET NULL,
        FOREIGN KEY(destination_id) REFERENCES destinations (id) ON DELETE RESTRICT
    )"""


def _deliveries(digest: bool) -> str:
    event = "event_id VARCHAR(36)," if digest else "event_id VARCHAR(36) NOT NULL,"
    extra = (
        "digest_period_start DATETIME, digest_period_end DATETIME,"
        "CONSTRAINT uq_deliveries_digest_period "
        "UNIQUE (destination_id, project_id, digest_period_end),"
        "CONSTRAINT ck_deliveries_kind CHECK ((event_id IS NULL) = "
        "(digest_period_end IS NOT NULL) AND "
        "(digest_period_start IS NULL) = (digest_period_end IS NULL)),"
        if digest
        else ""
    )
    return f"""CREATE TABLE deliveries (
        id VARCHAR(36) NOT NULL, {event} project_id VARCHAR(36) NOT NULL,
        destination_id VARCHAR(36) NOT NULL, status VARCHAR(20) DEFAULT 'pending' NOT NULL,
        attempt_count INTEGER DEFAULT '0' NOT NULL, max_attempts INTEGER DEFAULT '6' NOT NULL,
        manual_retry_count INTEGER DEFAULT '0' NOT NULL, next_attempt_at DATETIME,
        lease_owner VARCHAR(128), lease_until DATETIME, last_attempt_at DATETIME,
        last_error_category VARCHAR(120), last_error_message VARCHAR(500),
        last_http_status INTEGER, delivered_at DATETIME, created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL, {extra}
        PRIMARY KEY (id),
        CONSTRAINT uq_deliveries_event_destination UNIQUE (event_id, destination_id),
        CONSTRAINT ck_deliveries_status CHECK
            (status IN ('pending','processing','delivered','retry_scheduled','failed')),
        CONSTRAINT ck_deliveries_error_length
            CHECK (last_error_message IS NULL OR length(last_error_message) <= 500),
        FOREIGN KEY(event_id) REFERENCES feedback_events (id) ON DELETE CASCADE,
        FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
        FOREIGN KEY(destination_id) REFERENCES destinations (id) ON DELETE RESTRICT
    )"""


INDEXES = {
    "destinations": (
        "CREATE UNIQUE INDEX uq_destinations_name_nocase ON destinations (name COLLATE NOCASE)",
    ),
    "routing_matches": ("CREATE INDEX ix_routing_matches_event ON routing_matches (event_id)",),
    "deliveries": (
        "CREATE INDEX ix_deliveries_status_next_attempt ON deliveries (status, next_attempt_at)",
        "CREATE INDEX ix_deliveries_destination_status ON deliveries (destination_id, status)",
        "CREATE INDEX ix_deliveries_project_created ON deliveries (project_id, created_at, id)",
    ),
}


def _rebuild(digest: bool) -> None:
    bind = op.get_bind()
    # migrations/env.py runs every migration in one transaction with foreign keys off.
    for table, ddl, columns in (
        ("destinations", _destinations(digest), DESTINATION_COLUMNS),
        ("routing_matches", _matches(digest), MATCH_COLUMNS),
        ("deliveries", _deliveries(digest), DELIVERY_COLUMNS),
    ):
        # Releases before 0.5.1 could leave this behind after a failed run.
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS _new_{table}")
        # Renaming the old table would rewrite other tables' foreign keys to it.
        bind.exec_driver_sql(ddl.replace(f"TABLE {table} (", f"TABLE _new_{table} (", 1))
        bind.exec_driver_sql(f"INSERT INTO _new_{table} ({columns}) SELECT {columns} FROM {table}")
        bind.exec_driver_sql(f"DROP TABLE {table}")
        bind.exec_driver_sql(f"ALTER TABLE _new_{table} RENAME TO {table}")
        for statement in INDEXES[table]:
            bind.exec_driver_sql(statement)
    broken = bind.exec_driver_sql("PRAGMA foreign_key_check").first()
    if broken:
        raise RuntimeError(
            f"Weekly digest migration found a {broken[0]} row (rowid {broken[1]}) "
            f"that points to a missing {broken[2]} row; fix or delete it, then retry"
        )


def upgrade() -> None:
    _rebuild(True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM destinations WHERE delivery_mode='weekly_digest' LIMIT 1")
    ).first():
        raise RuntimeError("Switch weekly digest destinations to another mode before downgrading")
    bind.execute(
        sa.text(
            "DELETE FROM delivery_attempts WHERE delivery_id IN "
            "(SELECT id FROM deliveries WHERE event_id IS NULL)"
        )
    )
    bind.execute(sa.text("DELETE FROM deliveries WHERE event_id IS NULL"))
    bind.execute(sa.text("DELETE FROM routing_matches WHERE outcome='digest_queued'"))
    _rebuild(False)
