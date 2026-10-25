"""AC-07: an enabled, scoped correction is stored unverified; nothing else changes."""

from conftest import (
    Admin,
    FakeLlm,
    Mail,
    Receiver,
    mcp_session,
    record_content,
    run,
    submit,
    unique,
)


def test_ac_07_unverified_correction_reaches_only_configured_destination(
    admin: Admin, receiver: Receiver, mail: Mail, llm: FakeLlm
) -> None:
    project = admin.project()
    admin.patch(f"/projects/{project['id']}", {"settings": {
        "corrections": {"enabled": True}, "deduplication": {"mode": "exact"}}})
    admin.put(f"/projects/{project['id']}/default-tags", {"tags": ["kb"]})
    path = unique("ac07")
    hook = admin.webhook(path)
    admin.rule(project["id"], ["kb"], [hook["id"]], event_types=["knowledge_correction"])
    token = admin.token(project["id"], ["feedback:gap", "feedback:correction"])
    question = record_content(unique("AC07 What is the guest Wi-Fi password policy"))
    answer = record_content(unique("AC07 Guests get a daily code from reception"))
    llm_calls, mails = len(llm.requests()), len(mail.messages())

    async def scenario() -> dict[str, object]:
        async with mcp_session(token) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names == {"report_knowledge_gap", "submit_knowledge_correction"}
            result = await session.call_tool(
                "submit_knowledge_correction", {"question": question, "correction": answer}
            )
            assert not result.is_error, result
            return dict(result.structured_content or {})

    via_mcp = run(scenario())
    assert via_mcp["verification_status"] == "unverified"
    via_rest = submit(token, {"event_type": "knowledge_correction", "question": question + " REST",
                              "correction": answer})
    assert via_rest.status_code == 202, via_rest.text

    for event_id in (str(via_mcp["event_id"]), via_rest.json()["event_id"]):
        admin.delivered(event_id)
        detail = admin.event(event_id)
        assert detail["event_type"] == "knowledge_correction"
        assert detail["agent_reported"]["correction"] == answer
    # The only outbound calls were the two configured deliveries.
    assert len(receiver.hits(path)) == 2
    assert len(mail.messages()) == mails
    assert len(llm.requests()) == llm_calls
