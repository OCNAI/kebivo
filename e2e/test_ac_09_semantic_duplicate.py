"""AC-09: semantic matching groups reworded questions when the policy is met."""

from conftest import Admin, FakeLlm, gap
from llm_support import semantic_project


def test_ac_09_semantic_match_above_threshold_and_separate_below(
    admin: Admin, llm: FakeLlm
) -> None:
    project, token = semantic_project(admin)
    try:
        llm.scenario("valid")
        first = gap(token, "My VPN password reset link has expired", ["it"])
        admin.processed(first)
        matched = gap(token, "The VPN password reset link expired", ["it"])
        admin.processed(matched)
        llm.scenario("low_confidence")
        separate = gap(token, "VPN password reset link expired again", ["it"])
        admin.processed(separate)
    finally:
        llm.scenario("valid")

    group = admin.group_of(first)
    assert admin.group_of(matched)["id"] == group["id"]
    members = {row["id"]: row for row in admin.group_events(group["id"])}
    assert set(members) == {first, matched}
    assert members[matched]["match"]["method"] == "semantic"
    assert members[matched]["semantic"]["outcome"] == "matched"
    alone = admin.group_of(separate)
    assert alone["id"] != group["id"] and alone["occurrence_count"] == 1
    assert admin.group_events(alone["id"])[0]["semantic"]["outcome"] == "low_confidence"
    sent = [call for call in llm.requests() if "My VPN password reset link" in call["body"]]
    assert sent and all(call["headers"].get("X-E2E-Canary") for call in sent)
