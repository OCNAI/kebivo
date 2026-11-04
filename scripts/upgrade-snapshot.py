#!/usr/bin/env python3
"""Stable per-table counts and content hashes for an idle SQLite volume.

Set UPGRADE_COLUMNS to an earlier snapshot to hash only the columns it recorded, so columns
added by a migration do not count as changed data.
"""

import hashlib
import json
import os
import sqlite3

connection = sqlite3.connect("/data/app.sqlite3")
connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
earlier = json.loads(os.environ.get("UPGRADE_COLUMNS") or "{}")
result = {}
for (table,) in connection.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
    "AND name != 'alembic_version' ORDER BY name"
).fetchall():
    columns = earlier.get(table, {}).get("columns") or [
        row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
    ]
    selected = ", ".join(f'"{column}"' for column in columns)
    rows = connection.execute(f'SELECT {selected} FROM "{table}"').fetchall()
    encoded = json.dumps(
        sorted(rows, key=lambda row: json.dumps(row, default=lambda value: value.hex())),
        default=lambda value: value.hex(), separators=(",", ":")
    )
    result[table] = {
        "columns": columns,
        "count": len(rows),
        "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
    }
print(json.dumps(result, sort_keys=True))
