"""AC-01: install, create a project and submit feedback without any LLM."""

import httpx
from conftest import APP, Admin, FakeLlm, gap, record_content, unique


def test_ac_01_submission_persists_and_is_visible_without_ai(admin: Admin, llm: FakeLlm) -> None:
    llm_calls = len(llm.requests())
    assert httpx.get(APP + "/health/ready", timeout=5).json()["status"] == "ok"
    project = admin.project()
    assert project["settings"]["deduplication"] == {"mode": "exact"}
    token = admin.token(project["id"])
    question = record_content(unique("AC01 How do I request a new laptop"))

    event_id = gap(token, question, ["it"])

    detail = admin.processed(event_id)
    assert detail["agent_reported"]["question"] == question
    inbox = admin.get(f"/admin/feedback?project_id={project['id']}")["items"]
    assert [row["id"] for row in inbox] == [event_id]
    groups = admin.get(f"/admin/groups?project_id={project['id']}")["items"]
    assert [row["representative"]["event_id"] for row in groups] == [event_id]
    assert '<div id="root">' in httpx.get(APP + "/inbox", timeout=5).text
    assert len(llm.requests()) == llm_calls
