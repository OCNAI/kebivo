"""AC-13: filter open `it` feedback, resolve a group, and the filter follows.

The `main` stage sets up and resolves; the `after_restart` stage checks that
the status and filters survive an app restart.
"""

import pytest
from conftest import Admin, gap, load_state, save_state, unique


def _ids(admin: Admin, project_id: str, query: str) -> set[str]:
    rows = admin.get(f"/admin/groups?project_id={project_id}&{query}")["items"]
    return {row["id"] for row in rows}


def test_ac_13_resolve_group_and_filter_reflects_it(admin: Admin) -> None:
    project = admin.project()
    token = admin.token(project["id"])
    target = gap(token, unique("AC13 VPN drops every hour"), ["it"])
    other = gap(token, unique("AC13 Second monitor flickers"), ["it"])
    hr = gap(token, unique("AC13 Where is the holiday calendar"), ["hr"])
    for event_id in (target, other, hr):
        admin.processed(event_id)
    target_group = admin.event(target)["group"]["id"]
    other_group = admin.event(other)["group"]["id"]
    assert _ids(admin, project["id"], "status=open&tag=it") == {target_group, other_group}

    admin.patch(f"/admin/groups/{target_group}", {"status": "resolved"})

    assert admin.get(f"/admin/groups/{target_group}")["status"] == "resolved"
    assert _ids(admin, project["id"], "status=open&tag=it") == {other_group}
    assert _ids(admin, project["id"], "status=resolved&tag=it") == {target_group}
    save_state("ac13", {"project": project["id"], "resolved": target_group, "open": other_group})


@pytest.mark.stage("after_restart")
def test_ac_13_status_and_filters_survive_restart(admin: Admin) -> None:
    state = load_state("ac13")
    assert _ids(admin, state["project"], "status=open&tag=it") == {state["open"]}
    assert _ids(admin, state["project"], "status=resolved&tag=it") == {state["resolved"]}
