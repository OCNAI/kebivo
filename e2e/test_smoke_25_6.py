"""PRD §25.6: the fifteen-step end-to-end smoke test, in order, in one test."""

import os

import httpx
from conftest import APP, Admin, Receiver, record_content, submit, unique


def test_smoke_25_6_all_steps_in_order(admin: Admin, receiver: Receiver) -> None:
    # 1-2. scripts/e2e.sh started a temporary stack and bootstrapped the admin.
    assert httpx.get(APP + "/health/ready", timeout=5).json()["status"] == "ok"
    assert admin.get("/auth/session")["admin"]["email"] == os.environ["E2E_ADMIN_EMAIL"]
    # 3. Create a project.
    project = admin.project(unique("Smoke 25.6"))
    # 4. Generate a project token.
    token = admin.token(project["id"])
    # 5. Configure a local test webhook.
    path = unique("smoke256")
    hook = admin.webhook(path)
    # 6. Create a tag-based routing rule.
    rule = admin.rule(project["id"], ["it"], [hook["id"]])
    # 7. Submit a knowledge gap.
    question = record_content(unique("Smoke how do I reset my VPN token"))
    body = {"event_type": "knowledge_gap", "question": question, "tags": ["it"]}
    first = submit(token, body, "smoke-25-6-key-1")
    assert first.status_code == 202, first.text
    event_id = first.json()["event_id"]
    # 8. Confirm persistence.
    detail = admin.processed(event_id)
    assert detail["agent_reported"]["question"] == question
    # 9. Confirm routing.
    assert [(m["rule_id"], m["destination_id"]) for m in detail["routing"]["matches"]] == [
        (rule["id"], hook["id"])]
    # 10. Confirm webhook delivery.
    delivery = admin.delivered(event_id)[0]
    hits = receiver.hits(path)
    assert [hit["headers"]["webhook-id"] for hit in hits] == [delivery["id"]]
    # 11. Resubmit with the same idempotency key.
    replay = submit(token, body, "smoke-25-6-key-1")
    assert replay.status_code == 202 and replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json()["event_id"] == event_id
    # 12. No second event is created.
    events = admin.get(f"/admin/feedback?project_id={project['id']}")["items"]
    assert [row["id"] for row in events] == [event_id]
    # 13. Submit an exact duplicate with a new idempotency key.
    duplicate = submit(token, {**body, "question": question.upper()}, "smoke-25-6-key-2")
    assert duplicate.status_code == 202
    duplicate_id = duplicate.json()["event_id"]
    admin.processed(duplicate_id)
    # 14. One feedback group with multiple occurrences.
    groups = admin.get(f"/admin/groups?project_id={project['id']}")["items"]
    assert len(groups) == 1 and groups[0]["occurrence_count"] == 2
    group_id = groups[0]["id"]
    # 15. Inbox behaviour: both events listed newest first, filters and status work.
    events = admin.get(f"/admin/feedback?project_id={project['id']}")["items"]
    assert [row["id"] for row in events] == [duplicate_id, event_id]
    assert {row["group_id"] for row in events} == {group_id}
    open_it = admin.get(f"/admin/groups?project_id={project['id']}&status=open&tag=it")["items"]
    assert [row["id"] for row in open_it] == [group_id]
    assert admin.get(f"/admin/groups?project_id={project['id']}&tag=hr")["items"] == []
    admin.patch(f"/admin/groups/{group_id}", {"status": "in_progress"})
    assert admin.get(f"/admin/groups?project_id={project['id']}&status=open")["items"] == []
    assert len(admin.group_events(group_id)) == 2
