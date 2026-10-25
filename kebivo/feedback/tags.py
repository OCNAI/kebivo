"""Version 1 of project tag normalization."""

import re
import unicodedata


def normalize_tag_v1(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Tag must be a string")
    if any(
        unicodedata.category(char) == "Cs" or ord(char) < 32 or 127 <= ord(char) <= 159
        for char in value
        if char not in "\t\n\r"
    ):
        raise ValueError("Tag contains a control character")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = re.sub(r"\s+", "-", normalized)
    if any(char not in "-_" and unicodedata.category(char)[0] not in "LMN" for char in normalized):
        raise ValueError("Tag contains an unsupported character")
    normalized = re.sub(r"-+", "-", normalized).strip("-_")
    if not 1 <= len(normalized) <= 64:
        raise ValueError("Tag must be 1–64 code points after normalization")
    return normalized


def display_tag_v1(value: str) -> str:
    """Store a compact, single-line display name independent of its slug."""
    display = re.sub(r"\s+", " ", value).strip()
    if len(display) > 64:
        raise ValueError("Tag display name must be at most 64 code points")
    return display
