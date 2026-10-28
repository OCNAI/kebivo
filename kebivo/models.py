"""Relational foundation for the approved Phase 0 entities."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_utc() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Record(Base):
    __tablename__ = "records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    external_id: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (Index("ix_records_owner_created", "owner_id", "created_at"),)


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    event_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_events.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(30), default="pending", server_default="pending", nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="ck_processing_jobs_status",
        ),
        Index("ix_processing_jobs_status_next_attempt", "status", "next_attempt_at"),
    )


class Destination(Base):
    __tablename__ = "destinations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    config_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}", nullable=False
    )
    secrets_ciphertext: Mapped[str | None] = mapped_column(Text)
    secrets_key_id: Mapped[str | None] = mapped_column(String(8))
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    delivery_mode: Mapped[str] = mapped_column(
        String(30), default="new_groups_only", server_default="new_groups_only", nullable=False
    )
    allow_private_network: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    created_by_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 120", name="ck_destinations_name"),
        CheckConstraint("type IN ('webhook','smtp','inbox','slack')", name="ck_destinations_type"),
        CheckConstraint(
            "length(CAST(config_json AS BLOB)) <= 16384", name="ck_destinations_config_length"
        ),
        CheckConstraint(
            "delivery_mode IN ('new_groups_only','every_occurrence')",
            name="ck_destinations_delivery_mode",
        ),
        CheckConstraint(
            "(secrets_ciphertext IS NULL) = (secrets_key_id IS NULL)",
            name="ck_destinations_secrets_pair",
        ),
        Index("uq_destinations_name_nocase", text("name COLLATE NOCASE"), unique=True),
    )


class LlmProviderConfig(Base):
    __tablename__ = "llm_provider_configs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(30), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    model: Mapped[str | None] = mapped_column(String(200))
    deployment: Mapped[str | None] = mapped_column(String(200))
    api_version: Mapped[str | None] = mapped_column(String(100))
    structured_output: Mapped[str] = mapped_column(String(20), nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_private_network: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    api_key_env: Mapped[str | None] = mapped_column(String(80))
    headers_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    secrets_ciphertext: Mapped[str | None] = mapped_column(Text)
    secrets_key_id: Mapped[str | None] = mapped_column(String(8))
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    created_by_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    __table_args__ = (
        CheckConstraint(
            "provider_type IN ('openai_compatible','azure_openai','local')",
            name="ck_llm_provider_type",
        ),
        CheckConstraint(
            "structured_output IN ('json_schema','json_object','prompt_only')",
            name="ck_llm_structured_output",
        ),
        CheckConstraint("timeout_seconds BETWEEN 1 AND 60", name="ck_llm_timeout"),
        CheckConstraint(
            "(secrets_ciphertext IS NULL) = (secrets_key_id IS NULL)", name="ck_llm_secrets_pair"
        ),
        Index("uq_llm_provider_name_nocase", text("name COLLATE NOCASE"), unique=True),
    )


class DedupEvaluation(Base):
    __tablename__ = "dedup_evaluations"
    event_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_events.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("llm_provider_configs.id", ondelete="SET NULL")
    )
    model: Mapped[str | None] = mapped_column(String(200))
    method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="judge", server_default="judge"
    )
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    matched_group_id: Mapped[str | None] = mapped_column(
        ForeignKey("feedback_groups.id", ondelete="SET NULL")
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(String(500))
    error_category: Mapped[str | None] = mapped_column(String(40))
    http_status: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('matched','no_match','low_confidence','invalid_response',"
            "'invalid_candidate','no_candidates','provider_unavailable',"
            "'provider_error','timeout','skipped_group_changed')",
            name="ck_dedup_evaluations_outcome",
        ),
        CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1",
            name="ck_dedup_evaluations_confidence",
        ),
        CheckConstraint(
            "reason IS NULL OR length(reason) <= 500", name="ck_dedup_evaluations_reason"
        ),
        Index("ix_dedup_evaluations_project_created", "project_id", "created_at"),
    )


class QuestionEmbedding(Base):
    __tablename__ = "question_embeddings"
    event_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_events.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("llm_provider_configs.id", ondelete="SET NULL")
    )
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    dims: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    __table_args__ = (Index("ix_question_embeddings_lookup", "project_id", "event_type", "model"),)


class EmbeddingBackfill(Base):
    __tablename__ = "embedding_backfills"
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    provider_id: Mapped[str] = mapped_column(String(36), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    dims: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining: Mapped[int] = mapped_column(Integer, nullable=False)
    cursor_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor_group_id: Mapped[str | None] = mapped_column(String(36))


class RoutingRule(Base):
    __tablename__ = "routing_rules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    event_types_json: Mapped[str] = mapped_column(Text, nullable=False)
    match_mode: Mapped[str] = mapped_column(String(3), nullable=False)
    match_behavior: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 120", name="ck_routing_rules_name"),
        CheckConstraint("priority BETWEEN 0 AND 1000000", name="ck_routing_rules_priority"),
        CheckConstraint(
            "event_types_json IN ("
            "'[\"knowledge_gap\"]',"
            "'[\"knowledge_correction\"]',"
            '\'["knowledge_gap","knowledge_correction"]\','
            '\'["knowledge_correction","knowledge_gap"]\')',
            name="ck_routing_rules_event_types",
        ),
        CheckConstraint("match_mode IN ('any','all')", name="ck_routing_rules_match_mode"),
        CheckConstraint(
            "match_behavior IN ('continue','stop')", name="ck_routing_rules_match_behavior"
        ),
        Index("ix_routing_rules_project_order", "project_id", "priority", "created_at", "id"),
    )


class RoutingRuleTag(Base):
    __tablename__ = "routing_rule_tags"
    rule_id: Mapped[str] = mapped_column(
        ForeignKey("routing_rules.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(
        ForeignKey("tags.id", ondelete="RESTRICT"), primary_key=True
    )


class RoutingRuleDestination(Base):
    __tablename__ = "routing_rule_destinations"
    rule_id: Mapped[str] = mapped_column(
        ForeignKey("routing_rules.id", ondelete="CASCADE"), primary_key=True
    )
    destination_id: Mapped[str] = mapped_column(
        ForeignKey("destinations.id", ondelete="RESTRICT"), primary_key=True
    )


class RoutingMatch(Base):
    __tablename__ = "routing_matches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    event_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_events.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[str | None] = mapped_column(ForeignKey("routing_rules.id", ondelete="SET NULL"))
    rule_name: Mapped[str | None] = mapped_column(String(120))
    rule_order: Mapped[int | None] = mapped_column(Integer)
    via: Mapped[str] = mapped_column(String(8), nullable=False)
    destination_id: Mapped[str | None] = mapped_column(
        ForeignKey("destinations.id", ondelete="RESTRICT")
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (
        CheckConstraint("via IN ('rule','fallback')", name="ck_routing_matches_via"),
        CheckConstraint(
            "outcome IN ('delivery_created','duplicate_destination',"
            "'destination_disabled','fallback_skipped_disabled','suppressed_duplicate')",
            name="ck_routing_matches_outcome",
        ),
        Index("ix_routing_matches_event", "event_id"),
    )


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    event_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_events.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    destination_id: Mapped[str] = mapped_column(
        ForeignKey("destinations.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, default=6, server_default="6", nullable=False
    )
    manual_retry_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_category: Mapped[str | None] = mapped_column(String(120))
    last_error_message: Mapped[str | None] = mapped_column(String(500))
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    __table_args__ = (
        UniqueConstraint("event_id", "destination_id", name="uq_deliveries_event_destination"),
        CheckConstraint(
            "status IN ('pending','processing','delivered','retry_scheduled','failed')",
            name="ck_deliveries_status",
        ),
        CheckConstraint(
            "last_error_message IS NULL OR length(last_error_message) <= 500",
            name="ck_deliveries_error_length",
        ),
        Index("ix_deliveries_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_deliveries_destination_status", "destination_id", "status"),
        Index("ix_deliveries_project_created", "project_id", "created_at", "id"),
    )


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    delivery_id: Mapped[str] = mapped_column(
        ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(8), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(String(24))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_category: Mapped[str | None] = mapped_column(String(120))
    sanitized_error: Mapped[str | None] = mapped_column(String(500))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        UniqueConstraint("delivery_id", "attempt_number", name="uq_delivery_attempts_number"),
        Index("ix_delivery_attempts_completed", "completed_at"),
        CheckConstraint("trigger IN ('auto','manual')", name="ck_delivery_attempts_trigger"),
        CheckConstraint(
            "result IS NULL OR result IN ('success','retryable_failure',"
            "'permanent_failure','interrupted')",
            name="ck_delivery_attempts_result",
        ),
        CheckConstraint(
            "sanitized_error IS NULL OR length(sanitized_error) <= 500",
            name="ck_delivery_attempts_error_length",
        ),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # actor_id remains the Phase 0 user reference; actor_ref supports later actors
    # (admins, project tokens, and system tasks) without changing that contract.
    actor_type: Mapped[str] = mapped_column(
        String(32), default="user", server_default="user", nullable=False
    )
    actor_ref: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String(36))
    details_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (
        Index("ix_audit_events_subject", "subject_type", "subject_id"),
        Index("ix_audit_events_created", "created_at"),
    )


class MaintenanceRun(Base):
    __tablename__ = "maintenance_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    kind: Mapped[str] = mapped_column(String(24), default="retention", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    cutoffs_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    counts_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (
        CheckConstraint("kind = 'retention'", name="ck_maintenance_runs_kind"),
        CheckConstraint(
            "status IN ('running','completed','failed')", name="ck_maintenance_runs_status"
        ),
        Index("ix_maintenance_runs_kind_started", "kind", "started_at"),
    )


class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    admin_user_id: Mapped[str] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_admin_sessions_admin_expires", "admin_user_id", "expires_at"),
        Index("ix_admin_sessions_expires", "expires_at"),
    )


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    succeeded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (
        Index("ix_login_attempts_email_time", "email", "attempted_at"),
        Index("ix_login_attempts_attempted_at", "attempted_at"),
    )


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    settings_json: Mapped[str] = mapped_column(
        Text,
        default='{"corrections":{"enabled":false},"deduplication":{"mode":"exact"}}',
        server_default='{"corrections":{"enabled":false},"deduplication":{"mode":"exact"}}',
        nullable=False,
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fallback_destination_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "destinations.id", ondelete="RESTRICT", name="fk_projects_fallback_destination_id"
        )
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    created_by_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    __table_args__ = (
        UniqueConstraint("project_id", "slug", name="uq_tags_project_slug"),
        Index("ix_tags_project_default", "project_id", "is_default"),
    )


class FeedbackEvent(Base):
    __tablename__ = "feedback_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    api_token_id: Mapped[str | None] = mapped_column(
        ForeignKey("api_tokens.id", ondelete="SET NULL")
    )
    submitted_via: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    correction: Mapped[str | None] = mapped_column(Text)
    previous_answer: Mapped[str | None] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text)
    source_reference: Mapped[str | None] = mapped_column(Text)
    agent_id: Mapped[str | None] = mapped_column(String(128))
    conversation_id: Mapped[str | None] = mapped_column(String(256))
    source_event_id: Mapped[str | None] = mapped_column(String(256))
    user_reference: Mapped[str | None] = mapped_column(String(256))
    metadata_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}", nullable=False
    )
    normalized_question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    question_hash_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    processing_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    __table_args__ = (
        CheckConstraint("submitted_via IN ('rest','mcp')", name="ck_feedback_events_submitted_via"),
        CheckConstraint(
            "event_type IN ('knowledge_gap','knowledge_correction')", name="ck_feedback_events_type"
        ),
        CheckConstraint(
            "(event_type = 'knowledge_correction') = (correction IS NOT NULL)",
            name="ck_feedback_events_correction",
        ),
        CheckConstraint(
            "processing_status IN ('pending','processing','completed','failed')",
            name="ck_feedback_events_status",
        ),
        CheckConstraint(
            "length(question) BETWEEN 1 AND 10000", name="ck_feedback_events_question_length"
        ),
        CheckConstraint(
            "correction IS NULL OR length(correction) BETWEEN 1 AND 20000",
            name="ck_feedback_events_correction_length",
        ),
        CheckConstraint(
            "previous_answer IS NULL OR length(previous_answer) <= 10000",
            name="ck_feedback_events_previous_answer_length",
        ),
        CheckConstraint(
            "context IS NULL OR length(context) <= 4000", name="ck_feedback_events_context_length"
        ),
        CheckConstraint(
            "source_reference IS NULL OR length(source_reference) <= 2000",
            name="ck_feedback_events_source_reference_length",
        ),
        CheckConstraint(
            "agent_id IS NULL OR length(agent_id) <= 128", name="ck_feedback_events_agent_id_length"
        ),
        CheckConstraint(
            "conversation_id IS NULL OR length(conversation_id) <= 256",
            name="ck_feedback_events_conversation_id_length",
        ),
        CheckConstraint(
            "source_event_id IS NULL OR length(source_event_id) <= 256",
            name="ck_feedback_events_source_event_id_length",
        ),
        CheckConstraint(
            "user_reference IS NULL OR length(user_reference) <= 256",
            name="ck_feedback_events_user_reference_length",
        ),
        CheckConstraint(
            "length(CAST(metadata_json AS BLOB)) <= 8192", name="ck_feedback_events_metadata_length"
        ),
        Index("ix_feedback_events_project_created", "project_id", "created_at", "id"),
        Index("ix_feedback_events_created", "created_at", "id"),
        Index(
            "ix_feedback_events_question_hash",
            "project_id",
            "event_type",
            "normalized_question_hash",
        ),
        Index("ix_feedback_events_status_created", "processing_status", "created_at"),
    )


class FeedbackEventTag(Base):
    __tablename__ = "feedback_event_tags"
    event_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_events.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(
        ForeignKey("tags.id", ondelete="RESTRICT"), primary_key=True
    )
    from_default: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    from_submission: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    from_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "from_default = 1 OR from_submission = 1 OR from_admin = 1",
            name="ck_feedback_event_tags_source",
        ),
        Index("ix_feedback_event_tags_tag_event", "tag_id", "event_id"),
    )


class FeedbackGroup(Base):
    __tablename__ = "feedback_groups"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    representative_event_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_events.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="open", server_default="open", nullable=False
    )
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('knowledge_gap','knowledge_correction')", name="ck_feedback_groups_type"
        ),
        CheckConstraint(
            "status IN ('open','in_progress','resolved','dismissed')",
            name="ck_feedback_groups_status",
        ),
        CheckConstraint("occurrence_count >= 1", name="ck_feedback_groups_count"),
        Index(
            "ix_feedback_groups_project_status_last", "project_id", "status", "last_seen_at", "id"
        ),
        Index("ix_feedback_groups_last", "last_seen_at", "id"),
        Index("ix_feedback_groups_status_last", "status", "last_seen_at", "id"),
        Index("ix_feedback_groups_count", "occurrence_count", "id"),
        Index("ix_feedback_groups_first", "first_seen_at", "id"),
    )


class FeedbackGroupMember(Base):
    __tablename__ = "feedback_group_members"
    event_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_events.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_groups.id", ondelete="RESTRICT"), nullable=False
    )
    match_method: Mapped[str] = mapped_column(String(20), nullable=False)
    triggered_reopen: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "match_method IN ('initial','manual_merge','manual_split','exact','semantic')",
            name="ck_feedback_group_members_method",
        ),
        Index("ix_feedback_group_members_group_event", "group_id", "event_id"),
    )


class FeedbackGroupNote(Base):
    __tablename__ = "feedback_group_notes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    group_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_groups.id", ondelete="CASCADE"), nullable=False
    )
    author_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    __table_args__ = (
        CheckConstraint("length(body) BETWEEN 1 AND 4000", name="ck_feedback_group_notes_body"),
        Index("ix_feedback_group_notes_group_created", "group_id", "created_at", "id"),
    )


class FeedbackGroupHistory(Base):
    __tablename__ = "feedback_group_history"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    group_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_groups.id", ondelete="CASCADE"), nullable=False
    )
    origin_group_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(10), nullable=False)
    actor_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    details_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "action IN ('created','status_changed','representative_changed',"
            "'tags_changed','note_added','note_deleted','merged','split_out',"
            "'split_in','event_deleted','occurrence_added')",
            name="ck_feedback_group_history_action",
        ),
        CheckConstraint("actor_type IN ('system','admin')", name="ck_feedback_group_history_actor"),
        CheckConstraint(
            "length(CAST(details_json AS BLOB)) <= 4096", name="ck_feedback_group_history_details"
        ),
        Index("ix_feedback_group_history_group_created", "group_id", "created_at", "id"),
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_events.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_idempotency_records_project_key"),
        Index("ix_idempotency_records_expires", "expires_at"),
    )


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    scopes_json: Mapped[str] = mapped_column(Text, default='["feedback:gap"]', nullable=False)
    created_by_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_api_tokens_project_revoked", "project_id", "revoked_at"),)
