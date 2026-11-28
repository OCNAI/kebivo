import os
import shutil
import sqlite3
import stat
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from kebivo.startup import (
    StartupError,
    acquire_instance_lock,
    current_revision,
    prepare_database,
    restrict_database_files,
)

BEFORE_HEAD = "0018_star_prompt_dismissal"
HEAD = "0019_weekly_digest"

BAD_MIGRATION = '''
import sqlalchemy as sa
from alembic import op

revision = "0020_broken"
down_revision = "0019_weekly_digest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("broken_table", sa.Column("id", sa.Integer, primary_key=True))
    op.add_column("projects", sa.Column("broken_column", sa.Integer))
    raise RuntimeError("broken migration")


def downgrade() -> None:
    pass
'''


@pytest.fixture
def migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Config]:
    database = tmp_path / "data" / "app.sqlite3"
    database.parent.mkdir()
    # The backup subprocess and migrations/env.py both read DATABASE_URL.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    scripts = tmp_path / "migrations"
    shutil.copytree("migrations", scripts, ignore=shutil.ignore_patterns("__pycache__"))
    config = Config("alembic.ini")
    config.set_main_option("script_location", str(scripts))
    return database, config


def _schema(database: Path) -> list[tuple[str, str, str | None]]:
    with sqlite3.connect(database) as db:
        return db.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()


def test_failed_migration_rolls_back_the_whole_run(
    migrations: tuple[Path, Config],
) -> None:
    database, config = migrations
    command.upgrade(config, BEFORE_HEAD)
    before = _schema(database)
    bad = Path(config.get_main_option("script_location") or "") / "versions" / "0020_broken.py"
    bad.write_text(BAD_MIGRATION)

    with pytest.raises(RuntimeError, match="broken migration"):
        prepare_database(database, config)

    # 0019 and the broken migration both ran in one transaction: nothing stuck.
    assert current_revision(database) == BEFORE_HEAD
    assert _schema(database) == before
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    bad.unlink()
    prepare_database(database, config)
    assert current_revision(database) == HEAD
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_keeps_only_the_latest_checked_backup(
    migrations: tuple[Path, Config],
) -> None:
    database, config = migrations
    command.upgrade(config, BEFORE_HEAD)
    stale = database.with_name("pre-upgrade-0017_sample_data.sqlite3")
    stale.write_bytes(b"old backup")

    backup = prepare_database(database, config)

    assert backup == database.with_name(f"pre-upgrade-{BEFORE_HEAD}.sqlite3")
    assert sorted(database.parent.glob("pre-upgrade-*.sqlite3")) == [backup]
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert current_revision(backup) == BEFORE_HEAD
    assert current_revision(database) == HEAD
    # Already at head: no new backup, the existing one stays.
    assert prepare_database(database, config) is None
    assert sorted(database.parent.glob("pre-upgrade-*.sqlite3")) == [backup]


def test_fresh_database_is_not_backed_up(migrations: tuple[Path, Config]) -> None:
    database, config = migrations
    assert prepare_database(database, config) is None
    assert current_revision(database) == HEAD
    assert list(database.parent.glob("pre-upgrade-*")) == []


def test_failed_backup_stops_before_migrating(
    migrations: tuple[Path, Config], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database, config = migrations
    command.upgrade(config, BEFORE_HEAD)
    # The backup subprocess reads this path and finds no database there.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'missing.sqlite3'}")

    with pytest.raises(StartupError, match="Pre-upgrade backup failed, not migrating"):
        prepare_database(database, config)

    assert current_revision(database) == BEFORE_HEAD
    assert sorted(path.name for path in database.parent.iterdir()) == ["app.sqlite3"]


def test_second_lock_on_the_same_directory_fails(tmp_path: Path) -> None:
    first = acquire_instance_lock(tmp_path)
    try:
        with pytest.raises(StartupError) as caught:
            acquire_instance_lock(tmp_path)
        assert str(caught.value) == f"Another Kebivo is already using {tmp_path}; stop it first"
        assert stat.S_IMODE((tmp_path / ".kebivo.lock").stat().st_mode) == 0o600
    finally:
        os.close(first)
    os.close(acquire_instance_lock(tmp_path))


def test_existing_database_files_become_private(tmp_path: Path) -> None:
    database = tmp_path / "app.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        path = tmp_path / f"app.sqlite3{suffix}"
        path.write_bytes(b"")
        path.chmod(0o644)
    restrict_database_files(database)
    for suffix in ("", "-wal", "-shm"):
        assert stat.S_IMODE((tmp_path / f"app.sqlite3{suffix}").stat().st_mode) == 0o600
