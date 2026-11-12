"""Create trial keys once, before the container validates settings or migrates."""

import logging
import os
import secrets
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from sqlalchemy.engine import make_url

from kebivo.settings import SettingsError


def database_is_fresh(path: Path) -> bool:
    if not path.exists():
        return True
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        try:
            return db.execute("SELECT 1 FROM alembic_version LIMIT 1").fetchone() is None
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return True
            raise


def generate(environ: dict[str, str] | None = None) -> None:
    values = os.environ if environ is None else environ
    filename = values.get("KEBIVO_SECRETS_FILE")
    if not filename:
        return
    path = Path(filename)
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name in {"SECRET_KEY", "ENCRYPTION_KEY"}:
                existing[name] = value
    missing = [
        name
        for name in ("SECRET_KEY", "ENCRYPTION_KEY")
        if not values.get(name) and not existing.get(name)
    ]
    if not missing:
        return
    database_url = values.get("DATABASE_URL", "sqlite:///./data/app.sqlite3")
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database:
        raise SettingsError("DATABASE_URL must use SQLite")
    database_path = Path(url.database)
    if not database_is_fresh(database_path) or path.exists():
        raise SettingsError(f"{missing[0]} is required for an existing database or secrets file")
    content = "".join(f"{name}={secrets.token_hex(32)}\n" for name in missing)
    try:
        fd, temporary = tempfile.mkstemp(prefix=".kebivo-secrets-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            try:
                os.link(temporary, path)
            except (FileExistsError, PermissionError):
                raise
            except OSError:
                # The filesystem has no hard links; create the file exclusively instead.
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(temporary)
    except FileExistsError:
        # A concurrent start installed its own complete file first.
        return
    except PermissionError:
        raise SettingsError(
            "Cannot write secrets file as uid 10001; set SECRET_KEY and ENCRYPTION_KEY"
        ) from None
    except OSError as exc:
        raise SettingsError(
            f"Cannot write secrets file ({exc.strerror}); set SECRET_KEY and ENCRYPTION_KEY"
        ) from None
    if len(missing) == 1:
        logging.getLogger(__name__).warning(
            "Generated %s in KEBIVO_SECRETS_FILE; the other key comes from the environment",
            missing[0],
        )


if __name__ == "__main__":
    try:
        generate()
    except (SettingsError, sqlite3.Error) as exc:
        print(
            f"SettingsError: {exc}"
            if isinstance(exc, SettingsError)
            else "Cannot inspect database for first run",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
