"""Exercise completed Phase 5 group APIs against the running app container.

Keep merge, split, and delete acceptance in the later Phase 5 smoke task.
"""

from __future__ import annotations

import http.cookiejar
import hashlib
import json
import os
import sqlite3
import subprocess
import time
import urllib.error
from urllib.parse import urlencode
import urllib.request
from uuid import uuid4

origin = os.environ["SMOKE_ORIGIN"]
ui_origin = os.environ["SMOKE_UI_ORIGIN"]
api = os.environ["SMOKE_API"]
jar = http.cookiejar.MozillaCookieJar(os.environ["SMOKE_COOKIE_JAR"])
jar.load(ignore_discard=True, ignore_expires=True)
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
csrf = os.environ["SMOKE_CSRF"]


def request(path: str, body: object | None = None, *, token: str | None = None, method: str | None = None, idempotency_key: str | None = None):
    headers = {"Origin": origin, "Accept": "application/json"}
    data = None
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        headers["X-CSRF-Token"] = csrf
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers.pop("X-CSRF-Token", None)
        headers.pop("Origin", None)
    req = urllib.request.Request(api + path, data=data, headers=headers, method=method)
    try:
        with (urllib.request.build_opener() if token else opener).open(req, timeout=8) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else None


def ok(path: str, body: object | None = None, **kwargs):
    status, value = request(path, body, **kwargs)
    assert 200 <= status < 300, (path, status, value)
    return value


project_id = ok("/projects", {"name": "Phase 5 acceptance smoke", "slug": "phase5-acceptance"}, method="POST")["id"]
gap_token = ok(f"/projects/{project_id}/tokens", {
    "name": "phase5 gap credential", "scopes": ["feedback:gap", "feedback:correction"],
}, method="POST")["token"]


def deny_agent(path: str, method: str = "GET", body: object | None = None) -> None:
    status, _ = request(path, body, token=gap_token, method=method)
    assert status == 401, (path, status)


# Every new group administration surface is cookie-admin only, including
# read APIs and triage mutations.
group_path = "/admin/groups"
for path in (group_path, group_path + "/summary", group_path + "/smoke-group",
             group_path + "/smoke-group/events", group_path + "/smoke-group/history"):
    deny_agent(path)
for path, method, body in (
    (group_path + "/smoke-group", "PATCH", {"status": "resolved"}),
    (group_path + "/smoke-group/notes", "POST", {"body": "denied"}),
    (group_path + "/smoke-group/notes/smoke-note", "DELETE", None),
    (group_path + "/smoke-group/tags", "PATCH", {"add": ["denied"]}),
):
    deny_agent(path, method, body)

# A separately scoped credential provides a real correction-type result.
correction = ok(f"/projects/{project_id}/tokens", {
    "name": "phase5 correction smoke", "scopes": ["feedback:gap", "feedback:correction"],
}, method="POST")["token"]
ok(f"/projects/{project_id}", {"settings": {"corrections": {"enabled": True}, "deduplication": {"enabled": False}}}, method="PATCH")

canary = "P5-FEEDBACK-PRIVATE-CANARY-71c9"
def submit(token: str, event_type: str, question: str, tags: list[str], key: str) -> str:
    payload = {"event_type": event_type, "question": question, "tags": tags}
    if event_type == "knowledge_correction":
        payload["correction"] = "P5-ANSWER-PRIVATE-CANARY-c1a8"
    status, result = request("/feedback", payload, token=token, method="POST", idempotency_key=key)
    assert status == 202, (status, result)
    return result["event_id"]


gap_a = submit(gap_token, "knowledge_gap", canary, ["it"], "ignored-a")
gap_b = submit(gap_token, "knowledge_gap", "P5 separate group 83b2", ["it"], "ignored-b")
gap_hr = submit(gap_token, "knowledge_gap", "P5 HR group 4ad1", ["hr"], "ignored-hr")
correction_id = submit(correction, "knowledge_correction", "P5 correction type d0e4", ["phase5-alpha"], "ignored-c")
merge_corrections = [
    submit(correction, "knowledge_correction", f"P5 merge correction {index}", ["merge-set"], f"merge-correction-{index}")
    for index in range(2, 6)
]
search_delete_canary = "P5-SEARCH-DELETE-CANARY-06b2"
delete_search_id = submit(gap_token, "knowledge_gap", search_delete_canary, ["search-canary"], "ignored-delete")
groups = ok(group_path + "?project_id=" + project_id + "&sort=occurrences_desc")["items"]
by_event = {row["representative"]["event_id"]: row for row in groups}
assert {gap_a, gap_b, gap_hr, correction_id, delete_search_id} <= by_event.keys(), groups
group_id = by_event[gap_a]["id"]
assert [row["occurrence_count"] for row in groups if row["id"] in {by_event[gap_a]["id"], by_event[gap_b]["id"]}] == [1, 1]

