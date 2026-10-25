#!/usr/bin/env python3
"""Stable per-table counts and content hashes for an idle SQLite volume."""

import hashlib
import json
import sqlite3

connection = sqlite3.connect("/data/app.sqlite3")
connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
result = {}
for (table,) in connection.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
    "AND name != 'alembic_version' ORDER BY name"
):
    rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
    encoded = json.dumps(
        sorted(rows, key=lambda row: json.dumps(row, default=lambda value: value.hex())),
        default=lambda value: value.hex(), separators=(",", ":")
    )
    result[table] = {"count": len(rows), "sha256": hashlib.sha256(encoded.encode()).hexdigest()}
print(json.dumps(result, sort_keys=True))
