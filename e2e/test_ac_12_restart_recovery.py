"""AC-12: pending and in-flight deliveries survive a restart and a SIGKILL.

The host script (scripts/e2e.sh) runs the stages in order and restarts the
app container between them: before_restart -> `docker compose restart app` ->
after_restart -> before_kill -> `docker compose kill -s KILL app` + `up` ->
after_kill. The receiver holds requests during the outage, so some deliveries
are in flight and others still wait in the queue when the app goes away.
"""

from collections import Counter

import pytest
from conftest import Admin, Receiver, gap, load_state, record_content, save_state, unique, wait_for


def _queue(admin: Admin, receiver: Receiver, name: str) -> None:
    project = admin.project()
    path = unique(name)
    hook = admin.webhook(path)
    admin.rule(project["id"], ["restart"], [hook["id"]])
    token = admin.token(project["id"])
    receiver.reset(hold_seconds=60)
    events = [gap(token, record_content(unique(f"{name} {index}")), ["restart"])
              for index in range(6)]
    wait_for(lambda: receiver.hits(path), what="a delivery in flight")
    save_state(name, {"path": path, "events": events})


def _recovered(admin: Admin, receiver: Receiver, name: str) -> list[dict[str, object]]:
    state = load_state(name)
    receiver.reset()
    details = []
    for event_id in state["events"]:
        delivery = admin.delivered(event_id, timeout=90)[0]
        details.append(admin.get(f"/deliveries/{delivery['id']}"))
    ids = Counter(hit["headers"]["webhook-id"] for hit in receiver.hits(state["path"]))
    assert set(ids) == {detail["id"] for detail in details}
    assert all(count == 1 for count in ids.values()), ids
    return details


@pytest.mark.stage("before_restart")
def test_ac_12_queue_before_restart(admin: Admin, receiver: Receiver) -> None:
    _queue(admin, receiver, "ac12-restart")


@pytest.mark.stage("after_restart")
def test_ac_12_delivered_once_after_restart(admin: Admin, receiver: Receiver) -> None:
    _recovered(admin, receiver, "ac12-restart")


@pytest.mark.stage("before_kill")
def test_ac_12_queue_before_kill(admin: Admin, receiver: Receiver) -> None:
    _queue(admin, receiver, "ac12-kill")


@pytest.mark.stage("after_kill")
def test_ac_12_delivered_once_after_kill(admin: Admin, receiver: Receiver) -> None:
    details = _recovered(admin, receiver, "ac12-kill")
    results = [attempt["result"] for detail in details for attempt in detail["attempts"]]
    assert "interrupted" in results, results
