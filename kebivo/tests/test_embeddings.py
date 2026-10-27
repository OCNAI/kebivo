"""Embedding storage and provider boundaries."""

import asyncio
import math
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text

from kebivo.db import make_engine
from kebivo.dedup.embeddings import cosine, decode, encode, load, normalize, store
from kebivo.llm.adapter import embed, validate_embeddings
from kebivo.models import LlmProviderConfig, QuestionEmbedding
from kebivo.retention import run
from kebivo.tests.test_db import _alembic_config
from kebivo.tests.test_projects import write_headers
from kebivo.tests.test_retention import seed, setup
from scripts.fake_llm_server import FakeLLMServer


def test_codec_and_validation() -> None:
    assert decode(encode([3, 4])) == pytest.approx([0.6, 0.8])
    assert cosine(normalize([3, 4]), normalize([6, 8])) == pytest.approx(1)
    with pytest.raises(ValueError):
        encode([0, 0])
    with pytest.raises(ValueError):
        decode(b"bad")
    data = {
        "data": [
            {"index": 1, "embedding": [0, 1], "extra": True},
            {"index": 0, "embedding": [1, 0]},
        ],
        "extra": 1,
    }
    assert validate_embeddings(data, 2, 2) == [[1, 0], [0, 1]]
    invalid: list[dict[str, object]] = [
        {"data": []},
        {"data": [{"embedding": [1]}]},
        {"data": [{"embedding": [math.nan, 0]}]},
        {"data": [{"embedding": [math.inf, 0]}]},
        {"data": [{"index": 2, "embedding": [1, 0]}]},
    ]
    for bad in invalid:
        assert validate_embeddings(bad, 1, 2) is None
    assert validate_embeddings({"data": [{"embedding": [0] * 8193}]}, 1, 8193) is None


def test_adapter_and_fake_embeddings() -> None:
    server = FakeLLMServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = LlmProviderConfig(
        provider_type="local",
        base_url=f"http://127.0.0.1:{server.server_port}",
        headers_json="[]",
        timeout_seconds=2,
        allow_private_network=True,
    )
    try:
        result = asyncio.run(
            embed(provider, {}, "fake-64", ["reset password", "forgot passcode"], 64)
        )
        assert result.vectors is not None
        assert cosine(result.vectors[0], result.vectors[1]) > 0.9
        assert server.requests[-1]["path"] == "/embeddings"
        assert '"model":"fake-64"' in server.requests[-1]["body"]
        vpn = asyncio.run(embed(provider, {}, "fake-64", ["VPN", "VPS"], 64))
        assert vpn.vectors is not None
        assert cosine(*vpn.vectors) < 0.1
        provider.provider_type = "azure_openai"
        with pytest.raises(ValueError, match="unsupported"):
            asyncio.run(embed(provider, {}, "fake-64", ["a"], 64))
        provider.provider_type = "local"
        server.scenario = "oversized"
        assert (
            asyncio.run(embed(provider, {}, "fake-64", ["a"], 64)).error_category
            == "invalid_response"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_cascade_retention_and_model_filter(tmp_path: Path) -> None:
    client, app, project, _ = setup(tmp_path)
    old = datetime(2025, 1, 1, tzinfo=UTC)
    manual, automatic = (
        seed(app, project["id"], old, "manual"),
        seed(app, project["id"], old, "automatic"),
    )
    with app.state.session_factory() as db:
        for event_id, model in ((manual, "old"), (automatic, "new")):
            store(
                db,
                event_id=event_id,
                project_id=project["id"],
                event_type="knowledge_gap",
                provider_id=None,
                model=model,
                values=[3, 4],
            )
        db.commit()
        assert [
            row.event_id
            for row in load(
                db, project_id=project["id"], event_type="knowledge_gap", model="new", dims=2
            )
        ] == [automatic]
        assert (
            load(db, project_id=project["id"], event_type="knowledge_gap", model="new", dims=3)
            == []
        )
    with app.state.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    assert (
        client.delete(f"/api/v1/admin/feedback/{manual}", headers=write_headers(client)).status_code
        == 200
    )
    with app.state.session_factory() as db:
        assert db.get(QuestionEmbedding, manual) is None
        assert db.get(QuestionEmbedding, automatic) is not None
    assert (
        run(app.state.session_factory, app.state.settings, datetime(2026, 1, 1, tzinfo=UTC))[
            "feedback"
        ]
        == 1
    )
    with app.state.session_factory() as db:
        assert db.get(QuestionEmbedding, automatic) is None


def test_migration_populated_0013_upgrade_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "migration.db"
    config = _alembic_config(path)
    command.upgrade(config, "0013_slack_destination")
    engine = make_engine(f"sqlite:///{path}")
    stamp = "2025-01-01T00:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects(id,name,slug,created_at,updated_at) "
                "VALUES ('p','Existing','existing',:stamp,:stamp)"
            ),
            {"stamp": stamp},
        )
        connection.execute(
            text(
                "INSERT INTO feedback_events(id,project_id,submitted_via,event_type,question,"
                "normalized_question_hash,created_at,updated_at) "
                "VALUES ('e','p','rest','knowledge_gap','preserved',:hash,:stamp,:stamp)"
            ),
            {"hash": "a" * 64, "stamp": stamp},
        )
    engine.dispose()
    command.upgrade(config, "0014_question_embeddings")
    engine = make_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO question_embeddings(event_id,project_id,event_type,model,dims,"
                "vector,created_at) VALUES ('e','p','knowledge_gap','fake',2,:vector,:stamp)"
            ),
            {"vector": encode([3, 4]), "stamp": stamp},
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()
    command.downgrade(config, "0013_slack_destination")
    engine = make_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT question FROM feedback_events WHERE id='e'"
            ).scalar_one()
            == "preserved"
        )
        assert "question_embeddings" not in inspect(connection).get_table_names()
        assert "method" not in {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info('dedup_evaluations')")
        }
    engine.dispose()
