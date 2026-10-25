"""AC-04: one event matching two rules reaches two distinct destinations."""

from conftest import Admin, Mail, Receiver, gap, record_content, unique, wait_for


def test_ac_04_two_rules_deliver_to_webhook_and_email_once_each(
    admin: Admin, receiver: Receiver, mail: Mail
) -> None:
    project = admin.project()
    path = unique("ac04")
    hook = admin.webhook(path)
    smtp = admin.smtp()
    admin.rule(project["id"], ["multi"], [hook["id"]], priority=1, behavior="continue")
    admin.rule(project["id"], ["multi"], [smtp["id"]], priority=2, behavior="continue")
    token = admin.token(project["id"])
    question = record_content(unique("AC04 Printer queue stuck"))

    event_id = gap(token, question, ["multi"])

    rows = admin.delivered(event_id, count=2)
    assert {row["destination_id"] for row in rows} == {hook["id"], smtp["id"]}
    wait_for(lambda: mail.matching(question), what="email")
    assert len(receiver.hits(path)) == 1
    assert len(mail.matching(question)) == 1
