"""AC-05: an unmatched event stays in the inbox and uses the fallback if set."""

from conftest import Admin, Receiver, gap, record_content, unique


def test_ac_05_fallback_destination_and_inbox_only(admin: Admin, receiver: Receiver) -> None:
    path = unique("ac05")
    fallback = admin.webhook(path)
    project = admin.project()
    admin.rule(project["id"], ["matched-only"], [fallback["id"]])
    admin.patch(f"/projects/{project['id']}", {"fallback_destination_id": fallback["id"]})
    token = admin.token(project["id"])

    with_fallback = gap(token, record_content(unique("AC05 fallback")), ["no-rule"])
    rows = admin.delivered(with_fallback)
    assert rows[0]["destination_id"] == fallback["id"]
    assert len(receiver.hits(path)) == 1

    admin.patch(f"/projects/{project['id']}", {"fallback_destination_id": None})
    inbox_only = gap(token, record_content(unique("AC05 inbox only")), ["no-rule"])
    detail = admin.processed(inbox_only)
    assert detail["routing"]["deliveries"] == []
    assert admin.deliveries(inbox_only) == []
    rows = admin.get(f"/admin/feedback?project_id={project['id']}")["items"]
    listed = {row["id"] for row in rows}
    assert listed == {with_fallback, inbox_only}
    assert len(receiver.hits(path)) == 1
