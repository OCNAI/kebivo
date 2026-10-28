"""Read-only semantic discovery and audited preview for administrators."""

import asyncio
import json
import time
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from kebivo.admin_groups import _reject_bearer
from kebivo.auth import AdminAuth
from kebivo.dedup.embeddings import normalize
from kebivo.dedup.normalize import question_hash
from kebivo.dedup.semantic import (
    SYSTEM_PROMPT,
    candidates,
    embedding_candidates,
    prompt,
    score_embeddings,
)
from kebivo.errors import error_response
from kebivo.llm.adapter import complete, embed
from kebivo.models import (
    AuditEvent,
    FeedbackEvent,
    FeedbackGroup,
    FeedbackGroupMember,
    LlmProviderConfig,
    Project,
)
from kebivo.projects import EMBEDDING_MAX_SCAN_DEFAULT, EMBEDDING_SIMILARITY_DEFAULT
from kebivo.security.secretbox import SecretBox, SecretError


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: Literal["knowledge_gap", "knowledge_correction"]
    question: str = Field(min_length=1, max_length=10000)


def _id(value: str) -> str | None:
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _public_candidates(items: Any) -> list[dict[str, Any]]:
    return [
        {
            "label": f"c{index}",
            "group_id": item.group_id,
            "representative_event_id": item.event_id,
            "question_excerpt": item.question[:280],
            "score": item.score,
        }
        for index, item in enumerate(items, 1)
    ]