assert {row["id"] for row in ok(group_path + "?project_id=" + project_id + "&event_type=knowledge_gap")["items"]} >= {by_event[gap_a]["id"], by_event[gap_b]["id"], by_event[gap_hr]["id"]}
assert all(row["event_type"] == "knowledge_correction" for row in ok(group_path + "?project_id=" + project_id + "&event_type=knowledge_correction")["items"])
assert {row["id"] for row in ok(group_path + "?project_id=" + project_id + "&tag=it&status=open")["items"]} == {by_event[gap_a]["id"], by_event[gap_b]["id"]}
assert {row["id"] for row in ok(group_path + "?project_id=" + project_id + "&tag=hr")["items"]} == {by_event[gap_hr]["id"]}
assert all(row["project"]["id"] == project_id for row in groups)
assert [row["occurrence_count"] for row in groups] == sorted(
    (row["occurrence_count"] for row in groups), reverse=True
)
assert ok(group_path + "?project_id=" + project_id + "&q=separate")["items"][0]["id"] == by_event[gap_b]["id"]

# The admin triage flow persists status, notes, tags and history.
ok(f"{group_path}/{group_id}", {"status": "resolved"}, method="PATCH")
assert ok(f"{group_path}/{group_id}")["status"] == "resolved"
resolved_detail = ok(f"{group_path}/{group_id}")
assert resolved_detail["resolved_at"] is not None
assert {row["id"] for row in ok(group_path + "?project_id=" + project_id + "&status=open&tag=it")["items"]} == {by_event[gap_b]["id"]}
assert {row["id"] for row in ok(group_path + "?project_id=" + project_id + "&status=resolved&tag=it")["items"]} == {group_id}
multi_status_query = urlencode([("project_id", project_id), ("status", "open"), ("status", "resolved")])
assert {row["id"] for row in ok(group_path + "?" + multi_status_query)["items"]} >= {by_event[gap_a]["id"], by_event[gap_b]["id"]}
note = ok(f"{group_path}/{group_id}/notes", {"body": "P5 admin-only note"}, method="POST")
ok(f"{group_path}/{group_id}/tags", {"add": ["triaged-p5"]}, method="PATCH")
detail = ok(f"{group_path}/{group_id}")
assert any(item["id"] == note["id"] for item in detail["notes"])
assert "triaged-p5" in {item["slug"] for item in detail["tags"]}
multi_tag_query = urlencode([("project_id", project_id), ("tag", "it"), ("tag", "triaged-p5")])
assert {row["id"] for row in ok(group_path + "?" + multi_tag_query)["items"]} == {group_id}
date_query = urlencode([
    ("project_id", project_id), ("tag", "it"), ("status", "resolved"), ("q", "canary"),
    ("first_seen_from", detail["first_seen_at"]), ("last_seen_to", detail["last_seen_at"]),
    ("min_occurrences", 1),
])
assert {row["id"] for row in ok(group_path + "?" + date_query)["items"]} == {group_id}
history = ok(f"{group_path}/{group_id}/history")["items"]
assert {item["action"] for item in history} >= {"status_changed", "note_added", "tags_changed"}
assert {row["id"] for row in ok(group_path + "?project_id=" + project_id + "&status=resolved")["items"]} >= {group_id}
occurrences = ok(f"{group_path}/{group_id}/events")["items"]
assert any(row["id"] == gap_a and row["agent_reported"]["question"] == canary for row in occurrences)

# Merge three or more groups containing five correction events, verify each
# original question and correction, then split two events back out.
correction_group_id = by_event[correction_id]["id"]
merge_event_ids = [correction_id, *merge_corrections]
source_group_ids = [by_event[event_id]["id"] for event_id in merge_corrections]
merged = ok(f"{group_path}/{correction_group_id}/merge", {"source_group_ids": source_group_ids}, method="POST")
assert merged["occurrence_count"] == 5
assert {row["id"] for row in ok(group_path + "?project_id=" + project_id + "&min_occurrences=5")["items"]} == {correction_group_id}
merged_events = ok(f"{group_path}/{correction_group_id}/events")["items"]
assert {row["id"] for row in merged_events} == set(merge_event_ids)
assert all(row["agent_reported"]["correction"] == "P5-ANSWER-PRIVATE-CANARY-c1a8" for row in merged_events)
split_event_ids = merge_corrections[:2]
split = ok(f"{group_path}/{correction_group_id}/split", {"event_ids": split_event_ids}, method="POST")
assert split["occurrence_count"] == 2
assert {row["id"] for row in ok(f"{group_path}/{correction_group_id}/events")["items"]} == {correction_id, *merge_corrections[2:]}
assert {row["id"] for row in ok(f"{group_path}/{split['group_id']}/events")["items"]} == set(split_event_ids)
for event_id in merge_event_ids:
    detail_event = next(row for row in merged_events if row["id"] == event_id)
    expected_question = "P5 correction type d0e4" if event_id == correction_id else f"P5 merge correction {merge_corrections.index(event_id) + 2}"
    assert detail_event["agent_reported"]["question"] == expected_question
