"""AC-10: a timed-out semantic request still accepts, routes and delivers."""

import time

from conftest import Admin, FakeLlm, Receiver, gap, record_content, unique
from llm_support import semantic_project


def test_ac_10_timeout_falls_back_without_losing_the_event(
    admin: Admin, llm: FakeLlm, receiver: Receiver
) -> None:
    project, token = semantic_project(admin, timeout_seconds=1)
    path = unique("ac10")
    hook = admin.webhook(path)
    admin.rule(project["id"], ["it"], [hook["id"]])
    seed = gap(token, "Laptop docking station not charging", ["it"])
    admin.delivered(seed)
    llm.scenario("slow")
    try:
        started = time.monotonic()
        event_id = gap(token, record_content("Laptop docking station stops charging"), ["it"])
        assert time.monotonic() - started < 1.0, "the 202 waited for the model"
        admin.delivered(event_id)
    finally:
        llm.scenario("valid")
    group = admin.group_of(event_id)
    assert group["occurrence_count"] == 1
    event = admin.group_events(group["id"])[0]
    assert event["semantic"]["outcome"] == "timeout"
    assert event["match"]["method"] == "initial"
    assert len(admin.get(f"/admin/feedback?project_id={project['id']}")["items"]) == 2
    assert len(receiver.hits(path)) == 2
