"""Container start: lock the data directory, back up before an upgrade, migrate, run the app.

The lock is an exclusive ``flock`` on ``<data dir>/.kebivo.lock``. Its descriptor is
inherited by the app process through ``exec``, so it is held until the app exits.
"""

import fcntl
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from kebivo.settings import Settings

LOCK_NAME = ".kebivo.lock"
BACKUP_PREFIX = "pre-upgrade-"
BACKUP_SUFFIX = ".sqlite3"
DATABASE_FILE_SUFFIXES = ("", "-wal", "-shm")


class StartupError(RuntimeError):
    pass


def acquire_instance_lock(directory: Path) -> int:
    """Return a descriptor holding the data-directory lock, or fail if another holds it."""
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise StartupError(
            f"Another Kebivo is already using {directory}; stop it first"
        ) from None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def restrict_database_files(database: Path) -> None:
    """Make database files created before umask 077 private to the app user."""
    for suffix in DATABASE_FILE_SUFFIXES:
        path = database.with_name(database.name + suffix)
        try:
            if path.is_file() and path.stat().st_mode & 0o077:
                path.chmod(0o600)
        except OSError:
            # Not ours to change (for example a bind mount owned by another user).
            pass


def current_revision(database: Path) -> str | None:
    """Return the database's Alembic revision, or None for a missing or empty database."""
    if not database.is_file():
        return None
    uri = f"file:{quote(str(database.resolve()))}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        try:
            row = db.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
    return None if row is None else str(row[0])


def backup_before_upgrade(database: Path, revision: str) -> Path:
    """Write a checked backup next to the database and remove older pre-upgrade backups."""
    target = database.with_name(f"{BACKUP_PREFIX}{revision}{BACKUP_SUFFIX}")
    descriptor, name = tempfile.mkstemp(
        prefix=".pre-upgrade-", suffix=".tmp", dir=database.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            result = subprocess.run(
                [sys.executable, "-m", "scripts.sqlite_maintenance", "backup"],
                stdout=output,
                stderr=subprocess.PIPE,
                text=False,
                check=False,
            )
            output.flush()
            os.fsync(output.fileno())
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip() or f"exit {result.returncode}"
            raise StartupError(f"Pre-upgrade backup failed, not migrating: {detail}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    for old in database.parent.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"):
        if old != target:
            old.unlink(missing_ok=True)
    return target


def prepare_database(database: Path, config: Config) -> Path | None:
    """Back up a database that is behind head, then migrate. Return the backup path."""
    revision = current_revision(database)
    scripts = ScriptDirectory.from_config(config)
    head = scripts.get_current_head()
    known = {script.revision for script in scripts.walk_revisions()}
    backup = None
    # A revision this image does not know is from a newer release: Alembic refuses it
    # below, and backing it up would delete the copy taken before that upgrade.
    if revision in known and revision != head:
        backup = backup_before_upgrade(database, revision)
        print(f"Kebivo: backed up the database before upgrading to {backup}", flush=True)
    command.upgrade(config, "head")
    return backup


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("Usage: python -m kebivo.startup <command> [args...]")
    database = Path(Settings.from_env().database_path).resolve()
    try:
        lock = acquire_instance_lock(database.parent)
        restrict_database_files(database)
        prepare_database(database, Config("alembic.ini"))
    except StartupError as exc:
        raise SystemExit(str(exc)) from None
    os.set_inheritable(lock, True)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
