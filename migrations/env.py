import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from kebivo.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
database_url = (
    config.attributes.get("database_url")
    or os.getenv("DATABASE_URL")
    or config.get_main_option("sqlalchemy.url")
)
config.set_main_option("sqlalchemy.url", database_url)
target_metadata = Base.metadata


def include_object(object, name, type_, reflected, compare_to):
    if (
        reflected
        and type_ == "table"
        and (name == "feedback_search" or name.startswith("feedback_search_"))
    ):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    if connectable.dialect.name == "sqlite":
        # SQLAlchemy's pysqlite recipe: stop sqlite3 from managing transactions and emit
        # BEGIN ourselves, so DDL joins the transaction and a failed run rolls back fully.
        @event.listens_for(connectable, "connect")
        def disable_driver_transactions(dbapi_connection, _record):
            dbapi_connection.isolation_level = None
            # Table rebuilds need foreign keys off, and SQLite ignores this PRAGMA once
            # a transaction is open. The check below runs before the commit instead.
            dbapi_connection.execute("PRAGMA foreign_keys=OFF")

        @event.listens_for(connectable, "begin")
        def begin(connection):
            connection.exec_driver_sql("BEGIN")

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            transactional_ddl=True,
        )
        with context.begin_transaction():
            before = context.get_context().get_current_heads()
            context.run_migrations()
            migrated = context.get_context().get_current_heads() != before
            if migrated and connection.dialect.name == "sqlite":
                broken = connection.exec_driver_sql("PRAGMA foreign_key_check").first()
                if broken:
                    raise RuntimeError(
                        f"Migration left a {broken[0]} row (rowid {broken[1]}) that points "
                        f"to a missing {broken[2]} row; nothing was changed"
                    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