ok(f"/admin/feedback/{delete_search_id}", method="DELETE")
assert not ok(group_path + "?project_id=" + project_id + "&q=" + search_delete_canary)["items"]

# Exercise every documented ordering and walk three one-row pages without a
# duplicate or skipped group.
all_ids = {row["id"] for row in ok(group_path + "?project_id=" + project_id)["items"]}
for ordering in ("last_activity_desc", "last_activity_asc", "occurrences_desc", "occurrences_asc", "status", "first_reported_desc"):
    ordered = ok(group_path + "?" + urlencode([("project_id", project_id), ("sort", ordering)]))["items"]
    assert {row["id"] for row in ordered} == all_ids, ordering
cursor = None
paginated_ids = []
for _ in range(3):
    params = [("project_id", project_id), ("limit", 1)]
    if cursor:
        params.append(("cursor", cursor))
    page = ok(group_path + "?" + urlencode(params))
    paginated_ids.extend(row["id"] for row in page["items"])
    cursor = page["next_cursor"]
    if not cursor:
        break
assert len(paginated_ids) == len(set(paginated_ids)) and set(paginated_ids) <= all_ids
assert len(paginated_ids) == 3
first_page_ids = [row["id"] for row in ok(group_path + "?" + urlencode([("project_id", project_id), ("sort", "last_activity_desc")]))["items"]]
assert paginated_ids == first_page_ids[:3]

with sqlite3.connect("/data/app.sqlite3") as connection:
    delete_audit = connection.execute(
        "SELECT details_json FROM audit_events WHERE action='feedback.deleted' AND subject_id=?",
        (delete_search_id,),
    ).fetchall()
assert delete_audit and all(search_delete_canary not in row[0] for row in delete_audit)

# Phase 6 exact matching stays within the same ingestion transaction.
ok(f"/projects/{project_id}", {"settings": {"corrections": {"enabled": True}, "deduplication": {"mode": "exact"}}}, method="PATCH")
exact_a = submit(gap_token, "knowledge_gap", "How do I reset my VPN?", [], "p6-exact-a")
exact_b = submit(gap_token, "knowledge_gap", "  how do I reset my VPN  ", [], "p6-exact-b")
exact_groups = ok(group_path + "?project_id=" + project_id + "&q=reset%20my%20VPN")["items"]
assert len(exact_groups) == 1 and exact_groups[0]["occurrence_count"] == 2
exact_members = ok(f"{group_path}/{exact_groups[0]['id']}/events")["items"]
assert {row["id"] for row in exact_members} == {exact_a, exact_b}
assert {row["match"]["method"] for row in exact_members} == {"initial", "exact"}

# Seed 10,000 isolated, completed events and one-event groups in the built
# container DB. The FTS insert trigger indexes the questions as production does.
perf_tag_id = str(uuid4())
perf_now = "2026-10-18T00:00:00+00:00"
perf_events = []
perf_groups = []
perf_members = []
perf_jobs = []
perf_tags = []
for index in range(10_000):
    event_id, group_id_perf = str(uuid4()), str(uuid4())
    question = f"phase5latency sample feedback {index}"
    perf_events.append((event_id, project_id, "rest", "knowledge_gap", question,
                        hashlib.sha256(question.lower().encode()).hexdigest(), "{}", "completed", perf_now, perf_now))
    perf_groups.append((group_id_perf, project_id, "knowledge_gap", event_id, "open", perf_now, 1, perf_now, perf_now, perf_now, perf_now))
    perf_members.append((event_id, group_id_perf, "initial", perf_now))
    perf_jobs.append((str(uuid4()), event_id, "completed", 0, perf_now, perf_now))
    perf_tags.append((event_id, perf_tag_id, 0, 1, 0))
