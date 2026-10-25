"""AC-02: a default tag routes a knowledge gap to one formatted email."""

from email import message_from_string, policy
from typing import Any

from conftest import Admin, Mail, gap, record_content, unique, wait_for


def _parse(message: dict[str, Any]) -> Any:
    return message_from_string(message["data"], policy=policy.default)


def test_ac_02_default_tag_rule_sends_one_formatted_email(admin: Admin, mail: Mail) -> None:
    name = unique("AC02 Support")
    project = admin.project(name)
    admin.put(f"/projects/{project['id']}/default-tags", {"tags": ["customer-service"]})
    smtp = admin.smtp()
    admin.rule(project["id"], ["customer-service"], [smtp["id"]])
    token = admin.token(project["id"])
    question = record_content(unique("AC02 Where is the refund policy"))

    event_id = gap(token, question)

    delivery = admin.delivered(event_id)[0]
    assert delivery["destination_id"] == smtp["id"]
    messages = wait_for(lambda: mail.matching(question), what="email")
    assert len(messages) == 1
    email = _parse(messages[0])
    assert email["Subject"] == f"[Knowledge Gap] {name} — {question[:60]}"
    assert email["From"] == "kebivo@example.invalid"
    assert email["To"] == "team@example.invalid"
    assert email["Message-ID"]
    assert email["X-Feedback-Delivery-Id"] == delivery["id"]
    text = email.get_body(preferencelist=("plain",)).get_content()
    for field in (name, question, "customer-service"):
        assert field in text
    assert email.get_body(preferencelist=("html",)) is not None


def test_ac_02_smtp_outage_is_retried_then_delivered_once(admin: Admin, mail: Mail) -> None:
    project = admin.project()
    smtp = admin.smtp()
    admin.rule(project["id"], ["mail-outage"], [smtp["id"]])
    token = admin.token(project["id"])
    question = record_content(unique("AC02 Outage question"))
    mail.down(True)
    try:
        event_id = gap(token, question, ["mail-outage"])

        def first_failure() -> dict[str, Any] | None:
            rows = admin.deliveries(event_id)
            if not rows:
                return None
            detail = admin.get(f"/deliveries/{rows[0]['id']}")
            attempts = detail["attempts"]
            return detail if attempts and attempts[0]["result"] else None

        detail = wait_for(first_failure, interval=0.05, what="first SMTP attempt")
        assert detail["attempts"][0]["result"] == "retryable_failure", detail
    finally:
        mail.down(False)
    admin.delivered(event_id)
    assert len(mail.matching(question)) == 1
