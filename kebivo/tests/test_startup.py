import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from kebivo.settings import Settings, SettingsError
from kebivo.startup import (
    StartupError,
    acquire_instance_lock,
    current_revision,
    prepare_database,
    restrict_database_files,
)

BEFORE_HEAD = "0018_star_prompt_dismissal"
HEAD = "0019_weekly_digest"

BAD_MIGRATION = """
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
"""


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
        return db.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()


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


def test_newer_database_keeps_the_existing_backup(migrations: tuple[Path, Config]) -> None:
    database, config = migrations
    command.upgrade(config, HEAD)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE alembic_version SET version_num = '0099_from_a_newer_release'")
    kept = database.with_name(f"pre-upgrade-{BEFORE_HEAD}.sqlite3")
    kept.write_bytes(b"backup taken before the newer release")

    with pytest.raises(Exception, match="0099_from_a_newer_release"):
        prepare_database(database, config)

    assert sorted(database.parent.glob("pre-upgrade-*.sqlite3")) == [kept]


def test_failed_backup_stops_before_migrating(
    migrations: tuple[Path, Config], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database, config = migrations
    command.upgrade(config, BEFORE_HEAD)
    # The backup subprocess runs sys.executable; make it exit 1.
    monkeypatch.setattr(sys, "executable", shutil.which("false"))

    with pytest.raises(StartupError, match="Pre-upgrade backup failed, not migrating"):
        prepare_database(database, config)

    assert current_revision(database) == BEFORE_HEAD
    assert sorted(path.name for path in database.parent.iterdir()) == ["app.sqlite3"]


def test_database_path_override_is_the_file_backed_up_and_migrated(
    migrations: tuple[Path, Config], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database, config = migrations
    command.upgrade(config, BEFORE_HEAD)
    other = tmp_path / "other.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{other}")
    monkeypatch.setenv("DATABASE_PATH", str(database))
    with pytest.raises(SettingsError, match="name different files"):
        Settings.from_env()

    # DATABASE_PATH alone: every part of startup and the app uses that file.
    monkeypatch.delenv("DATABASE_URL")
    settings = Settings.from_env()
    assert settings.database_url == f"sqlite:///{database}"
    # Even a caller that passes a different DATABASE_URL gets the checked file migrated.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{other}")
    backup = prepare_database(Path(settings.database_path), config)

    assert current_revision(database) == HEAD
    with sqlite3.connect(database) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(destinations)")}
    assert "digest_weekday" in columns
    assert backup == database.with_name(f"pre-upgrade-{BEFORE_HEAD}.sqlite3")
    assert current_revision(backup) == BEFORE_HEAD
    assert not other.exists()


def test_matching_database_settings_are_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.sqlite3'}?timeout=5")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "." / "app.sqlite3"))
    assert Settings.from_env().database_path == str(tmp_path / "." / "app.sqlite3")
    monkeypatch.delenv("DATABASE_PATH")
    Settings.from_env()


def test_startup_rejects_divergence_then_serves_the_upgraded_database(
    migrations: tuple[Path, Config], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database, config = migrations
    other = tmp_path / "other.sqlite3"
    command.upgrade(config, BEFORE_HEAD)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{other}")
    command.upgrade(config, BEFORE_HEAD)

    environment = {**os.environ, "DATABASE_PATH": str(database), "APP_ENV": "test"}
    command_line = [
        sys.executable,
        "-m",
        "kebivo.startup",
        sys.executable,
        "-c",
        "from kebivo.app import create_app; print(create_app().state.engine.url.database)",
    ]
    rejected = subprocess.run(command_line, env=environment, capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "name different files" in rejected.stderr
    assert current_revision(database) == current_revision(other) == BEFORE_HEAD
    assert list(database.parent.glob("pre-upgrade-*.sqlite3")) == []

    environment.pop("DATABASE_URL")
    served = subprocess.run(
        command_line, env=environment, capture_output=True, text=True, check=True
    )
    assert Path(served.stdout.splitlines()[-1]) == database
    assert current_revision(database) == HEAD
    assert current_revision(other) == BEFORE_HEAD
    with sqlite3.connect(database) as db:
        assert "digest_weekday" in {row[1] for row in db.execute("PRAGMA table_info(destinations)")}
    backup = database.with_name(f"pre-upgrade-{BEFORE_HEAD}.sqlite3")
    assert current_revision(backup) == BEFORE_HEAD
    with sqlite3.connect(backup) as db:
        assert "digest_weekday" not in {
            row[1] for row in db.execute("PRAGMA table_info(destinations)")
        }


def test_directory_fsync_failure_stops_before_pruning_or_migrating(
    migrations: tuple[Path, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, config = migrations
    command.upgrade(config, BEFORE_HEAD)
    stale = database.with_name("pre-upgrade-0017_sample_data.sqlite3")
    stale.write_bytes(b"old backup")
    fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory fsync failed")
        fsync(descriptor)

    monkeypatch.setattr("kebivo.startup.os.fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="directory fsync failed"):
        prepare_database(database, config)
    assert current_revision(database) == BEFORE_HEAD
    assert stale.exists()


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
