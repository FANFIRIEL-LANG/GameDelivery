from __future__ import annotations

import sqlite3
from pathlib import Path

from core.database.connection import connect_sqlite
from core.database.ledger_migrations import apply_delivery_ledger_migrations, ensure_pre_migration_backup


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    item_type TEXT NOT NULL,
    item_name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'processing', 'completed', 'failed', 'cancelled')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_status_created
    ON orders(status, created_at);
"""


class Database:
    """Small SQLite wrapper. Use one instance per database path."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def initialize(self) -> None:
        ensure_pre_migration_backup(self.path)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            apply_delivery_ledger_migrations(connection)
