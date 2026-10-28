"""Compact, normalised vectors for question similarity."""

import math
import sys
from array import array
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from kebivo.models import QuestionEmbedding


def normalize(values: Sequence[float]) -> list[float]:
    if not 1 <= len(values) <= 8192 or any(not math.isfinite(x) for x in values):
        raise ValueError("Invalid embedding vector")
    length = math.sqrt(sum(x * x for x in values))
    if not math.isfinite(length) or length == 0:
        raise ValueError("Invalid embedding magnitude")
    return [x / length for x in values]


def encode(values: Sequence[float]) -> bytes:
    floats = array("f", normalize(values))
    if sys.byteorder != "little":
        floats.byteswap()
    return floats.tobytes()


def decode(blob: bytes) -> list[float]:
    if not blob or len(blob) % 4 or len(blob) > 8192 * 4:
        raise ValueError("Invalid embedding bytes")
    floats = array("f")
    floats.frombytes(blob)
    if sys.byteorder != "little":
        floats.byteswap()
    return floats.tolist()


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Embedding dimensions differ")
    return sum(a * b for a, b in zip(left, right, strict=True))


def store(
    db: Session,
    *,
    event_id: str,
    project_id: str,
    event_type: str,
    provider_id: str | None,
    model: str,
    values: Sequence[float],
) -> None:
    db.execute(
        insert(QuestionEmbedding)
        .values(
            event_id=event_id,
            project_id=project_id,
            event_type=event_type,
            provider_id=provider_id,
            model=model,
            dims=len(values),
            vector=encode(values),
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
    )


def load(
    db: Session, *, project_id: str, event_type: str, model: str, dims: int
) -> list[QuestionEmbedding]:
    return list(
        db.scalars(
            select(QuestionEmbedding).where(
                QuestionEmbedding.project_id == project_id,
                QuestionEmbedding.event_type == event_type,
                QuestionEmbedding.model == model,
                QuestionEmbedding.dims == dims,
            )
        )
    )
