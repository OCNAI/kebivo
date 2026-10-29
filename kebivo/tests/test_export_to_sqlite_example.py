import io
import json
import sqlite3

from examples.export_to_sqlite import load_export


def test_export_to_sqlite_upserts_by_id() -> None:
    event = {
        "id": "event-1", "project_id": "project-1", "event_type": "knowledge_gap",
        "submitted_via": "rest", "question": "Where is it?", "correction": None,
        "previous_answer": None, "context": None, "source_reference": None,
        "agent_id": None, "conversation_id": None, "source_event_id": None,
        "user_reference": None, "metadata": {"source": "test"},
        "processing_status": "pending", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "tags": ["docs"],
        "group": None, "deliveries": [],
    }
    connection = sqlite3.connect(":memory:")
    load_export(io.StringIO(json.dumps(event) + "\n"), connection)
    event["question"] = "Where are the docs?"
    load_export(io.StringIO(json.dumps(event) + "\n"), connection)
    rows = connection.execute(
        'SELECT "id", "question", "tags", "metadata" FROM feedback'
    ).fetchall()
    assert rows == [("event-1", "Where are the docs?", '["docs"]', '{"source": "test"}')]
