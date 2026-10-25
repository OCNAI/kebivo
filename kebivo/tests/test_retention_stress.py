"""Optional 10,000-event write-lock stress run: RUN_RETENTION_STRESS=1 uv run pytest ..."""

import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from kebivo.feedback.groups import check_integrity
from kebivo.retention import run
from kebivo.tests.test_projects import make_client, write_headers
from kebivo.tests.test_tokens import issue_token


@pytest.mark.skipif(
    os.getenv("RUN_RETENTION_STRESS") != "1", reason="run explicitly for the 10k stress sample"
)
def test_ten_thousand_expired_events_do_not_block_ingestion(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "stress.db")
    project = client.post(
        "/api/v1/projects",
        json={"name": "Stress", "slug": "stress"},
        headers=write_headers(client),
    ).json()
    token = issue_token(client, project["id"], "stress").json()["token"]
    app.state.settings = replace(
        app.state.settings,
        feedback_rate_limit_per_token=100_000,
        feedback_rate_limit_per_ip=100_000,
    )
    old = datetime(2025, 1, 1, tzinfo=UTC).isoformat()
    events = []
    groups = []
    members = []
    for number in range(10_000):
        event_id, group_id = str(uuid4()), str(uuid4())
        question = f"expired-{number}"
        events.append(
            {
                "id": event_id,
                "project_id": project["id"],
                "question": question,
                "hash": hashlib.sha256(question.encode()).hexdigest(),
                "time": old,
            }
        )
        groups.append(
            {"id": group_id, "project_id": project["id"], "event_id": event_id, "time": old}
        )
        members.append({"event_id": event_id, "group_id": group_id, "time": old})
    with app.state.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO feedback_events("
                "id,project_id,submitted_via,event_type,question,"
                "normalized_question_hash,created_at,updated_at) "
                "VALUES (:id,:project_id,'rest','knowledge_gap',:question,:hash,:time,:time)"
            ),
            events,
        )
        connection.execute(
            text(
                "INSERT INTO feedback_groups("
                "id,project_id,event_type,representative_event_id,status,occurrence_count,"
                "first_seen_at,last_seen_at,status_changed_at,created_at,updated_at) "
                "VALUES (:id,:project_id,'knowledge_gap',:event_id,'open',1,"
                ":time,:time,:time,:time,:time)"
            ),
            groups,
        )
        connection.execute(
            text(
                "INSERT INTO feedback_group_members(event_id,group_id,match_method,added_at) "
                "VALUES (:event_id,:group_id,'initial',:time)"
            ),
            members,
        )
    durations = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(
            run, app.state.session_factory, app.state.settings, datetime(2026, 1, 1, tzinfo=UTC)
        )
        while not future.done():
            start = time.perf_counter()
            response = client.post(
                "/api/v1/feedback",
                json={
                    "event_type": "knowledge_gap",
                    "question": str(uuid4()),
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            durations.append(time.perf_counter() - start)
            assert response.status_code == 202, response.text
            time.sleep(0.05)
        counts = future.result()
    assert counts["feedback"] == 10_000
    with app.state.session_factory() as db:
        assert check_integrity(db) == []
    assert durations
    p95 = sorted(durations)[int(0.95 * (len(durations) - 1))]
    print(
        f"retention ingest requests={len(durations)} p95_seconds={p95:.3f} "
        f"max_seconds={max(durations):.3f}"
    )
