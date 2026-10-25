"""Provider and project setup shared by the semantic acceptance tests."""

from typing import Any

from conftest import LLM, Admin, record_secret, unique


def semantic_project(admin: Admin, *, timeout_seconds: int = 2) -> tuple[dict[str, Any], str]:
    api_key = unique("E2E-LLM-API-KEY")
    header = unique("E2E-LLM-HEADER")
    record_secret("llm_api_key", api_key)
    record_secret("llm_header", header)
    provider = admin.post("/admin/llm-providers", {
        "name": unique("e2e provider"), "provider_type": "openai_compatible",
        "base_url": LLM + "/v1", "model": "fake", "allow_private_network": True,
        "timeout_seconds": 2, "structured_output": "prompt_only",
        "api_key": api_key, "headers": {"X-E2E-Canary": header},
    })
    project = admin.project()
    project = admin.patch(f"/projects/{project['id']}", {"settings": {"deduplication": {
        "mode": "semantic", "privacy_acknowledged": True,
        "semantic": {"provider_id": provider["id"], "min_confidence": 0.9,
                     "timeout_seconds": timeout_seconds, "max_attempts": 1},
    }}})
    return project, admin.token(project["id"])
