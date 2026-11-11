import logging
from pathlib import Path

from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from kebivo.app import create_app
from kebivo.models import AdminUser, FeedbackEvent, Project
from kebivo.settings import Settings
from kebivo.tests.test_db import _alembic_config
from kebivo.tests.test_projects import ORIGIN, make_client, write_headers

PROMPT = (
    "If Kebivo is useful, a GitHub star helps other people find it: "
    "https://github.com/OCNAI/kebivo"
)
PATH = "/api/v1/admin/star-prompt"


def test_real_report_and_persistent_dismissal(tmp_path: Path) -> None:
    path = tmp_path / "star.db"
    client, app = make_client(path)
    assert client.get(PATH).json() == {"show": False}
    assert TestClient(app, base_url=ORIGIN).get(PATH).status_code == 401
    assert client.post(f"{PATH}/dismiss", headers={"Origin": ORIGIN}).status_code == 403
    with app.state.session_factory() as db:
        sample = Project(name="Sample", slug="sample", is_sample=True)
        db.add(sample)
        db.flush()
        db.add(
            FeedbackEvent(
                project_id=sample.id,
                submitted_via="rest",
                event_type="knowledge_gap",
                question="Sample question?",
                normalized_question_hash="a" * 64,
            )
        )
        db.commit()
    assert client.get(PATH).json() == {"show": False}
    with app.state.session_factory() as db:
        db.execute(delete(FeedbackEvent).where(FeedbackEvent.project_id == sample.id))
        real = Project(name="Real", slug="real")
        db.add(real)
        db.flush()
        db.add(
            FeedbackEvent(
                project_id=real.id,
                submitted_via="rest",
                event_type="knowledge_gap",
                question="Real question?",
                normalized_question_hash="b" * 64,
            )
        )
        db.commit()
    assert client.get(PATH).json() == {"show": True}
    with app.state.session_factory() as db:
        db.add(
            FeedbackEvent(
                project_id=sample.id,
                submitted_via="rest",
                event_type="knowledge_gap",
                question="Another sample question?",
                normalized_question_hash="c" * 64,
            )
        )
        db.commit()
    assert client.get(PATH).json() == {"show": True}
    headers = write_headers(client)
    assert client.post(f"{PATH}/dismiss", headers=headers).status_code == 204
    assert client.post(f"{PATH}/dismiss", headers=headers).status_code == 204
    assert client.get(PATH).json() == {"show": False}
    with app.state.session_factory() as db:
        dismissed_at = db.scalar(select(AdminUser.star_prompt_dismissed_at))
        assert dismissed_at is not None
    client.close()
    app.state.engine.dispose()

    # A new app and login against the same database still see the dismissal.
    restarted, restarted_app = make_client(path)
    assert restarted.get(PATH).json() == {"show": False}
    with restarted_app.state.session_factory() as db:
        assert db.scalar(select(AdminUser.star_prompt_dismissed_at)) == dismissed_at
    restarted.close()
    restarted_app.state.engine.dispose()


def test_startup_log_skips_after_any_admin_dismisses(tmp_path: Path) -> None:
    path = tmp_path / "log.db"
    client, app = make_client(path)
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("kebivo.request")
    handler = Capture()
    logger.addHandler(handler)
    try:
        with client:
            assert [record.msg for record in records].count(PROMPT) == 1
            assert client.post(f"{PATH}/dismiss", headers=write_headers(client)).status_code == 204
        app.state.engine.dispose()
        records.clear()
        settings = Settings(
            "Kebivo", "test", "project-tests-secret-key-000000000000", str(path),
            allowed_origins=(ORIGIN,), worker_enabled=False,
        )
        restarted = create_app(settings)
        with TestClient(restarted, base_url=ORIGIN):
            assert PROMPT not in [record.msg for record in records]
    finally:
        logger.removeHandler(handler)


def test_dismissal_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    from sqlalchemy import inspect

    path = tmp_path / "migration.db"
    config = _alembic_config(path)
    command.upgrade(config, "0017_sample_data")
    from kebivo.db import make_engine

    engine = make_engine(f"sqlite:///{path}")
    assert "star_prompt_dismissed_at" not in {
        column["name"] for column in inspect(engine).get_columns("admin_users")
    }
    engine.dispose()
    command.upgrade(config, "0018_star_prompt_dismissal")
    engine = make_engine(f"sqlite:///{path}")
    columns = {column["name"]: column for column in inspect(engine).get_columns("admin_users")}
    assert columns["star_prompt_dismissed_at"]["nullable"]
    engine.dispose()
    command.downgrade(config, "0017_sample_data")
    engine = make_engine(f"sqlite:///{path}")
    assert "star_prompt_dismissed_at" not in {
        column["name"] for column in inspect(engine).get_columns("admin_users")
    }
    engine.dispose()
