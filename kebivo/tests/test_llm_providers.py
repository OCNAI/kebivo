"""Provider contract, secret boundaries and local fake-server transport."""

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from kebivo.llm.adapter import complete
from kebivo.models import AuditEvent, LlmProviderConfig
from kebivo.security.secretbox import SecretBox, SecretError
from kebivo.tests.test_db import _alembic_config
from kebivo.tests.test_projects import make_client, write_headers
from scripts.fake_llm_server import FakeLLMServer

CANARY = "llm-key-secret-canary"
HEADER_CANARY = "llm-header-secret-canary"


def payload(url: str, kind: str = "openai_compatible", mode: str | None = None) -> dict[str, Any]:
    return {
        "name": "Test provider",
        "provider_type": kind,
        "base_url": url,
        "model": "test-model",
        "deployment": "my-deployment" if kind == "azure_openai" else None,
        "api_version": "2025-01-01" if kind == "azure_openai" else None,
        "structured_output": mode,
        "allow_private_network": True,
        "api_key": CANARY if kind != "local" else None,
        "headers": {"X-Proxy-Key": HEADER_CANARY} if kind == "openai_compatible" else {},
    }


@pytest.fixture
def fake() -> Any:
    server = FakeLLMServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "kind,mode",
    [
        (kind, mode)
        for kind in ("openai_compatible", "azure_openai", "local")
        for mode in ("json_schema", "json_object", "prompt_only")
    ],
)
def test_adapter_wire_formats(tmp_path: Path, fake: FakeLLMServer, kind: str, mode: str) -> None:
    client, app = make_client(tmp_path / "wire.db")
    url = f"http://127.0.0.1:{fake.server_port}"
    created = client.post(
        "/api/v1/admin/llm-providers", json=payload(url, kind, mode), headers=write_headers(client)
    )
    assert created.status_code == 201, created.text
    with app.state.session_factory() as db:
        row = db.scalar(select(LlmProviderConfig))
        assert row is not None
        secrets = (
            SecretBox.from_settings(app.state.settings).decrypt(
                row.id, row.secrets_ciphertext, purpose="llm-provider-secrets"
            )
            if row.secrets_ciphertext
            else {}
        )
        result = asyncio.run(complete(row, secrets, "System", "User", {"c1", "c2", "c3"}))
    assert result.answer and result.answer["candidate"] == "c1"
    request = fake.requests[-1]
    body = json.loads(request["body"])
    assert body["temperature"] == 0 and body["max_tokens"] == 300
    assert ("response_format" in body) == (mode != "prompt_only")
    if mode != "prompt_only":
        assert body["response_format"]["type"] == mode
    if kind == "azure_openai":
        assert (
            request["path"]
            == "/openai/deployments/my-deployment/chat/completions?api-version=2025-01-01"
        )
        assert request["headers"]["api-key"] == CANARY
    else:
        assert request["path"] == "/chat/completions"
        assert ("Authorization" in request["headers"]) == (kind != "local")
    if kind == "openai_compatible":
        assert request["headers"]["X-Proxy-Key"] == HEADER_CANARY
    app.state.engine.dispose()


