"""Load a Kebivo JSON Lines export into a SQLite database."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import TextIO

COLUMNS = (
    "id", "project_id", "event_type", "submitted_via", "question", "correction",
    "previous_answer", "context", "source_reference", "agent_id", "conversation_id",
    "source_event_id", "user_reference", "metadata", "processing_status",
    "created_at", "updated_at", "tags", "group", "deliveries",
)
JSON_COLUMNS = {"metadata", "tags", "group", "deliveries"}


def load_export(source: TextIO, connection: sqlite3.Connection) -> None:
    columns = ", ".join(f'"{column}" TEXT' for column in COLUMNS)
    connection.execute(f'CREATE TABLE IF NOT EXISTS feedback ({columns}, PRIMARY KEY ("id"))')
    names = ", ".join(f'"{column}"' for column in COLUMNS)
    placeholders = ", ".join("?" for _ in COLUMNS)
    updates = ", ".join(f'"{column}" = excluded."{column}"' for column in COLUMNS if column != "id")
    statement = (
        f'INSERT INTO feedback ({names}) VALUES ({placeholders}) '
        f'ON CONFLICT("id") DO UPDATE SET {updates}'
    )
    with connection:
        for line in source:
            if not line.strip():
                continue
            event = json.loads(line)
            values = [
                json.dumps(event[column]) if column in JSON_COLUMNS else event[column]
                for column in COLUMNS
            ]
            connection.execute(statement, values)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", nargs="?", type=Path, help="JSON Lines file (default: stdin)")
    parser.add_argument("--database", type=Path, default=Path("feedback.sqlite3"))
    args = parser.parse_args()
    with sqlite3.connect(args.database) as connection:
        if args.export:
            with args.export.open(encoding="utf-8") as source:
                load_export(source, connection)
        else:
            load_export(sys.stdin, connection)


if __name__ == "__main__":
    main()
