"""AC-06: with corrections off the MCP tool is hidden and direct calls fail."""

import pytest
from conftest import Admin, mcp_session, run, submit
from mcp.shared.exceptions import MCPError


def test_ac_06_correction_tool_hidden_and_rejected_with_zero_rows(admin: Admin) -> None:
    project = admin.project()
    assert project["settings"]["corrections"] == {"enabled": False}
    token = admin.token(project["id"], ["feedback:gap", "feedback:correction"])

    async def scenario() -> list[str]:
        async with mcp_session(token) as session:
            names = [tool.name for tool in (await session.list_tools()).tools]
            with pytest.raises(MCPError) as rejected:
                await session.call_tool(
                    "submit_knowledge_correction", {"question": "q", "correction": "c"}
                )
            assert rejected.value.code == -32602 and "Unknown tool" in str(rejected.value)
            return names

    assert run(scenario()) == ["report_knowledge_gap"]
    rest = submit(token, {"event_type": "knowledge_correction", "question": "q", "correction": "c"})
    assert rest.status_code == 403, rest.text
    assert admin.get(f"/admin/feedback?project_id={project['id']}")["items"] == []
    assert admin.get(f"/admin/groups?project_id={project['id']}")["items"] == []
