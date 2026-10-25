# Project data export

An administrator can download one project's feedback as JSON Lines with `GET /api/v1/admin/projects/{id}/export`. A valid admin session is required; project bearer tokens cannot export. The response has `application/x-ndjson` and one JSON object per line, ordered by creation time and event ID. It is streamed in bounded pages. Exporting writes a `project.exported` audit row. The export excludes token information, destination secrets, provider keys, group notes, LLM reasons and idempotency keys. CSV is not available.

Each line follows this JSON Schema:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["id", "project_id", "event_type", "submitted_via", "question", "correction", "previous_answer", "context", "source_reference", "agent_id", "conversation_id", "source_event_id", "user_reference", "metadata", "processing_status", "created_at", "updated_at", "tags", "group", "deliveries"],
  "properties": {
    "id": {"type": "string", "format": "uuid"},
    "project_id": {"type": "string", "format": "uuid"},
    "event_type": {"enum": ["knowledge_gap", "knowledge_correction"]},
    "submitted_via": {"enum": ["rest", "mcp"]},
    "question": {"type": "string"},
    "correction": {"type": ["string", "null"]},
    "previous_answer": {"type": ["string", "null"]},
    "context": {"type": ["string", "null"]},
    "source_reference": {"type": ["string", "null"]},
    "agent_id": {"type": ["string", "null"]},
    "conversation_id": {"type": ["string", "null"]},
    "source_event_id": {"type": ["string", "null"]},
    "user_reference": {"type": ["string", "null"]},
    "metadata": {"type": "object"},
    "processing_status": {"type": "string"},
    "created_at": {"type": "string", "format": "date-time"},
    "updated_at": {"type": "string", "format": "date-time"},
    "tags": {"type": "array", "items": {"type": "string"}},
    "group": {"type": ["object", "null"], "additionalProperties": false, "required": ["id", "status", "match_method"], "properties": {"id": {"type": "string", "format": "uuid"}, "status": {"type": "string"}, "match_method": {"type": "string"}}},
    "deliveries": {"type": "array", "items": {"type": "object", "additionalProperties": false, "required": ["status", "attempt_count", "destination_id"], "properties": {"status": {"type": "string"}, "attempt_count": {"type": "integer", "minimum": 0}, "destination_id": {"type": "string", "format": "uuid"}}}}
  }
}
```

The export contains event text as submitted, including any personal data in it. Protect and delete downloaded files according to your data policy.
