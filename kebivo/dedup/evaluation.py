"""Read-only threshold evaluation for labelled question pairs."""

import asyncio
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kebivo.dedup.embeddings import cosine, normalize
from kebivo.dedup.semantic import candidates
from kebivo.llm.adapter import embed
from kebivo.models import (
    DedupEvaluation,
    FeedbackEvent,
    FeedbackGroup,
    FeedbackGroupHistory,
    FeedbackGroupMember,
    LlmProviderConfig,
    Project,
)
from kebivo.security.secretbox import SecretBox
from kebivo.settings import Settings


def corpus() -> list[tuple[str, str, bool]]:
    path = Path(__file__).with_name("embedding_pairs.json")
    rows = json.loads(path.read_text())
    return [(row["left"], row["right"], row["label"]) for row in rows]


def project_pairs(db: Session, project_id: str) -> list[tuple[str, str, bool]]:
    pairs: set[tuple[str, str, bool]] = set()
    evaluations = db.scalars(
        select(DedupEvaluation).where(
            DedupEvaluation.project_id == project_id,
            DedupEvaluation.outcome.in_(["matched", "no_match", "low_confidence"]),
        )
    ).all()
    for evaluation in evaluations:
        event = db.get(FeedbackEvent, evaluation.event_id)
        if event is None:
            continue
        if evaluation.outcome == "matched" and evaluation.matched_group_id:
            group = db.get(FeedbackGroup, evaluation.matched_group_id)
            representative = db.get(FeedbackEvent, group.representative_event_id) if group else None
            if representative is not None and event.id != representative.id:
                pairs.add((event.question, representative.question, True))
        else:
            member = db.get(FeedbackGroupMember, event.id)
            for candidate in candidates(
                db,
                project_id,
                event.event_type,
                event.question,
                member.group_id if member else None,
                5,
            ):
                pairs.add((event.question, candidate.question, False))
    histories = db.scalars(
        select(FeedbackGroupHistory)
        .join(FeedbackGroup, FeedbackGroup.id == FeedbackGroupHistory.group_id)
        .where(
            FeedbackGroup.project_id == project_id,
            FeedbackGroupHistory.action.in_(["merged", "split_in"]),
            FeedbackGroupHistory.actor_type == "admin",
        )
    ).all()
    for history in histories:
        if history.action == "merged":
            group = db.get(FeedbackGroup, history.group_id)
            rep = db.get(FeedbackEvent, group.representative_event_id) if group else None
            if rep is not None and group is not None:
                for member in db.scalars(
                    select(FeedbackGroupMember).where(
                        FeedbackGroupMember.group_id == group.id,
                        FeedbackGroupMember.match_method == "manual_merge",
                    )
                ):
                    event = db.get(FeedbackEvent, member.event_id)
                    if event is not None and event.id != rep.id:
                        pairs.add((event.question, rep.question, True))
            continue
        detail = json.loads(history.details_json)
        source = db.get(FeedbackGroup, detail.get("source_group_id"))
        target = db.get(FeedbackGroup, detail.get("new_group_id"))
        left = db.get(FeedbackEvent, source.representative_event_id) if source else None
        right = db.get(FeedbackEvent, target.representative_event_id) if target else None
        if left and right:
            pairs.add((left.question, right.question, False))
    return sorted(pairs)


async def score_pairs(
    provider: LlmProviderConfig,
    secrets: dict[str, str],
    model: str,
    pairs: list[tuple[str, str, bool]],
) -> list[tuple[float, bool]]:
    texts = list(dict.fromkeys(text for left, right, _ in pairs for text in (left, right)))
    vectors: dict[str, list[float]] = {}
    for start in range(0, len(texts), 16):
        batch = texts[start : start + 16]
        result = await embed(provider, secrets, model, batch)
        if result.vectors is None:
            raise ValueError(f"Embedding provider failed: {result.error_category}")
        vectors.update(
            (text, normalize(vector)) for text, vector in zip(batch, result.vectors, strict=True)
        )
    return [(cosine(vectors[left], vectors[right]), label) for left, right, label in pairs]


def table(scores: list[tuple[float, bool]]) -> str:
    lines = ["threshold precision recall tp fp fn"]
    for step in range(50, 100):
        threshold = step / 100
        tp = sum(label and score >= threshold for score, label in scores)
        fp = sum(not label and score >= threshold for score, label in scores)
        fn = sum(label and score < threshold for score, label in scores)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        lines.append(f"{threshold:.2f} {precision:.3f} {recall:.3f} {tp} {fp} {fn}")
    return "\n".join(lines)


def evaluate_project(db: Session, settings: Settings, project_id: str, use_corpus: bool) -> str:
    project = db.get(Project, project_id)
    if project is None:
        raise ValueError("Project not found")
    dedup = json.loads(project.settings_json).get("deduplication", {})
    semantic = dedup.get("semantic", {})
    if dedup.get("privacy_acknowledged") is not True or semantic.get("method") != "embeddings":
        raise ValueError("Project must acknowledge privacy and configure embeddings")
    provider = db.get(LlmProviderConfig, semantic.get("provider_id"))
    if provider is None or not provider.enabled:
        raise ValueError("Project embedding provider is unavailable")
    secrets = (
        SecretBox.from_settings(settings).decrypt(
            provider.id, provider.secrets_ciphertext, purpose="llm-provider-secrets"
        )
        if provider.secrets_ciphertext
        else {}
    )
    pairs = corpus() if use_corpus else project_pairs(db, project_id)
    if not pairs:
        raise ValueError("No labelled pairs available")
    scores = asyncio.run(
        score_pairs(
            provider, secrets, semantic["embedding_model"], pairs
        )
    )
    return f"pairs={len(pairs)} model={semantic['embedding_model']}\n{table(scores)}"
