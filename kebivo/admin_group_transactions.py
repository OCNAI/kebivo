"""SQLite write transaction shared by administrator group mutations."""

from sqlalchemy.orm import Session


def begin_group_write(db: Session) -> None:
    """Serialize group writes before reading rows that may be merged or deleted."""
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
