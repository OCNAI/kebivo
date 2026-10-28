"""AC-16: embeddings group paraphrases and fail open when the provider is down."""

import json
import sqlite3
import time

from conftest import LLM, Admin, FakeLlm, Receiver, gap, unique


def test_ac_16_embeddings_match_near_miss_and_provider_down(
    admin: Admin, llm: FakeLlm, receiver: Receiver
) -> None:
    prior_embeddings = [
        request for request in llm.requests() if request["path"].endswith("/embeddings")
    ]
    api_key = unique("E2E-EMBEDDINGS-API-KEY")
    project = admin.project()
    token = admin.token(project["id"])
    first_question = "Recover private passcode"
    baseline_questions = [first_question] + [
        f"Baseline unique topic {index}" for index in range(16)
    ]
    baseline_events = []
    baseline_groups = {}
    for question in baseline_questions:
        event_id = gap(token, question, ["it"])
        admin.processed(event_id)
        baseline_events.append(event_id)
        baseline_groups[event_id] = admin.event(event_id)["group"]["id"]
    first = baseline_events[0]

    provider = admin.post("/admin/llm-providers", {
        "name": unique("e2e embeddings provider"), "provider_type": "openai_compatible",
        "base_url": LLM + "/v1", "model": "fake", "allow_private_network": True,
        "timeout_seconds": 2, "api_key": api_key,
    })
    defaults = admin.get("/projects/dedup/defaults")["embeddings"]
    assert defaults["similarity_threshold"] == 0.93
    semantic = {"method": "embeddings", "provider_id": provider["id"],
                "embedding_model": "fake-64"}
    no_ack = admin.request("PATCH", f"/projects/{project['id']}", {"settings": {
        "deduplication": {"mode": "semantic", "semantic": semantic},
    }})
    assert no_ack.status_code == 422
    settings = {"deduplication": {
        "mode": "semantic", "privacy_acknowledged": True, "semantic": semantic,
    }}
    configured = admin.patch(f"/projects/{project['id']}", {"settings": settings})
    assert configured["settings"]["deduplication"]["semantic"]["similarity_threshold"] == 0.93

    azure = admin.post("/admin/llm-providers", {
        "name": unique("e2e Azure embeddings provider"), "provider_type": "azure_openai",
        "base_url": LLM + "/v1", "deployment": "fake", "api_version": "2025-01-01",
        "allow_private_network": True, "timeout_seconds": 2,
    })
    azure_settings = {"deduplication": {"mode": "semantic", "privacy_acknowledged": True,
                      "semantic": {**semantic, "provider_id": azure["id"]}}}
    rejected = admin.request("PATCH", f"/projects/{project['id']}", {"settings": azure_settings})
    assert rejected.status_code == 422

    path = unique("ac16")
    hook = admin.webhook(path)
    admin.rule(project["id"], ["it"], [hook["id"]])

    paraphrase = "Forgot VPN login"
    near_miss = "Forgot VPS login"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with sqlite3.connect("/appdata/app.sqlite3") as db:
            embedded = db.execute(
                "SELECT COUNT(*) FROM question_embeddings WHERE project_id=? AND model=?",
                (project["id"], "fake-64"),
            ).fetchone()[0]
            backfill = db.execute(
                "SELECT 1 FROM embedding_backfills WHERE project_id=?", (project["id"],)
            ).fetchone()
        if embedded == len(baseline_events) and backfill is None:
            break
        time.sleep(0.2)
    assert embedded == len(baseline_events) and backfill is None, (
        "backfill did not finish in batches"
    )
    assert (
        {event_id: admin.event(event_id)["group"]["id"] for event_id in baseline_events}
        == baseline_groups
    )

    deleted_event = baseline_events[-1]
    assert admin.request("DELETE", f"/admin/feedback/{deleted_event}").status_code == 200
    with sqlite3.connect("/appdata/app.sqlite3") as db:
        assert db.execute(
            "SELECT 1 FROM question_embeddings WHERE event_id=?", (deleted_event,)
        ).fetchone() is None

    preview = admin.post(f"/admin/projects/{project['id']}/dedup/preview", {
        "event_type": "knowledge_gap", "question": "How do I reset a VPN password?",
    })
    assert preview["method"] == "embeddings"
    assert len(preview["similarities"]) == 5

    matched = gap(token, paraphrase, ["it"])
    admin.processed(matched)
    separate = gap(token, near_miss, ["it"])
    admin.processed(separate)

    group = admin.group_of(first)
    assert admin.group_of(matched)["id"] == group["id"]
    members = {row["id"]: row for row in admin.group_events(group["id"])}
    assert set(members) == {first, matched}
    assert members[matched]["semantic"]["outcome"] == "matched"
    with sqlite3.connect("/appdata/app.sqlite3") as db:
        method = db.execute(
            "SELECT method FROM dedup_evaluations WHERE event_id=?", (matched,)
        ).fetchone()
    assert method == ("embeddings",)
    other_group = admin.group_of(separate)
    assert other_group["id"] != group["id"]
    assert other_group["occurrence_count"] == 1

    down_provider = admin.post("/admin/llm-providers", {
        "name": unique("e2e unavailable embeddings provider"),
        "provider_type": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
        "model": "fake", "allow_private_network": True, "timeout_seconds": 1,
    })
    settings["deduplication"]["semantic"]["provider_id"] = down_provider["id"]
    admin.patch(f"/projects/{project['id']}", {"settings": settings})
    event_id = gap(token, "Forgot my VPN login on the tablet", ["it"])
    admin.delivered(event_id)
    failed_group = admin.group_of(event_id)
    assert failed_group["id"] != group["id"] and failed_group["occurrence_count"] == 1

    calls = [request for request in llm.requests() if request["path"].endswith("/embeddings")]
    calls = calls[len(prior_embeddings):]
    assert calls
    questions = {question.lower() for question in baseline_questions + [paraphrase, near_miss,
                "How do I reset a VPN password?"]}
    for call in calls:
        body = json.loads(call["body"])
        assert set(body) == {"model", "input"}
        assert body["model"] == "fake-64"
        assert all(isinstance(question, str) for question in body["input"])
        assert set(body["input"]) <= questions
        assert len(body["input"]) <= 16
    assert any(len(json.loads(call["body"])["input"]) > 1 for call in calls), calls
    assert any(
        json.loads(hit["body"])["event_id"] == event_id for hit in receiver.hits(path)
    )
