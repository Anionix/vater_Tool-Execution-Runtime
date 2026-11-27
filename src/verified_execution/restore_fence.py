"""task.operations.016: a restored ledger refuses new external effects until its gap is reconciled.

A backup cannot hold what happened after it was taken. When a ledger is
restored from a backup, operations reserved and executed after the backup
are missing, and resubmitting one would reserve a new operation and execute
it again. scripts/backup_ledger.py therefore installs a fence in the restored
ledger file; while a fence is active, executors with external effects send
nothing new (replays of recorded operations and lookups still work). The
fence is cleared only with a reconciliation note: by a gap report that found
no unknown object at the destination, or by a person.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS restore_fences (
    fence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    backup_taken_at_ms INTEGER NOT NULL,
    backup_sha256 TEXT NOT NULL,
    installed_at_ms INTEGER NOT NULL,
    cleared_at_ms INTEGER,
    reconciled_by TEXT
)"""


def _connect(path: str | Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), timeout=5.0)


def install(path: str | Path, *, backup_taken_at_ms: int, backup_sha256: str) -> int:
    with closing(_connect(path)) as connection, connection:
        connection.execute(_SCHEMA)
        cursor = connection.execute(
            "INSERT INTO restore_fences(backup_taken_at_ms,backup_sha256,installed_at_ms) VALUES (?,?,?)",
            (backup_taken_at_ms, backup_sha256, int(time.time() * 1000)))
        return int(cursor.lastrowid)


def active(path: str | Path) -> dict | None:
    """The oldest uncleared fence, or None. A ledger never restored has no fence table."""
    with closing(_connect(path)) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='restore_fences'").fetchone()
        if not exists:
            return None
        row = connection.execute(
            "SELECT fence_id,backup_taken_at_ms,backup_sha256,installed_at_ms FROM restore_fences "
            "WHERE cleared_at_ms IS NULL ORDER BY fence_id LIMIT 1").fetchone()
    if row is None:
        return None
    return dict(zip(("fence_id", "backup_taken_at_ms", "backup_sha256", "installed_at_ms"), row))


def clear(path: str | Path, *, reconciled_by: str) -> int:
    """Clear every active fence; returns how many were cleared."""
    if not isinstance(reconciled_by, str) or not reconciled_by.strip():
        raise ValueError("a fence is cleared only with a reconciliation note")
    with closing(_connect(path)) as connection, connection:
        connection.execute(_SCHEMA)
        cursor = connection.execute(
            "UPDATE restore_fences SET cleared_at_ms=?, reconciled_by=? WHERE cleared_at_ms IS NULL",
            (int(time.time() * 1000), reconciled_by))
        return cursor.rowcount
