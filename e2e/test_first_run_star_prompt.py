"""First-run star prompt persistence across an app container restart."""

import httpx
import pytest
from conftest import API, Admin


@pytest.mark.stage("first_run")
def test_star_prompt_is_absent_on_fresh_and_sample_only_install(admin: Admin) -> None:
    assert admin.get("/admin/star-prompt") == {"show": False}
    assert admin.request("POST", "/admin/sample-data", {}).status_code == 201
    assert admin.get("/admin/star-prompt") == {"show": False}
    assert admin.request("DELETE", "/admin/sample-data").status_code == 204
    assert admin.get("/admin/star-prompt") == {"show": False}


@pytest.mark.stage("before_restart")
def test_star_prompt_requires_admin_and_dismissal_is_durable(admin: Admin) -> None:
    # Earlier main-stage tests have created real (non-sample) reports.
    assert httpx.get(f"{API}/admin/star-prompt", timeout=5).status_code == 401
    assert admin.get("/admin/star-prompt") == {"show": True}

    response = admin.request("POST", "/admin/star-prompt/dismiss")
    assert response.status_code == 204, response.text
    assert admin.get("/admin/star-prompt") == {"show": False}


@pytest.mark.stage("after_restart")
def test_star_prompt_dismissal_survives_container_restart(admin: Admin) -> None:
    assert admin.get("/admin/star-prompt") == {"show": False}