def test_crud_secrets_validation_and_test_results(tmp_path: Path, fake: FakeLLMServer) -> None:
    db_path = tmp_path / "provider.db"
    client, app = make_client(db_path)
    path = "/api/v1/admin/llm-providers"
    assert client.get(path).status_code == 200
    assert client.post(path, json=payload("https://example.com"), headers={}).status_code == 403
    with TestClient(app) as bearer_only:
        assert bearer_only.get(
            path, headers={"Authorization": "Bearer project-token"}
        ).status_code == 401
    headers = write_headers(client)
    created = client.post(
        path, json=payload(f"http://127.0.0.1:{fake.server_port}"), headers=headers
    )
    assert created.status_code == 201, created.text
    provider_id = created.json()["id"]
    for response in (created, client.get(path), client.get(f"{path}/{provider_id}")):
        assert response.headers["cache-control"] == "no-store"
        assert CANARY not in response.text and HEADER_CANARY not in response.text
    assert created.json()["api_key"] == {"configured": True, "source": "stored"}
    assert created.json()["headers"] == [{"name": "X-Proxy-Key", "configured": True}]
    with app.state.session_factory() as db:
        row = db.get(LlmProviderConfig, provider_id)
        assert row is not None and row.secrets_ciphertext
        ciphertext = row.secrets_ciphertext
        audit = "".join(e.details_json for e in db.scalars(select(AuditEvent)).all())
        assert CANARY not in audit and HEADER_CANARY not in audit
    raw = db_path.read_bytes()
    assert CANARY.encode() not in raw and HEADER_CANARY.encode() not in raw
    box = SecretBox.from_settings(app.state.settings)
    with pytest.raises(SecretError):
        box.decrypt(
            "00000000-0000-0000-0000-000000000000", ciphertext, purpose="llm-provider-secrets"
        )
    with pytest.raises(SecretError):
        box.decrypt(provider_id, ciphertext)
    assert box.decrypt(provider_id, ciphertext, purpose="llm-provider-secrets")["api_key"] == CANARY
    test = client.post(f"{path}/{provider_id}/test", headers=headers)
    assert test.status_code == 200 and test.json()["ok"] is True
    fake.scenario = "malformed"
    failed = client.post(f"{path}/{provider_id}/test", headers=headers).json()
    assert failed["ok"] is False and failed["error_category"] == "invalid_json"
    fake.scenario = "valid_c2"
    wrong = client.post(f"{path}/{provider_id}/test", headers=headers).json()
    assert wrong["ok"] is True and wrong["expected_answer"] is False and wrong["warning"], wrong
    for _ in range(3):
        client.post(f"{path}/{provider_id}/test", headers=headers)
    assert client.post(f"{path}/{provider_id}/test", headers=headers).status_code == 429
    for name in ("SECRET_KEY", "ENCRYPTION_KEY", "PATH", "LLM_API_KEY_bad"):
        assert (
            client.patch(
                f"{path}/{provider_id}", json={"api_key_env": name}, headers=headers
            ).status_code
            == 422
        )
    assert (
        client.patch(
            f"{path}/{provider_id}", json={"headers": {"Host": "evil"}}, headers=headers
        ).status_code
        == 422
    )
    patched = client.patch(
        f"{path}/{provider_id}",
        json={"api_key": None, "headers": {"X-Proxy-Key": None}, "api_key_env": "LLM_API_KEY_TEST"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["api_key"]["source"] == "env" and patched.json()["headers"] == []
    assert client.delete(f"{path}/{provider_id}", headers=headers).status_code == 204
    app.state.engine.dispose()


@pytest.mark.parametrize("change", ["base_url", "allow_private_network"])
def test_destination_change_clears_credentials(tmp_path: Path, change: str) -> None:
    client, app = make_client(tmp_path / "clear.db")
    path = "/api/v1/admin/llm-providers"
    initial_url = "https://example.com" if change == "allow_private_network" else "http://127.0.0.1:11434"
    created = client.post(
        path, json=payload(initial_url), headers=write_headers(client)
    )
    assert created.status_code == 201, created.text
    provider_id = created.json()["id"]
    changed = client.patch(
        f"{path}/{provider_id}",
        json={change: "http://127.0.0.1:1234" if change == "base_url" else False},
        headers=write_headers(client),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["api_key"] == {"configured": False, "source": "none"}
    assert changed.json()["headers"] == []
    with app.state.session_factory() as db:
        row = db.get(LlmProviderConfig, provider_id)
        assert row is not None and row.secrets_ciphertext is None
    reentered = client.patch(
        f"{path}/{provider_id}",
        json={"api_key": "new-key", "headers": {"X-Proxy-Key": "new-header"}},
        headers=write_headers(client),
    )
    assert reentered.status_code == 200, reentered.text
    assert reentered.json()["api_key"]["source"] == "stored"
    app.state.engine.dispose()


def test_delete_provider_referenced_by_archived_semantic_project(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "guard.db")
    headers = write_headers(client)
    provider = client.post(
        "/api/v1/admin/llm-providers",
        json=payload("http://127.0.0.1:11434"),
        headers=headers,
    )
    assert provider.status_code == 201, provider.text
    provider_id = provider.json()["id"]
    project = client.post(
        "/api/v1/projects", json={"name": "Research", "slug": "research"}, headers=headers
    ).json()
    changed = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"settings": {"deduplication": {
            "mode": "semantic", "semantic": {"provider_id": provider_id},
            "privacy_acknowledged": True,
        }}},
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    client.post(f"/api/v1/projects/{project['id']}/archive", headers=headers)
    deleted = client.delete(f"/api/v1/admin/llm-providers/{provider_id}", headers=headers)
    assert deleted.status_code == 409
    assert deleted.json()["error"]["code"] == "provider_in_use"
    app.state.engine.dispose()


def test_connection_test_respects_llm_capacity(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "busy.db")
    created = client.post(
        "/api/v1/admin/llm-providers",
        json=payload("https://example.com"),
        headers=write_headers(client),
    )
    assert created.status_code == 201, created.text
    app.state.llm_semaphore = asyncio.Semaphore(0)
    result = client.post(
        f"/api/v1/admin/llm-providers/{created.json()['id']}/test",
        headers=write_headers(client),
    )
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "llm_busy"
    app.state.engine.dispose()


def test_azure_model_optional_and_provider_name_limit(tmp_path: Path, fake: FakeLLMServer) -> None:
    client, app = make_client(tmp_path / "azure.db")
    path = "/api/v1/admin/llm-providers"
    headers = write_headers(client)
    data = payload(f"http://127.0.0.1:{fake.server_port}", "azure_openai")
    data.pop("model")
    data["name"] = "A" * 101
    assert client.post(path, json=data, headers=headers).status_code == 422
    data["name"] = "A" * 100
    created = client.post(path, json=data, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["model"] is None
    tested = client.post(f"{path}/{created.json()['id']}/test", headers=headers)
    assert tested.status_code == 200 and tested.json()["ok"] is True
    assert "model" not in json.loads(fake.requests[-1]["body"])
    app.state.engine.dispose()


@pytest.mark.parametrize(
    "scenario,category",
    [
        ("429", "http_429"),
        ("500", "http_5xx"),
        ("redirect", "http_4xx"),
        ("oversized", "too_large"),
        ("malformed", "invalid_json"),
    ],
)
def test_transport_errors(
    tmp_path: Path, fake: FakeLLMServer, scenario: str, category: str
) -> None:
    fake.scenario = scenario
    client, app = make_client(tmp_path / "error.db")
    response = client.post(
        "/api/v1/admin/llm-providers",
        json=payload(f"http://127.0.0.1:{fake.server_port}"),
        headers=write_headers(client),
    )
    result = client.post(
        f"/api/v1/admin/llm-providers/{response.json()['id']}/test", headers=write_headers(client)
    )
    assert result.json()["error_category"] == category
    assert len(fake.requests) == 1
    app.state.engine.dispose()


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://100.100.100.200/v1",
        "http://[fd00:ec2::254]/v1",
        "http://[fe80::1]/v1",
        "http://0.0.0.1/v1",
    ],
)
def test_blocked_metadata_at_configuration(tmp_path: Path, url: str) -> None:
    client, app = make_client(tmp_path / "ssrf.db")
    result = client.post(
        "/api/v1/admin/llm-providers", json=payload(url), headers=write_headers(client)
    )
    assert result.status_code == 422
    app.state.engine.dispose()


def test_migration_0009_to_0010(tmp_path: Path) -> None:
    path = tmp_path / "migration.db"
    config = _alembic_config(path)
    command.upgrade(config, "0009_exact_dedup")
    from kebivo.db import make_engine

    engine = make_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        count_before = connection.execute(text("SELECT COUNT(*) FROM projects")).scalar_one()
    engine.dispose()
    command.upgrade(config, "0010_llm_providers")
    engine = make_engine(f"sqlite:///{path}")
    assert "llm_provider_configs" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM projects")).scalar_one() == count_before
        )
    engine.dispose()
