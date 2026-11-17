"""MCP tool schemas must match the REST validators they front; a hand edit to either fails here."""

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from kebivo.feedback.schemas import CorrectionSubmission, GapSubmission
from kebivo.feedback.service import validate_idempotency_key
from kebivo.feedback.tags import normalize_tag_v1
from kebivo.gaps_read import MAX_LIMIT, SORTS, STATUSES
from kebivo.mcp_server import _TOOL_SCOPES, CORRECTION, GAP, LIST_GAPS, TOOLS
from kebivo.tests.test_projects import make_client
from kebivo.tokens import VALID_SCOPES, TokenCreate

UI_SCOPES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "scopes.ts"


def _accepts(tool: str, field: str, value: Any) -> bool:
    if field == "idempotency_key":
        try:
            validate_idempotency_key(value)
        except ValueError:
            return False
        return True
    model = CorrectionSubmission if tool == CORRECTION else GapSubmission
    payload: dict[str, Any] = {
        "event_type": "knowledge_correction" if tool == CORRECTION else "knowledge_gap",
        "question": "q",
    }
    if tool == CORRECTION:
        payload["correction"] = "c"
    try:
        model.model_validate({**payload, field: value})
    except ValidationError:
        return False
    return True


WRITE_FIELDS = [
    (tool, field, spec)
    for tool in (GAP, CORRECTION)
    for field, spec in TOOLS[tool].input_schema["properties"].items()
]


@pytest.mark.parametrize(("tool", "field", "spec"), WRITE_FIELDS, ids=lambda v: str(v)[:30])
def test_write_tool_limits_match_rest_validation(tool: str, field: str, spec: dict[str, Any]) -> None:
    if spec["type"] == "array":
        limit = spec["maxItems"]
        tags = [f"tag-{index}" for index in range(limit + 1)]
        assert _accepts(tool, field, tags[:limit])
        assert not _accepts(tool, field, tags)
        tag_limit = spec["items"]["maxLength"]
        assert normalize_tag_v1("t" * tag_limit)
        with pytest.raises(ValueError):
            normalize_tag_v1("t" * (tag_limit + 1))
    else:
        limit = spec["maxLength"]
        assert _accepts(tool, field, "x" * limit)
        assert not _accepts(tool, field, "x" * (limit + 1))


def test_list_open_gaps_matches_rest_query(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "drift.db")
    operation = app.openapi()["paths"]["/api/v1/groups"]["get"]
    rest = {param["name"]: param["schema"] for param in operation["parameters"]}
    mcp = TOOLS[LIST_GAPS].input_schema["properties"]
    assert set(mcp) == set(rest)
    assert mcp["status"]["items"]["enum"] == list(STATUSES)
    assert mcp["status"]["maxItems"] == len(STATUSES)
    assert mcp["sort"]["enum"] == list(SORTS)
    assert mcp["limit"]["minimum"] == rest["limit"]["minimum"] == 1
    assert mcp["limit"]["maximum"] == rest["limit"]["maximum"] == MAX_LIMIT
    assert mcp["limit"]["default"] == rest["limit"]["default"]
    cursor = next(item for item in rest["cursor"]["anyOf"] if item["type"] == "string")
    assert mcp["cursor"]["maxLength"] == cursor["maxLength"]
    app.state.engine.dispose()


def test_token_scopes_have_one_source() -> None:
    assert set(_TOOL_SCOPES.values()) == VALID_SCOPES
    assert TokenCreate.model_fields["scopes"].metadata[-1].max_length == len(VALID_SCOPES)
    ui = set(re.findall(r":\s*'([^']+)'", UI_SCOPES.read_text(encoding="utf-8")))
    assert ui == VALID_SCOPES
