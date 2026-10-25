"""Stream a consistent SQLite backup or restore one while the app is stopped.

Backup uses SQLite's online backup API, so it can run while the app serves requests.
Restore must run in a one-off container after the application container is stopped.
"""

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from kebivo.settings import Settings


def _check_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"SQLite integrity check failed: {result!r}")
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone():
            raise RuntimeError("Backup has no Alembic version table")


def _temporary_path(parent: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".app-sqlite-", suffix=".tmp", dir=parent)
    os.close(descriptor)
    return Path(name)


def backup(database: Path) -> None:
    if not database.is_file():
        raise RuntimeError(f"SQLite database does not exist: {database}")
    temporary = _temporary_path(database.parent)
    try:
        with sqlite3.connect(database) as source, sqlite3.connect(temporary) as target:
            source.backup(target)
        _check_database(temporary)
        with temporary.open("rb") as backup_file:
            shutil.copyfileobj(backup_file, sys.stdout.buffer)
        sys.stdout.buffer.flush()
    finally:
        temporary.unlink(missing_ok=True)


def restore(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(database.parent)
    try:
        with temporary.open("wb") as restore_file:
            shutil.copyfileobj(sys.stdin.buffer, restore_file)
            restore_file.flush()
            os.fsync(restore_file.fileno())
        _check_database(temporary)
        if database.exists():
            with sqlite3.connect(database) as existing:
                busy, _log, _checkpointed = existing.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
            if busy:
                raise RuntimeError("SQLite database is busy; stop the app before restoring")
        # A validated backup is self-contained. Old WAL/SHM files must not be
        # replayed against the restored main file.
        for suffix in ("-wal", "-shm"):
            database.with_name(database.name + suffix).unlink(missing_ok=True)
        os.replace(temporary, database)
        directory = os.open(database.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    args = parser.parse_args()
    database = Path(Settings.from_env().database_path)
    try:
        if args.action == "backup":
            backup(database)
        else:
            restore(database)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"SQLite {args.action} failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
