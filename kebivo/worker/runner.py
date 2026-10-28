"""SQLite leased worker; every database unit runs in a worker thread."""

import asyncio
import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, cast
from uuid import uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from kebivo.dedup.embeddings import normalize, store
from kebivo.dedup.semantic import (
    SYSTEM_PROMPT,
    Candidate,
    candidates,
    embedding_candidates,
    prompt,
    score_embeddings,
)
from kebivo.destinations.base import ERRORS, REGISTRY, DeliveryResult, failure
from kebivo.destinations.payload import payload_v1
from kebivo.destinations.validation import ConfigError, validate_config
from kebivo.feedback.groups import associate
from kebivo.llm.adapter import EmbedResult, LlmResult, complete, embed
from kebivo.models import (
    DedupEvaluation,
    Delivery,
    DeliveryAttempt,
    Destination,
    EmbeddingBackfill,
    FeedbackEvent,
    FeedbackEventTag,
    FeedbackGroup,
    FeedbackGroupHistory,
    FeedbackGroupMember,
    FeedbackGroupNote,
    LlmProviderConfig,
    ProcessingJob,
    Project,
    QuestionEmbedding,
    RoutingMatch,
    RoutingRule,
    RoutingRuleDestination,
    RoutingRuleTag,
    Tag,
    now_utc,
)
from kebivo.projects import EMBEDDING_MAX_SCAN_DEFAULT, EMBEDDING_SIMILARITY_DEFAULT
from kebivo.retention import RetentionCancelled
from kebivo.retention import due as retention_due
from kebivo.retention import run as retention_run
from kebivo.routing.coverage import delivered_destinations, has_unrouted_earlier_member
from kebivo.routing.engine import DestinationInput, RuleInput, evaluate
from kebivo.security.secretbox import SecretBox, SecretError
from kebivo.settings import Settings

LOG = logging.getLogger("kebivo.request")
PROCESS_RETRY = (30, 120, 600, 1800)
PROVIDER_FAILURE_THRESHOLD = 2
PROVIDER_COOLDOWN_SECONDS = 60
TRANSPORT_FAILURES = frozenset({"timeout", "connect", "tls", "http_5xx"})


@dataclass
class SemanticWork:
    event_id: str
    project_id: str
    group_id: str
    question: str
    settings: dict[str, Any]
    candidates: list[Candidate] = field(default_factory=list)
    provider: LlmProviderConfig | None = None
    secrets: dict[str, str] = field(default_factory=dict)
    result: LlmResult | None = None
    outcome: str = "provider_unavailable"
    attempts: int = 0
    latency_ms: int = 0
    method: str = "judge"
    vector: list[float] | None = None
    scores: list[tuple[str, str, float]] = field(default_factory=list)
    scanned_count: int = 0
    embed_result: EmbedResult | None = None


def _log(name: str, **fields: Any) -> None:
    LOG.info(name, extra={"fields": fields})


