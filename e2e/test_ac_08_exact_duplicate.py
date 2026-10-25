"""AC-08: differently capitalized questions keep both events in one group."""

from conftest import Admin, gap


def test_ac_08_case_variants_share_one_group(admin: Admin) -> None:
    project = admin.project()
    token = admin.token(project["id"])

    first = gap(token, "How do I reset my VPN?", ["it"])
    second = gap(token, "HOW DO I RESET MY VPN", ["it"])

    admin.processed(first)
    admin.processed(second)
    events = admin.get(f"/admin/feedback?project_id={project['id']}")["items"]
    assert {row["id"] for row in events} == {first, second}
    groups = admin.get(f"/admin/groups?project_id={project['id']}")["items"]
    assert len(groups) == 1 and groups[0]["occurrence_count"] == 2
    members = admin.group_events(groups[0]["id"])
    assert {row["id"] for row in members} == {first, second}
    assert {row["match"]["method"] for row in members} == {"initial", "exact"}
