"""Versioned exact-question normalization."""

import hashlib
import re
import unicodedata

QUOTE_DASHES = str.maketrans(
    {
        **dict.fromkeys("‘’‚‛′", "'"),
        **dict.fromkeys("“”„‟″«»", '"'),
        **dict.fromkeys("‐‑‒–—―−", "-"),
    }
)


def normalize_v1(question: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", question).casefold()).strip()


def normalize_v2(question: str) -> str:
    value = unicodedata.normalize("NFKC", question).casefold().translate(QUOTE_DASHES)
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s*(?:[?!.。？！]\s*)+$", "", value).strip()


def question_hash(project_id: str, event_type: str, question: str, version: int = 2) -> str:
    normalized = normalize_v2(question) if version == 2 else normalize_v1(question)
    return hashlib.sha256(f"{project_id}|{event_type}|{normalized}".encode()).hexdigest()