with sqlite3.connect("/data/app.sqlite3") as connection:
    connection.execute(
        "INSERT INTO tags(id,project_id,name,slug,is_default,created_at) VALUES(?,?,?,?,0,?)",
        (perf_tag_id, project_id, "phase5perf", "phase5perf", perf_now),
    )
    connection.executemany(
        "INSERT INTO feedback_events(id,project_id,submitted_via,event_type,question,normalized_question_hash,metadata_json,processing_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        perf_events,
    )
    connection.executemany(
        "INSERT INTO feedback_groups(id,project_id,event_type,representative_event_id,status,status_changed_at,occurrence_count,first_seen_at,last_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        perf_groups,
    )
    connection.executemany(
        "INSERT INTO feedback_group_members(event_id,group_id,match_method,added_at) VALUES(?,?,?,?)",
        perf_members,
    )
    connection.executemany(
        "INSERT INTO processing_jobs(id,event_id,status,attempt_count,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        perf_jobs,
    )
    connection.executemany(
        "INSERT INTO feedback_event_tags(event_id,tag_id,from_default,from_submission,from_admin) VALUES(?,?,?,?,?)",
        perf_tags,
    )

latencies_ms = []
perf_query = group_path + "?" + urlencode([
    ("project_id", project_id), ("q", "phase5latency"), ("tag", "phase5perf"), ("status", "open")
])
for _ in range(20):
    started = time.perf_counter()
    results = ok(perf_query)
    latencies_ms.append((time.perf_counter() - started) * 1000)
    assert len(results["items"]) == 50
latencies_ms.sort()
search_p95_ms = round(latencies_ms[18], 2)
search_median_ms = round(latencies_ms[9], 2)
subprocess.run(["python", "-m", "kebivo.cli", "check-groups"], capture_output=True, text=True, check=True)
with sqlite3.connect("/data/app.sqlite3") as connection:
    event_count = connection.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0]
    occurrence_total = connection.execute("SELECT COALESCE(SUM(occurrence_count), 0) FROM feedback_groups").fetchone()[0]
assert occurrence_total == event_count, (occurrence_total, event_count)

# The command checks membership, representatives and stored counts. Also assert
# the aggregate count matches the surviving event rows in this same container DB.
check = subprocess.run(["python", "-m", "kebivo.cli", "check-groups"], capture_output=True, text=True, check=True)
assert "consistent" in check.stdout.lower(), check.stdout
with sqlite3.connect("/data/app.sqlite3") as connection:
    event_count = connection.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0]
    occurrence_total = connection.execute("SELECT COALESCE(SUM(occurrence_count), 0) FROM feedback_groups").fetchone()[0]
assert occurrence_total == event_count, (occurrence_total, event_count)

# The real shell and bundled UI entry points are served; API reads above prove
# those screens have backing persisted data. Browser assertions run separately.
for route in ("/inbox", f"/inbox/groups/{group_id}"):
    with urllib.request.urlopen(ui_origin + route, timeout=8) as response:
        page = response.read().decode()
        assert response.status == 200 and '<div id="root">' in page, route

# Feedback content may be returned to an authorized admin, but must not be
# copied to structured audit details.
import sqlite3
with sqlite3.connect("/data/app.sqlite3") as connection:
    audit_rows = connection.execute("SELECT details_json FROM audit_events").fetchall()
audit_text = json.dumps(audit_rows)
assert canary not in audit_text and "P5-ANSWER-PRIVATE-CANARY" not in audit_text

print(json.dumps({
    "agent_token_denied_all_group_admin_routes": "passed",
    "project_type_tag_search_sort_filters": "passed",
    "status_note_tag_history": "passed",
    "individual_occurrences": "passed",
    "merge_split_delete_membership": "passed",
    "check_groups_and_occurrence_total": {"passed": True, "events": event_count, "occurrences": occurrence_total},
    "audit_feedback_redaction": "passed",
    "inbox_and_detail_shell_routes": "passed",
    "group_ids": [by_event[gap_a]["id"], by_event[gap_b]["id"], by_event[correction_id]["id"]],
    "merge_split_five_event_groups": {"merged": 5, "split_out": 2},
    "ac13_target_group_id": group_id,
    "split_group_id": split["group_id"],
    "ac13_open_it_group_id": by_event[gap_b]["id"],
    "ac13_project_id": project_id,
    "search_delete_canary": search_delete_canary,
    "search_10k_events": {"calls": len(latencies_ms), "p95_ms": search_p95_ms, "median_ms": search_median_ms},
    "final_check_groups_totals": {"events": event_count, "occurrences": occurrence_total},
}))
