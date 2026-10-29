"""SQLite engine, session, and short transaction helpers."""

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from kebivo.settings import Settings


def make_engine(database_url: str | None = None) -> Engine:
    """Build an engine configured with the SQLite safety defaults."""
    configured = Settings.from_env()
    default_url = (
        "sqlite://"
        if configured.database_path == ":memory:"
        else f"sqlite:///{configured.database_path}"
    )
    url = database_url or configured.database_url or default_url
    kwargs: dict[str, Any] = {"pool_pre_ping": True, "hide_parameters": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 5}
        database = make_url(url).database
        if database and database != ":memory:":
            kwargs["pool_timeout"] = 2
            Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA secure_delete=ON")
            cursor.close()

        @event.listens_for(engine, "checkin")
        def restore_busy_timeout(dbapi_connection: Any, _connection_record: Any) -> None:
            if dbapi_connection is not None:
                dbapi_connection.execute("PRAGMA busy_timeout=5000")

        # WAL is persistent per database and must be applied outside transactions.
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding one request-scoped session."""
    with SessionLocal() as session:
        yield session


@contextmanager
def transaction(session_factory: sessionmaker[Session] = SessionLocal) -> Iterator[Session]:
    """Run a short unit of work, committing on success and rolling back on error."""
    with session_factory() as session:
        with session.begin():
            yield session