def install_dedup_routes(app: Any) -> None:
    router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(_reject_bearer)])
    windows: dict[str, list[float]] = {}

    @router.get("/groups/{group_id}/similar")
    def similar(group_id: str, request: Request, auth: AdminAuth) -> Any:
        del auth
        db: Session = request.state.db
        group = db.get(FeedbackGroup, _id(group_id)) if _id(group_id) else None
        if group is None:
            return error_response(404, "not_found", "Group not found", request.state.request_id)
        representative = db.get(FeedbackEvent, group.representative_event_id)
        assert representative is not None
        return {
            "items": _public_candidates(
                candidates(
                    db, group.project_id, group.event_type, representative.question, group.id, 10
                )
            )
        }

    @router.post("/projects/{project_id}/dedup/preview")
    async def preview(project_id: str, body: PreviewBody, request: Request, auth: AdminAuth) -> Any:
        admin, _, _ = auth
        db: Session = request.state.db
        project = db.get(Project, _id(project_id)) if _id(project_id) else None
        if project is None:
            return error_response(404, "not_found", "Project not found", request.state.request_id)
        now = time.monotonic()
        recent = [value for value in windows.get(admin.id, []) if now - value < 60]
        if len(recent) >= 10:
            return error_response(
                429, "rate_limited", "Preview limit exceeded", request.state.request_id
            )
        windows[admin.id] = recent + [now]
        db.add(
            AuditEvent(
                actor_type="admin",
                actor_ref=admin.id,
                action="dedup.previewed",
                subject_type="project",
                subject_id=project.id,
                details_json="{}",
            )
        )
        db.commit()
        digest = question_hash(project.id, body.event_type, body.question)
        prior = db.scalar(
            select(FeedbackEvent)
            .where(
                FeedbackEvent.project_id == project.id,
                FeedbackEvent.event_type == body.event_type,
                FeedbackEvent.normalized_question_hash == digest,
            )
            .order_by(FeedbackEvent.created_at.desc(), FeedbackEvent.id.desc())
            .limit(1)
        )
        member = db.get(FeedbackGroupMember, prior.id) if prior else None
        exact_group_id = member.group_id if member else None
        settings = json.loads(project.settings_json).get("deduplication", {})
        semantic = settings.get("semantic", {})
        if settings.get("mode") == "semantic" and semantic.get("method") == "embeddings":
            embedding_response: dict[str, Any] = {
                "method": "embeddings",
                "exact_match_group_id": exact_group_id,
                "similarities": [],
                "verdict": None,
                "similarity_threshold": semantic.get(
                    "similarity_threshold", EMBEDDING_SIMILARITY_DEFAULT
                ),
            }
            if exact_group_id:
                return embedding_response
            provider = db.get(LlmProviderConfig, semantic.get("provider_id"))
            if provider is None or not provider.enabled:
                embedding_response["verdict"] = {"outcome": "provider_unavailable"}
                return embedding_response
            try:
                secrets = (
                    SecretBox.from_settings(request.app.state.settings).decrypt(
                        provider.id, provider.secrets_ciphertext, purpose="llm-provider-secrets"
                    )
                    if provider.secrets_ciphertext
                    else {}
                )
            except SecretError:
                embedding_response["verdict"] = {"outcome": "provider_unavailable"}
                return embedding_response
            db.expunge(provider)
            db.rollback()
            acquired = False
            try:
                await asyncio.wait_for(request.app.state.llm_semaphore.acquire(), timeout=2)
                acquired = True
                embedding_result = await asyncio.wait_for(
                    embed(
                        provider,
                        secrets,
                        semantic["embedding_model"],
                        [body.question[:2000]],
                    ),
                    timeout=45,
                )
            except TimeoutError:
                embedding_response["verdict"] = {"outcome": "timeout"}
                return embedding_response
            finally:
                if acquired:
                    request.app.state.llm_semaphore.release()
            if not embedding_result.vectors:
                embedding_response["verdict"] = {
                    "outcome": "provider_error",
                    "error_category": embedding_result.error_category,
                }
                return embedding_response
            try:
                vector = normalize(embedding_result.vectors[0])
            except ValueError:
                embedding_response["verdict"] = {"outcome": "provider_error"}
                return embedding_response

            def scan() -> list[tuple[str, str, float]]:
                with request.app.state.session_factory() as scan_db:
                    rows = embedding_candidates(
                        scan_db,
                        project.id,
                        body.event_type,
                        semantic["embedding_model"],
                        len(vector),
                        None,
                        semantic.get("max_scan", EMBEDDING_MAX_SCAN_DEFAULT),
                    )
                return score_embeddings(vector, rows)

            scores = await asyncio.to_thread(scan)
            excerpts = {
                event.id: event.question[:280]
                for event in db.scalars(
                    select(FeedbackEvent).where(
                        FeedbackEvent.id.in_([event_id for _, event_id, _ in scores])
                    )
                )
            }
            embedding_response["similarities"] = [
                {
                    "group_id": group_id,
                    "representative_event_id": event_id,
                    "question_excerpt": excerpts[event_id],
                    "similarity": score,
                    "threshold_met": score >= embedding_response["similarity_threshold"],
                }
                for group_id, event_id, score in scores
            ]
            embedding_response["verdict"] = {
                "outcome": "no_candidates"
                if not scores
                else "matched"
                if scores[0][2] >= embedding_response["similarity_threshold"]
                else "no_match",
                "confidence": scores[0][2] if scores else None,
            }
            return embedding_response
        offered = candidates(
            db,
            project.id,
            body.event_type,
            body.question,
            exact_group_id,
            semantic.get("candidate_count", 5),
        )
        response: dict[str, Any] = {
            "exact_match_group_id": exact_group_id,
            "candidates": _public_candidates(offered),
            "verdict": None,
        }
        if settings.get("mode") != "semantic" or exact_group_id or not offered:
            return response
        provider = db.get(LlmProviderConfig, semantic.get("provider_id"))
        if provider is None or not provider.enabled:
            response["verdict"] = {"outcome": "provider_unavailable"}
            return response
        try:
            secrets = (
                SecretBox.from_settings(request.app.state.settings).decrypt(
                    provider.id, provider.secrets_ciphertext, purpose="llm-provider-secrets"
                )
                if provider.secrets_ciphertext
                else {}
            )
        except SecretError:
            response["verdict"] = {"outcome": "provider_unavailable"}
            return response
        provider.model = semantic.get("model") or provider.model
        provider.timeout_seconds = min(
            provider.timeout_seconds, semantic.get("timeout_seconds", 15)
        )
        db.expunge(provider)
        db.rollback()
        acquired = False
        try:
            await asyncio.wait_for(request.app.state.llm_semaphore.acquire(), timeout=2)
            acquired = True
            result = await asyncio.wait_for(
                complete(
                    provider,
                    secrets,
                    SYSTEM_PROMPT,
                    prompt(body.question, offered),
                    {f"c{i}" for i in range(1, len(offered) + 1)},
                ),
                timeout=45,
            )
        except TimeoutError:
            response["verdict"] = {"outcome": "timeout"}
            return response
        finally:
            if acquired:
                request.app.state.llm_semaphore.release()
        if result.answer is None:
            response["verdict"] = {
                "outcome": "provider_error",
                "error_category": result.error_category,
            }
        else:
            answer = result.answer
            outcome = (
                "no_match"
                if not answer["same_issue"]
                else "low_confidence"
                if answer["confidence"] < semantic.get("min_confidence", 0.85)
                else "matched"
            )
            response["verdict"] = {
                "outcome": outcome,
                "candidate": answer["candidate"],
                "confidence": answer["confidence"],
                "reason": answer["reason"],
            }
        return response

    app.include_router(router)
