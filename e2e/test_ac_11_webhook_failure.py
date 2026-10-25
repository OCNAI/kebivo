"""AC-11: HTTP 500 is retried on schedule with the same delivery identity."""

import base64
import hashlib
import hmac

from conftest import Admin, Receiver, gap, record_content, unique


def _signature(secret: str, headers: dict[str, str], body: str) -> str:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.{body}".encode()
    return "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def test_ac_11_two_500s_then_success_and_manual_recovery(admin: Admin, receiver: Receiver) -> None:
    project = admin.project()
    path = unique("ac11")
    hook = admin.webhook(path)
    admin.rule(project["id"], ["retry"], [hook["id"]])
    token = admin.token(project["id"])

    receiver.reset(fail_first=2)
    event_id = gap(token, record_content(unique("AC11 retry")), ["retry"])
    delivery = admin.delivered(event_id)[0]
    detail = admin.get(f"/deliveries/{delivery['id']}")
    assert [row["result"] for row in detail["attempts"]] == [
        "retryable_failure", "retryable_failure", "success"]
    hits = receiver.hits(path)
    assert len(hits) == 3
    assert {hit["headers"]["webhook-id"] for hit in hits} == {delivery["id"]}
    for hit in hits:
        assert hmac.compare_digest(
            _signature(hook["signing_secret"], hit["headers"], hit["body"]),
            hit["headers"]["webhook-signature"])
    # DELIVERY_RETRY_SCHEDULE_SECONDS is 1 second per step in the e2e stack.
    gaps = [b["received_at"] - a["received_at"] for a, b in zip(hits, hits[1:], strict=False)]
    assert all(gap >= 0.9 for gap in gaps), gaps

    receiver.reset(fail_first=6)
    failing = gap(token, record_content(unique("AC11 permanent")), ["retry"])
    failed = admin.delivered(failing, status="failed")[0]
    assert len(admin.get(f"/deliveries/{failed['id']}")["attempts"]) == 6
    receiver.reset()
    manual = admin.post(f"/deliveries/{failed['id']}/retry")
    assert manual["status"] == "delivered"
    assert manual["attempts"][-1]["trigger"] == "manual"
