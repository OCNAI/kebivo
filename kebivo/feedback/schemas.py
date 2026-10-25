"""Strict public feedback submissions."""

import json
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kebivo.feedback.tags import display_tag_v1, normalize_tag_v1


def _text(value: str | None, maximum: int, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError("Required text is missing")
        return None
    if any(
        (
            unicodedata.category(char) == "Cs"
            or ord(char) in range(127, 160)
            or (ord(char) < 32 and char not in "\t\n\r")
        )
        for char in value
    ):
        raise ValueError("Control characters are forbidden")
    value = value.strip()
    if not value:
        if required:
            raise ValueError("Text cannot be blank")
        return None
    if len(value) > maximum:
        raise ValueError("Text exceeds maximum length")
    return value


def _metadata_depth(value: object, depth: int = 1) -> None:
    if depth > 8:
        raise ValueError("Metadata exceeds maximum depth")
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or len(key) > 128
                or any(
                    unicodedata.category(char) == "Cs" or ord(char) < 32 or 127 <= ord(char) <= 159
                    for char in key
                )
            ):
                raise ValueError("Metadata key exceeds maximum length")
            _metadata_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _metadata_depth(item, depth + 1)
    elif isinstance(value, str):
        if any(
            unicodedata.category(char) == "Cs" or ord(char) < 32 or 127 <= ord(char) <= 159
            for char in value
        ):
            raise ValueError("Metadata contains a control character")


class FeedbackBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    question: str
    previous_answer: str | None = None
    context: str | None = None
    agent_id: str | None = None
    conversation_id: str | None = None
    source_event_id: str | None = None
    user_reference: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_fields(self) -> "FeedbackBase":
        for name, limit in (
            ("question", 10000),
            ("correction", 20000),
            ("previous_answer", 10000),
            ("context", 4000),
            ("source_reference", 2000),
            ("agent_id", 128),
            ("conversation_id", 256),
            ("source_event_id", 256),
            ("user_reference", 256),
        ):
            if hasattr(self, name):
                setattr(
                    self,
                    name,
                    _text(getattr(self, name), limit, name in {"question", "correction"}),
                )
        return self

    @field_validator("tags")
    @classmethod
    def check_tags(cls, tags: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for tag in tags:
            slug = normalize_tag_v1(tag)
            display = display_tag_v1(tag)
            if slug not in seen:
                seen.add(slug)
                result.append(display)
        return result

    @field_validator("metadata")
    @classmethod
    def check_metadata(cls, metadata: dict[str, object]) -> dict[str, object]:
        _metadata_depth(metadata)
        try:
            size = len(
                json.dumps(
                    metadata, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
            )
        except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
            raise ValueError("Invalid metadata") from exc
        if size > 8192:
            raise ValueError("Metadata exceeds 8192 bytes")
        return metadata


class GapSubmission(FeedbackBase):
    event_type: Literal["knowledge_gap"]


class CorrectionSubmission(FeedbackBase):
    event_type: Literal["knowledge_correction"]
    correction: str
    source_reference: str | None = None


FeedbackSubmission = GapSubmission | CorrectionSubmission