class Worker:
    def __init__(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
        llm_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.factory = factory
        self.settings = settings
        self.boot_id = str(uuid4())
        self.wake_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.semaphore = asyncio.Semaphore(settings.worker_concurrency)
        self.llm_semaphore = llm_semaphore or asyncio.Semaphore(settings.llm_max_concurrency)
        self.task: asyncio.Task[None] | None = None
        self.inflight: set[asyncio.Task[None]] = set()
        self.semantic_inflight: set[asyncio.Task[None]] = set()
        self.provider_failures: dict[str, tuple[int, float]] = {}
        self.retention_check_at = time.monotonic() + 300
        self.retention_task: asyncio.Task[None] | None = None
        self.backfill_task: asyncio.Task[None] | None = None
        self.retention_stop = threading.Event()

    async def start(self) -> None:
        counts = await asyncio.to_thread(self._recover)
        _log("worker_recovered", **counts)
        self.task = asyncio.create_task(self._loop(), name="delivery-worker")
        _log("worker_started", boot_id=self.boot_id)

    async def stop(self) -> None:
        self.stop_event.set()
        self.retention_stop.set()
        self.wake_event.set()
        if self.task is not None:
            try:
                await self.task
            except asyncio.CancelledError:
                if not self.task.cancelled():
                    raise
                _log("worker_loop_stopped", boot_id=self.boot_id)
            except Exception:
                _log("worker_loop_stopped", boot_id=self.boot_id)
        if self.inflight:
            _, pending = await asyncio.wait(
                self.inflight, timeout=self.settings.worker_shutdown_grace_seconds
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if self.retention_task is not None:
            # The thread stops at the next batch boundary once retention_stop is set.
            await asyncio.gather(self.retention_task, return_exceptions=True)
        if self.backfill_task is not None:
            await asyncio.gather(self.backfill_task, return_exceptions=True)
        _log("worker_stopped", boot_id=self.boot_id)

    def wake(self) -> None:
        self.wake_event.set()

    def _recover(self) -> dict[str, int]:
        with self.factory() as db, db.begin():
            jobs = db.scalars(
                select(ProcessingJob).where(
                    ProcessingJob.status == "processing", ProcessingJob.lease_owner != self.boot_id
                )
            ).all()
            deliveries = db.scalars(
                select(Delivery).where(
                    Delivery.status == "processing", Delivery.lease_owner != self.boot_id
                )
            ).all()
            for job in jobs:
                job.status = "pending"
                job.lease_owner = None
                job.lease_until = None
                job.next_attempt_at = now_utc()
                event = db.get(FeedbackEvent, job.event_id)
                if event is not None:
                    event.processing_status = "pending"
            for delivery in deliveries:
                attempts = db.scalars(
                    select(DeliveryAttempt).where(
                        DeliveryAttempt.delivery_id == delivery.id,
                        DeliveryAttempt.completed_at.is_(None),
                    )
                ).all()
                for attempt in attempts:
                    attempt.completed_at = now_utc()
                    attempt.result = "interrupted"
                    attempt.error_category = "interrupted"
                delivery.status = (
                    "retry_scheduled"
                    if delivery.attempt_count < delivery.max_attempts
                    else "failed"
                )
                delivery.next_attempt_at = (
                    now_utc() if delivery.status == "retry_scheduled" else None
                )
                delivery.lease_owner = None
                delivery.lease_until = None
            return {"processing_jobs": len(jobs), "deliveries": len(deliveries)}

    def _claim(
        self, kind: Literal["job", "delivery"], limit: int, semantic: bool | None = None
    ) -> list[str]:
        now = now_utc().replace(tzinfo=None)
        engine = self.factory.kw["bind"]
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                if kind == "job":
                    statement = text("""
                        UPDATE processing_jobs SET status='processing', lease_owner=:owner,
                        lease_until=:lease, updated_at=:now
                        WHERE id IN (SELECT j.id FROM processing_jobs j
                            JOIN feedback_events e ON e.id=j.event_id
                            JOIN projects p ON p.id=e.project_id
                            WHERE ((j.status='pending' AND
                                    (j.next_attempt_at IS NULL OR j.next_attempt_at <= :now))
                               OR (j.status='processing' AND j.lease_until <= :now))
                              AND (:semantic IS NULL OR
                                   (COALESCE(json_extract(p.settings_json,
                                    '$.deduplication.mode'), 'exact')='semantic')=:semantic)
                            ORDER BY j.next_attempt_at, j.created_at, j.id LIMIT :limit)
                        RETURNING id
                    """)
                    lease = now + timedelta(seconds=60)
                else:
                    statement = text("""
                        UPDATE deliveries SET status='processing', lease_owner=:owner,
                        lease_until=datetime(:now, printf('+%d seconds',
                            CASE WHEN json_valid((SELECT config_json FROM destinations
                                WHERE destinations.id=deliveries.destination_id))
                              THEN COALESCE(json_extract((SELECT config_json FROM destinations
                                WHERE destinations.id=deliveries.destination_id),
                                '$.timeout_seconds'),10)
                              ELSE 10 END
                            + 30)), updated_at=:now
                        WHERE id IN (SELECT d.id FROM deliveries d JOIN destinations dest
                            ON dest.id=d.destination_id WHERE dest.enabled=1 AND
                            ((d.status IN ('pending','retry_scheduled') AND
                              (d.next_attempt_at IS NULL OR d.next_attempt_at <= :now))
                             OR (d.status='processing' AND d.lease_until <= :now))
                            ORDER BY d.next_attempt_at, d.created_at, d.id LIMIT :limit)
                        RETURNING id
                    """)
                    lease = now
                ids = list(
                    connection.execute(
                        statement,
                        {
                            "owner": self.boot_id,
                            "now": now.isoformat(sep=" "),
                            "lease": lease.isoformat(sep=" "),
                            "limit": limit,
                            "semantic": None if semantic is None else int(semantic),
                        },
                    ).scalars()
                )
                if kind == "job" and ids:
                    connection.execute(
                        text("""UPDATE feedback_events SET processing_status='processing'
                            WHERE id IN (SELECT event_id FROM processing_jobs
                                         WHERE lease_owner=:owner AND status='processing')"""),
                        {"owner": self.boot_id},
                    )
                connection.commit()
                return ids
            except BaseException:
                connection.rollback()
                raise

    async def _loop(self) -> None:
        backoff = 1
        while not self.stop_event.is_set():
            try:
                if self.backfill_task is None or self.backfill_task.done():
                    self.backfill_task = asyncio.create_task(
                        self._backfill(), name="embedding-backfill"
                    )
                if time.monotonic() >= self.retention_check_at and (
                    self.retention_task is None or self.retention_task.done()
                ):
                    self.retention_check_at = time.monotonic() + 600
                    self.retention_task = asyncio.create_task(self._retention(), name="retention")
                # Even a one-slot worker needs a separate lane for a model call.
                total_slots = max(2, self.settings.worker_concurrency)
                capacity = total_slots - len(self.inflight)
                if capacity > 0:
                    deliveries = await asyncio.to_thread(self._claim, "delivery", capacity)
                    for identifier in deliveries:
                        self._spawn(self._deliver(identifier))
                    capacity -= len(deliveries)
                    if capacity > 0:
                        jobs = await asyncio.to_thread(self._claim, "job", capacity, False)
                        for identifier in jobs:
                            self._spawn(self._process(identifier))
                        capacity -= len(jobs)
                    semantic_limit = min(
                        self.settings.llm_max_concurrency - len(self.semantic_inflight),
                        total_slots - 1 - len(self.semantic_inflight),
                        capacity,
                    )
                    if semantic_limit > 0:
                        jobs = await asyncio.to_thread(self._claim, "job", semantic_limit, True)
                        for identifier in jobs:
                            self._spawn(self._process(identifier), semantic=True)
                try:
                    await asyncio.wait_for(
                        self.wake_event.wait(), self.settings.worker_poll_interval_seconds
                    )
                    self.wake_event.clear()
                except TimeoutError:
                    pass
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log("worker_loop_error", category=type(exc).__name__)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 30)

    def _backfill_batch(
        self,
    ) -> (
        tuple[EmbeddingBackfill, LlmProviderConfig, dict[str, str], list[tuple[str, str, str]]]
        | None
    ):
        with self.factory() as db:
            jobs = db.scalars(
                select(EmbeddingBackfill).order_by(EmbeddingBackfill.project_id)
            ).all()
            selected = next(
                (
                    (candidate, configured, source)
                    for candidate in jobs
                    if (configured := db.get(Project, candidate.project_id)) is not None
                    and (source := db.get(LlmProviderConfig, candidate.provider_id)) is not None
                    and source.enabled
                    and self.provider_failures.get(source.id, (0, 0.0))[1] <= time.monotonic()
                ),
                None,
            )
            if selected is None:
                return None
            job, project, provider = selected
            dedup = json.loads(project.settings_json).get("deduplication", {})
            semantic = dedup.get("semantic", {})
            if (
                dedup.get("mode") != "semantic"
                or semantic.get("method") != "embeddings"
                or semantic.get("embedding_model") != job.model
            ):
                return None
            secrets = (
                SecretBox.from_settings(self.settings).decrypt(
                    provider.id, provider.secrets_ciphertext, purpose="llm-provider-secrets"
                )
                if provider.secrets_ciphertext
                else {}
            )
            db.execute(
                delete(QuestionEmbedding).where(
                    QuestionEmbedding.project_id == job.project_id,
                    (QuestionEmbedding.model != job.model)
                    | ((QuestionEmbedding.dims != job.dims) if job.dims is not None else False)
                    | QuestionEmbedding.provider_id.is_(None)
                    | (QuestionEmbedding.provider_id != job.provider_id),
                )
            )
            db.commit()
            if job.remaining <= 0:
                db.delete(job)
                db.commit()
                return None
            statement = (
                select(FeedbackGroup, FeedbackEvent)
                .join(FeedbackEvent, FeedbackEvent.id == FeedbackGroup.representative_event_id)
                .where(FeedbackGroup.project_id == job.project_id)
            )
            if job.cursor_seen_at is not None:
                statement = statement.where(
                    (FeedbackGroup.last_seen_at < job.cursor_seen_at)
                    | (
                        (FeedbackGroup.last_seen_at == job.cursor_seen_at)
                        & (FeedbackGroup.id < job.cursor_group_id)
                    )
                )
            rows = db.execute(
                statement.order_by(
                    FeedbackGroup.last_seen_at.desc(), FeedbackGroup.id.desc()
                ).limit(min(16, job.remaining))
            ).all()
            if not rows:
                db.delete(job)
                db.commit()
                return None
            pending = [
                (group.id, event.id, event.question[:2000])
                for group, event in rows
                if job.dims is None or db.get(QuestionEmbedding, event.id) is None
            ]
            db.refresh(job)
            db.refresh(provider)
            db.expunge(job)
            db.expunge(provider)
            return job, provider, secrets, pending

    async def _backfill(self) -> None:
        try:
            batch = await asyncio.to_thread(self._backfill_batch)
            if batch is None:
                return
            job, provider, secrets, pending = batch
            if self.provider_failures.get(provider.id, (0, 0.0))[1] > time.monotonic():
                return
            vectors: list[list[float]] = []
            if pending:
                async with self.llm_semaphore:
                    result = await asyncio.wait_for(
                        embed(provider, secrets, job.model, [item[2] for item in pending]),
                        timeout=45,
                    )
                if not result.vectors:
                    if result.error_category in TRANSPORT_FAILURES | {"http_429"}:
                        failures = self.provider_failures.get(provider.id, (0, 0.0))[0] + 1
                        self.provider_failures[provider.id] = (
                            failures,
                            time.monotonic() + PROVIDER_COOLDOWN_SECONDS
                            if failures >= PROVIDER_FAILURE_THRESHOLD
                            else 0.0,
                        )
                    return
                vectors = [normalize(vector) for vector in result.vectors]
                self.provider_failures.pop(provider.id, None)
            await asyncio.to_thread(
                self._finish_backfill_batch,
                job.project_id,
                job.provider_id,
                job.model,
                job.dims,
                pending,
                vectors,
            )
        except (SecretError, TimeoutError, ValueError) as exc:
            _log("embedding_backfill_error", category=type(exc).__name__)

    def _finish_backfill_batch(
        self,
        project_id: str,
        provider_id: str,
        model: str,
        dims: int | None,
        pending: list[tuple[str, str, str]],
        vectors: list[list[float]],
    ) -> None:
        with self.factory() as db, db.begin():
            job = db.get(EmbeddingBackfill, project_id)
            if job is None or (job.provider_id, job.model, job.dims) != (provider_id, model, dims):
                return
            if vectors:
                current_dims = len(vectors[0])
                if job.dims is not None and job.dims != current_dims:
                    project = db.get(Project, project_id)
                    assert project is not None
                    semantic = json.loads(project.settings_json)["deduplication"]["semantic"]
                    job.remaining = semantic.get("max_scan", EMBEDDING_MAX_SCAN_DEFAULT)
                    job.cursor_seen_at = None
                    job.cursor_group_id = None
                    job.dims = current_dims
                    db.execute(
                        delete(QuestionEmbedding).where(
                            QuestionEmbedding.project_id == project_id,
                            QuestionEmbedding.dims != current_dims,
                        )
                    )
                    return
                job.dims = current_dims
                db.execute(
                    delete(QuestionEmbedding).where(
                        QuestionEmbedding.project_id == project_id,
                        QuestionEmbedding.dims != current_dims,
                    )
                )
            statement = select(FeedbackGroup).where(FeedbackGroup.project_id == project_id)
            if job.cursor_seen_at is not None:
                statement = statement.where(
                    (FeedbackGroup.last_seen_at < job.cursor_seen_at)
                    | (
                        (FeedbackGroup.last_seen_at == job.cursor_seen_at)
                        & (FeedbackGroup.id < job.cursor_group_id)
                    )
                )
            groups = db.scalars(
                statement.order_by(
                    FeedbackGroup.last_seen_at.desc(), FeedbackGroup.id.desc()
                ).limit(min(16, job.remaining))
            ).all()
            if not groups:
                db.delete(job)
                return
            for (_, event_id, _), vector in zip(pending, vectors, strict=True):
                event = db.get(FeedbackEvent, event_id)
                if event is not None and db.get(QuestionEmbedding, event_id) is None:
                    store(
                        db,
                        event_id=event_id,
                        project_id=project_id,
                        event_type=event.event_type,
                        provider_id=job.provider_id,
                        model=model,
                        values=vector,
                    )
            job.cursor_seen_at = groups[-1].last_seen_at
            job.cursor_group_id = groups[-1].id
            job.remaining -= len(groups)
            if job.remaining <= 0:
                db.delete(job)

    async def _retention(self) -> None:
        """Single-flight retention check and run, off the claim loop."""
        try:
            should_run = await asyncio.to_thread(self._retention_due)
            if should_run and not self.retention_stop.is_set():
                await asyncio.to_thread(
                    retention_run, self.factory, self.settings, None, self.retention_stop
                )
        except RetentionCancelled:
            _log("retention_cancelled", boot_id=self.boot_id)
        except Exception as exc:
            _log("retention_error", category=type(exc).__name__)

    def _retention_due(self) -> bool:
        with self.factory() as db:
            return retention_due(db, now_utc())

    def _spawn(self, coroutine: Any, *, semantic: bool = False) -> None:
        task = asyncio.create_task(coroutine)
        self.inflight.add(task)
        task.add_done_callback(self.inflight.discard)
        if semantic:
            self.semantic_inflight.add(task)
            task.add_done_callback(self.semantic_inflight.discard)
        task.add_done_callback(lambda _: self.wake())

    async def _process(self, identifier: str) -> None:
        started = time.monotonic()
        try:
            work = await asyncio.to_thread(self._prepare_semantic, identifier)
            if (
                work is not None
                and work.provider is not None
                and (work.method == "embeddings" or work.candidates)
            ):
                try:
                    if work.method == "embeddings":
                        await self._ask_embedding(work)
                    else:
                        await self._ask_semantic(work)
                except Exception:
                    work.outcome = "provider_error"
                    work.result = None
            await asyncio.to_thread(self._process_sync, identifier, work)
            _log(
                "processing_job",
                job_id=identifier,
                result="completed",
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        except Exception:
            try:
                await asyncio.to_thread(self._process_failed, identifier)
            except Exception as exc:
                _log("processing_job_error", job_id=identifier, category=type(exc).__name__)
                return
            _log(
                "processing_job",
                job_id=identifier,
                result="failed",
                category="routing_error",
                duration_ms=round((time.monotonic() - started) * 1000),
            )

    def _untouched(self, db: Session, event_id: str, group: FeedbackGroup) -> bool:
        member = db.get(FeedbackGroupMember, event_id)
        if member is None or member.group_id != group.id or member.match_method != "initial":
            return False
        if (
            group.status != "open"
            or group.occurrence_count != 1
            or group.representative_event_id != event_id
        ):
            return False
        if (
            db.scalar(
                select(func.count())
                .select_from(FeedbackGroupMember)
                .where(FeedbackGroupMember.group_id == group.id)
            )
            != 1
        ):
            return False
        if (
            db.scalar(
                select(FeedbackGroupNote.id).where(FeedbackGroupNote.group_id == group.id).limit(1)
            )
            is not None
        ):
            return False
        history = db.scalars(
            select(FeedbackGroupHistory.action).where(FeedbackGroupHistory.group_id == group.id)
        ).all()
        return history == ["created"]

    def _prepare_semantic(self, identifier: str) -> SemanticWork | None:
        with self.factory() as db:
            job = db.get(ProcessingJob, identifier)
            if job is None or job.status != "processing" or job.lease_owner != self.boot_id:
                return None
            event = db.get(FeedbackEvent, job.event_id)
            if event is None:
                return None
            project = db.get(Project, event.project_id)
            if project is None:
                return None
            dedup = json.loads(project.settings_json).get("deduplication", {})
            if dedup.get("mode") != "semantic":
                return None
            member = db.get(FeedbackGroupMember, event.id)
            group = db.get(FeedbackGroup, member.group_id) if member else None
            if group is None or not self._untouched(db, event.id, group):
                return None
            settings = dedup.get("semantic", {})
            work = SemanticWork(event.id, project.id, group.id, event.question, settings)
            work.method = settings.get("method", "judge")
            try:
                if work.method == "judge":
                    work.candidates = candidates(
                        db,
                        project.id,
                        event.event_type,
                        event.question,
                        group.id,
                        settings.get("candidate_count", 5),
                    )
                    if not work.candidates:
                        work.outcome = "no_candidates"
                        return work
                provider = db.get(LlmProviderConfig, settings.get("provider_id"))
                if provider is None or not provider.enabled:
                    return work
                work.provider = provider
                if provider.secrets_ciphertext:
                    work.secrets = SecretBox.from_settings(self.settings).decrypt(
                        provider.id, provider.secrets_ciphertext, purpose="llm-provider-secrets"
                    )
            except SecretError:
                work.provider = None
            except Exception:
                work.outcome = "provider_error"
            return work

    def _score_embedding(self, work: SemanticWork) -> None:
        assert work.vector is not None
        with self.factory() as db:
            event = db.get(FeedbackEvent, work.event_id)
            assert event is not None
            rows = embedding_candidates(
                db,
                work.project_id,
                event.event_type,
                work.settings["embedding_model"],
                len(work.vector),
                work.group_id,
                work.settings.get("max_scan", EMBEDDING_MAX_SCAN_DEFAULT),
            )
        work.scanned_count = len(rows)
        work.scores = score_embeddings(work.vector, rows)
        work.outcome = (
            "no_candidates"
            if not rows
            else "matched"
            if work.scores[0][2]
            >= work.settings.get("similarity_threshold", EMBEDDING_SIMILARITY_DEFAULT)
            else "no_match"
        )

    async def _ask_embedding(self, work: SemanticWork) -> None:
        assert work.provider is not None
        provider = work.provider
        if self.provider_failures.get(provider.id, (0, 0.0))[1] > time.monotonic():
            return
        deadline = time.monotonic() + 45
        acquired = False
        try:
            await asyncio.wait_for(self.llm_semaphore.acquire(), timeout=45)
            acquired = True
            for attempt in range(work.settings.get("max_attempts", 2)):
                if self.provider_failures.get(provider.id, (0, 0.0))[1] > time.monotonic():
                    work.outcome = "provider_unavailable"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    work.outcome = "timeout"
                    break
                work.attempts += 1
                try:
                    result = await asyncio.wait_for(
                        embed(
                            provider,
                            work.secrets,
                            work.settings["embedding_model"],
                            [work.question[:2000]],
                        ),
                        timeout=remaining,
                    )
                except TimeoutError:
                    result = EmbedResult(error_category="timeout")
                work.embed_result = result
                work.latency_ms += result.latency_ms
                if result.vectors:
                    try:
                        work.vector = normalize(result.vectors[0])
                    except ValueError:
                        work.outcome = "provider_error"
                        break
                    self.provider_failures.pop(provider.id, None)
                    await asyncio.to_thread(self._score_embedding, work)
                    break
                category = result.error_category
                work.outcome = "timeout" if category == "timeout" else "provider_error"
                if category in TRANSPORT_FAILURES | {"http_429"}:
                    failures = self.provider_failures.get(provider.id, (0, 0.0))[0] + 1
                    self.provider_failures[provider.id] = (
                        failures,
                        time.monotonic() + PROVIDER_COOLDOWN_SECONDS
                        if failures >= PROVIDER_FAILURE_THRESHOLD
                        else 0.0,
                    )
                    if attempt + 1 < work.settings.get("max_attempts", 2):
                        await asyncio.sleep(
                            min(1 if attempt == 0 else 3, max(0, deadline - time.monotonic()))
                        )
                else:
                    self.provider_failures.pop(provider.id, None)
                    break
        except TimeoutError:
            work.outcome = "timeout"
        finally:
            if acquired:
                self.llm_semaphore.release()

    async def _ask_semantic(self, work: SemanticWork) -> None:
        assert work.provider is not None
        deadline = time.monotonic() + 45
        provider = work.provider
        failures, open_until = self.provider_failures.get(provider.id, (0, 0.0))
        if open_until > time.monotonic():
            work.outcome = "provider_unavailable"
            return
        original_model, original_timeout = provider.model, provider.timeout_seconds
        provider.model = work.settings.get("model") or original_model
        provider.timeout_seconds = min(work.settings.get("timeout_seconds", 15), original_timeout)
        acquired = False
        try:
            try:
                await asyncio.wait_for(self.llm_semaphore.acquire(), timeout=45)
                acquired = True
            except TimeoutError:
                work.outcome = "timeout"
                return
            for attempt in range(work.settings.get("max_attempts", 2)):
                failures, open_until = self.provider_failures.get(provider.id, (0, 0.0))
                if open_until > time.monotonic():
                    work.outcome = "provider_unavailable"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    work.outcome = "timeout"
                    break
                work.attempts += 1
                try:
                    result = await asyncio.wait_for(
                        complete(
                            provider,
                            work.secrets,
                            SYSTEM_PROMPT,
                            prompt(work.question, work.candidates),
                            {f"c{i}" for i in range(1, len(work.candidates) + 1)},
                        ),
                        timeout=remaining,
                    )
                except TimeoutError:
                    result = LlmResult(error_category="timeout")
                work.result = result
                work.latency_ms += result.latency_ms
                if result.error_category in TRANSPORT_FAILURES:
                    failures += 1
                    if failures >= PROVIDER_FAILURE_THRESHOLD:
                        self.provider_failures[provider.id] = (
                            failures,
                            time.monotonic() + PROVIDER_COOLDOWN_SECONDS,
                        )
                    else:
                        self.provider_failures[provider.id] = (failures, 0.0)
                else:
                    self.provider_failures.pop(provider.id, None)
                if result.error_category is None:
                    work.outcome = "no_match"
                    break
                work.outcome = (
                    "timeout"
                    if result.error_category == "timeout"
                    else "invalid_candidate"
                    if result.error_category == "invalid_candidate"
                    else "invalid_response"
                    if result.error_category in {"invalid_json", "invalid_response"}
                    else "provider_error"
                )
                if result.error_category not in TRANSPORT_FAILURES | {"http_429"}:
                    break
                if attempt + 1 < work.settings.get("max_attempts", 2):
                    await asyncio.sleep(
                        min(1 if attempt == 0 else 3, max(0, deadline - time.monotonic()))
                    )
        finally:
            if acquired:
                self.llm_semaphore.release()
            provider.model, provider.timeout_seconds = original_model, original_timeout

    def _commit_semantic(self, db: Session, event: FeedbackEvent, work: SemanticWork) -> None:
        if work.method == "embeddings":
            self._commit_embedding(db, event, work)
            return
        group = db.get(FeedbackGroup, work.group_id)
        outcome = work.outcome
        answer = work.result.answer if work.result else None
        matched_group: FeedbackGroup | None = None
        confidence: float | None = None
        reason: str | None = None
        if group is None or not self._untouched(db, event.id, group):
            outcome = "skipped_group_changed"
        elif answer is not None:
            confidence = answer["confidence"]
            reason = answer["reason"]
            if answer["same_issue"]:
                label = answer["candidate"]
                selected = work.candidates[int(label[1:]) - 1]
                matched_group = db.get(FeedbackGroup, selected.group_id)
                if (
                    matched_group is None
                    or matched_group.project_id != event.project_id
                    or matched_group.event_type != event.event_type
                    or matched_group.id == group.id
                ):
                    outcome = "invalid_candidate"
                    matched_group = None
                elif confidence < work.settings.get("min_confidence", 0.85):
                    outcome = "low_confidence"
                    matched_group = None
                else:
                    member = db.get(FeedbackGroupMember, event.id)
                    assert member is not None
                    db.delete(member)
                    db.flush()
                    db.delete(group)
                    db.flush()
                    associate(db, event, matched_group, "semantic", confidence)
                    outcome = "matched"
            else:
                outcome = "no_match"
        evaluation = db.get(DedupEvaluation, event.id)
        if evaluation is None:
            evaluation = DedupEvaluation(
                event_id=event.id,
                project_id=event.project_id,
                outcome=outcome,
                candidate_count=len(work.candidates),
                attempts=work.attempts,
                latency_ms=work.latency_ms,
            )
            db.add(evaluation)
        evaluation.provider_id = work.provider.id if work.provider else None
        evaluation.model = (
            (work.settings.get("model") or work.provider.model) if work.provider else None
        )
        evaluation.outcome = outcome
        evaluation.candidate_count = len(work.candidates)
        evaluation.matched_group_id = (
            matched_group.id if outcome == "matched" and matched_group else None
        )
        evaluation.confidence = confidence
        evaluation.reason = reason
        evaluation.error_category = work.result.error_category if work.result else None
        evaluation.http_status = work.result.http_status if work.result else None
        evaluation.attempts = work.attempts
        evaluation.latency_ms = work.latency_ms
        _log(
            "dedup_evaluation",
            event_id=event.id,
            project_id=event.project_id,
            provider_id=evaluation.provider_id,
            matched_group_id=evaluation.matched_group_id,
            outcome=outcome,
            category=evaluation.error_category,
            attempts=work.attempts,
            latency_ms=work.latency_ms,
        )

    def _commit_embedding(self, db: Session, event: FeedbackEvent, work: SemanticWork) -> None:
        group = db.get(FeedbackGroup, work.group_id)
        outcome = work.outcome
        matched_group = None
        if group is None or not self._untouched(db, event.id, group):
            outcome = "skipped_group_changed"
        elif outcome == "matched" and work.scores:
            matched_group = db.get(FeedbackGroup, work.scores[0][0])
            if (
                matched_group is None
                or matched_group.project_id != event.project_id
                or matched_group.event_type != event.event_type
            ):
                outcome = "skipped_group_changed"
                matched_group = None
            else:
                member = db.get(FeedbackGroupMember, event.id)
                assert member is not None
                db.delete(member)
                db.flush()
                db.delete(group)
                db.flush()
                associate(db, event, matched_group, "semantic", work.scores[0][2])
        if work.vector is not None:
            dims = len(work.vector)
            db.execute(
                delete(QuestionEmbedding).where(
                    QuestionEmbedding.project_id == event.project_id,
                    QuestionEmbedding.dims != dims,
                )
            )
            backfill = db.get(EmbeddingBackfill, event.project_id)
            if backfill is not None and backfill.dims != dims:
                if backfill.dims is not None:
                    backfill.remaining = work.settings.get("max_scan", EMBEDDING_MAX_SCAN_DEFAULT)
                    backfill.cursor_seen_at = None
                    backfill.cursor_group_id = None
                backfill.dims = dims
            store(
                db,
                event_id=event.id,
                project_id=event.project_id,
                event_type=event.event_type,
                provider_id=work.provider.id if work.provider else None,
                model=work.settings["embedding_model"],
                values=work.vector,
            )
        evaluation = DedupEvaluation(
            event_id=event.id,
            project_id=event.project_id,
            provider_id=work.provider.id if work.provider else None,
            model=work.settings["embedding_model"],
            method="embeddings",
            outcome=outcome,
            candidate_count=work.scanned_count,
            matched_group_id=matched_group.id if outcome == "matched" and matched_group else None,
            confidence=work.scores[0][2] if work.scores else None,
            reason=None,
            error_category=work.embed_result.error_category if work.embed_result else None,
            http_status=work.embed_result.http_status if work.embed_result else None,
            attempts=work.attempts,
            latency_ms=work.latency_ms,
        )
        db.add(evaluation)

    def _process_sync(self, identifier: str, work: SemanticWork | None = None) -> None:
        with self.factory() as db, db.begin():
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = db.get(ProcessingJob, identifier)
            if (
                job is None
                or job.status != "processing"
                or job.lease_owner != self.boot_id
                or (
                    job.lease_until is not None
                    and job.lease_until.replace(tzinfo=None) <= now_utc().replace(tzinfo=None)
                )
            ):
                return
            event = db.get(FeedbackEvent, job.event_id)
            assert event is not None
            project = db.get(Project, event.project_id)
            assert project is not None
            if work is not None:
                self._commit_semantic(db, event, work)
            tags = set(
                db.scalars(
                    select(Tag.slug)
                    .join(FeedbackEventTag, FeedbackEventTag.tag_id == Tag.id)
                    .where(FeedbackEventTag.event_id == event.id)
                )
            )
            rules = []
            for rule in db.scalars(select(RoutingRule).where(RoutingRule.project_id == project.id)):
                rule_tags = tuple(
                    db.scalars(
                        select(Tag.slug)
                        .join(RoutingRuleTag, RoutingRuleTag.tag_id == Tag.id)
                        .where(RoutingRuleTag.rule_id == rule.id)
                        .order_by(Tag.slug)
                    )
                )
                rule_destinations = tuple(
                    db.scalars(
                        select(RoutingRuleDestination.destination_id)
                        .where(RoutingRuleDestination.rule_id == rule.id)
                        .order_by(RoutingRuleDestination.destination_id)
                    )
                )
                rules.append(
                    RuleInput(
                        rule.id,
                        rule.name,
                        rule.priority,
                        rule.created_at,
                        rule.enabled,
                        tuple(json.loads(rule.event_types_json)),
                        cast(Literal["any", "all"], rule.match_mode),
                        rule_tags,
                        rule_destinations,
                        cast(Literal["continue", "stop"], rule.match_behavior),
                    )
                )
            member = db.get(FeedbackGroupMember, event.id)
            assert member is not None
            destinations = [
                DestinationInput(
                    row.id,
                    row.enabled,
                    cast(Literal["new_groups_only", "every_occurrence"], row.delivery_mode),
                )
                for row in db.scalars(select(Destination))
            ]
            previously_delivered = delivered_destinations(db, member.group_id, event.id)
            if has_unrouted_earlier_member(db, member.group_id, member):
                previously_delivered.update(destination.id for destination in destinations)
            decision = evaluate(
                event.event_type,
                tags,
                rules,
                destinations,
                project.fallback_destination_id,
                initial=member.match_method == "initial",
                triggered_reopen=member.triggered_reopen,
                previously_delivered=frozenset(previously_delivered),
            )
            db.execute(delete(RoutingMatch).where(RoutingMatch.event_id == event.id))
            rule_names = {rule.id: rule.name for rule in rules}
            rule_order = {
                rule.id: index
                for index, rule in enumerate(
                    sorted(rules, key=lambda item: (item.priority, item.created_at, item.id))
                )
            }
            for match in decision.destinations:
                db.add(
                    RoutingMatch(
                        event_id=event.id,
                        rule_id=match.rule_id,
                        rule_name=rule_names.get(match.rule_id) if match.rule_id else None,
                        rule_order=rule_order.get(match.rule_id) if match.rule_id else None,
                        via=match.via,
                        destination_id=match.destination_id,
                        outcome=match.outcome,
                    )
                )
                if match.outcome == "delivery_created":
                    db.execute(
                        text("""INSERT INTO deliveries
                        (id,event_id,project_id,destination_id,status,attempt_count,max_attempts,
                         manual_retry_count,created_at,updated_at)
                        VALUES (:id,:event,:project,:destination,'pending',0,6,0,:now,:now)
                        ON CONFLICT(event_id,destination_id) DO NOTHING"""),
                        {
                            "id": str(uuid4()),
                            "event": event.id,
                            "project": project.id,
                            "destination": match.destination_id,
                            "now": now_utc().replace(tzinfo=None).isoformat(sep=" "),
                        },
                    )
            job.status = "completed"
            job.lease_owner = None
            job.lease_until = None
            event.processing_status = "completed"
            job.error_code = None

    def _process_failed(self, identifier: str) -> None:
        with self.factory() as db, db.begin():
            job = db.get(ProcessingJob, identifier)
            if job is None or job.lease_owner != self.boot_id:
                return
            event = db.get(FeedbackEvent, job.event_id)
            job.attempt_count += 1
            job.error_code = "routing_error"
            job.lease_owner = None
            job.lease_until = None
            if job.attempt_count > len(PROCESS_RETRY):
                job.status = "failed"
                if event is not None:
                    event.processing_status = "failed"
            else:
                job.status = "pending"
                job.next_attempt_at = now_utc() + timedelta(
                    seconds=PROCESS_RETRY[job.attempt_count - 1]
                )

    def _begin_attempt(
        self, identifier: str, trigger: Literal["auto", "manual"] = "auto"
    ) -> tuple[str, dict[str, Any], dict[str, Any], int] | None:
        with self.factory() as db, db.begin():
            delivery = db.get(Delivery, identifier)
            if (
                delivery is None
                or delivery.status != "processing"
                or delivery.lease_owner != self.boot_id
            ):
                return None
            destination = db.get(Destination, delivery.destination_id)
            event = db.get(FeedbackEvent, delivery.event_id)
            assert destination is not None and event is not None
            for old in db.scalars(
                select(DeliveryAttempt).where(
                    DeliveryAttempt.delivery_id == identifier,
                    DeliveryAttempt.completed_at.is_(None),
                )
            ):
                old.completed_at = now_utc()
                old.result = "interrupted"
                old.error_category = "interrupted"
            if delivery.attempt_count >= delivery.max_attempts and trigger == "auto":
                delivery.status = "failed"
                delivery.lease_owner = None
                delivery.lease_until = None
                return None
            delivery.attempt_count += 1
            delivery.last_attempt_at = now_utc()
            attempt = DeliveryAttempt(
                delivery_id=identifier,
                attempt_number=delivery.attempt_count,
                trigger=trigger,
                started_at=now_utc(),
            )
            db.add(attempt)
            db.flush()
            project = db.get(Project, event.project_id)
            assert project is not None
            member = db.get(FeedbackGroupMember, event.id)
            assert member is not None
            group = db.get(FeedbackGroup, member.group_id)
            assert group is not None
            tag_names = db.scalars(
                select(Tag.name)
                .join(FeedbackEventTag, FeedbackEventTag.tag_id == Tag.id)
                .where(FeedbackEventTag.event_id == event.id)
                .order_by(Tag.name)
            ).all()
            message = payload_v1(
                delivery_id=identifier,
                event_id=event.id,
                event_type=event.event_type,
                project_id=project.id,
                project_name=project.name,
                question=event.question,
                correction=event.correction,
                context=event.context,
                tags=list(tag_names),
                created_at=event.created_at,
                group_id=group.id,
                occurrence_count=group.occurrence_count,
            )
            return (
                attempt.id,
                {
                    "id": destination.id,
                    "type": destination.type,
                    "config": destination.config_json,
                    "ciphertext": destination.secrets_ciphertext,
                    "allow_private_network": destination.allow_private_network,
                },
                message,
                delivery.attempt_count,
            )

    async def _deliver(self, identifier: str, trigger: Literal["auto", "manual"] = "auto") -> None:
        try:
            attempt_data = await asyncio.to_thread(self._begin_attempt, identifier, trigger)
            if attempt_data is None:
                return
            attempt_id, destination, message, number = attempt_data
            started = time.monotonic()
            async with self.semaphore:
                result = await send_destination(destination, message, self.settings)
            duration = round((time.monotonic() - started) * 1000)
            await asyncio.to_thread(
                self._finish_attempt, identifier, attempt_id, result, duration, trigger
            )
            _log(
                "delivery_attempt",
                delivery_id=identifier,
                attempt_number=number,
                category=result.category,
                outcome=result.outcome,
                duration_ms=duration,
            )
        except Exception as exc:
            _log("delivery_attempt_error", delivery_id=identifier, category=type(exc).__name__)

    def _finish_attempt(
        self,
        identifier: str,
        attempt_id: str,
        result: DeliveryResult,
        duration: int,
        trigger: Literal["auto", "manual"] = "auto",
    ) -> None:
        with self.factory() as db, db.begin():
            delivery = db.get(Delivery, identifier)
            attempt = db.get(DeliveryAttempt, attempt_id)
            if delivery is None or attempt is None or attempt.completed_at is not None:
                return
            if delivery.lease_owner != self.boot_id or delivery.status != "processing":
                return
            attempt.completed_at = now_utc()
            attempt.duration_ms = duration
            attempt.result = {
                "success": "success",
                "retryable": "retryable_failure",
                "permanent": "permanent_failure",
            }[result.outcome]
            attempt.error_category = result.category
            attempt.sanitized_error = result.sanitized_error
            attempt.http_status = result.http_status
            delivery.last_error_category = result.category
            delivery.last_error_message = result.sanitized_error
            delivery.last_http_status = result.http_status
            delivery.lease_owner = None
            delivery.lease_until = None
            if result.outcome == "success":
                delivery.status = "delivered"
                delivery.delivered_at = now_utc()
                delivery.next_attempt_at = None
            elif (
                result.outcome == "permanent"
                or trigger == "manual"
                or delivery.attempt_count >= delivery.max_attempts
            ):
                delivery.status = "failed"
                delivery.next_attempt_at = None
            else:
                schedule = self.settings.delivery_retry_schedule_seconds
                index = min(delivery.attempt_count - 1, len(schedule) - 1)
                minimum = max(1, round(schedule[index] * random.uniform(0.8, 1.2)))
                retry_after = min(
                    result.retry_after or 0, self.settings.delivery_retry_after_max_seconds
                )
                delivery.status = "retry_scheduled"
                delivery.next_attempt_at = now_utc() + timedelta(seconds=max(minimum, retry_after))


async def send_destination(
    destination: dict[str, Any], message: dict[str, Any], settings: Settings
) -> DeliveryResult:
    try:
        config = json.loads(destination["config"])
        if not isinstance(config, dict):
            raise ValueError
        config["allow_private_network"] = destination["allow_private_network"]
        config["user_agent"] = settings.webhook_user_agent
        if settings.allowed_origins:
            config["dashboard_origin"] = settings.allowed_origins[0]
        if settings.public_url:
            config["public_url"] = settings.public_url
        secrets = (
            SecretBox.from_settings(settings).decrypt(destination["id"], destination["ciphertext"])
            if destination["ciphertext"]
            else {}
        )
    except SecretError:
        return failure("permanent", "secret_unavailable")
    except (ValueError, TypeError):
        return failure("permanent", "config_invalid")
    kind = destination["type"]
    if kind != "inbox":
        try:
            full = {
                **config,
                "type": kind,
                "allow_private_network": destination["allow_private_network"],
                **secrets,
            }
            if kind == "webhook":
                full["custom_headers"] = {
                    name: secrets.get(f"custom_headers.{name}")
                    for name in config.get("custom_headers", [])
                }
                if "token" in secrets or "header_value" in secrets:
                    full["auth"] = {
                        **config["auth"],
                        **({"token": secrets["token"]} if "token" in secrets else {}),
                        **({"value": secrets["header_value"]} if "header_value" in secrets else {}),
                    }
            validate_config(full, {})
        except (ConfigError, KeyError, TypeError, ValueError):
            return failure("permanent", "config_invalid")
    timeout = config.get("timeout_seconds", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 30:
        return failure("permanent", "config_invalid")
    if kind in {"webhook", "slack"} and (
        "url" not in secrets or (config.get("signing") and "signing_secret" not in secrets)
    ):
        return failure("permanent", "config_invalid")
    if kind == "smtp" and config.get("username") and "password" not in secrets:
        return failure("permanent", "config_invalid")
    adapter = REGISTRY.get(kind)
    if adapter is None:
        return failure("permanent", "config_invalid")
    try:
        result = await asyncio.wait_for(
            adapter.send(message, config, secrets, timeout=timeout), timeout=timeout
        )
        if result.outcome == "success":
            return DeliveryResult(
                "success", category=result.category, http_status=result.http_status
            )
        category = result.category if result.category in ERRORS else "adapter_error"
        return failure(
            result.outcome, category, http_status=result.http_status, retry_after=result.retry_after
        )
    except TimeoutError:
        return failure("retryable", "timeout")
    except Exception:
        return failure("retryable", "adapter_error")
