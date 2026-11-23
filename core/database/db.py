from __future__ import annotations

import sqlite3
from pathlib import Path

from core.database.connection import connect_sqlite
from core.database.ledger_migrations import (
    PLAYEROK_SYNC_BACKUP_SUFFIX,
    PLAYEROK_SYNC_MIGRATION_ID,
    ORDER_LINE_CONFIG_BACKUP_SUFFIX,
    ORDER_LINE_CONFIG_MIGRATION_ID,
    UNIFIED_RESERVATION_BACKUP_SUFFIX,
    UNIFIED_RESERVATION_MIGRATION_ID,
    ASSET_ID_KEY_BACKUP_SUFFIX,
    ASSET_ID_KEY_MIGRATION_ID,
    ASSET_ID_BLOCKING_STATE_BACKUP_SUFFIX,
    ASSET_ID_BLOCKING_STATE_MIGRATION_ID,
    RECIPIENT_SNAPSHOT_BACKUP_SUFFIX,
    RECIPIENT_SNAPSHOT_MIGRATION_ID,
    QUANTITY_STOCK_BACKUP_SUFFIX,
    QUANTITY_STOCK_MIGRATION_ID,
    QUANTITY_STOCK_AUDIT_BACKUP_SUFFIX,
    QUANTITY_STOCK_AUDIT_MIGRATION_ID,
    QUANTITY_STOCK_TRANSFER_BACKUP_SUFFIX,
    QUANTITY_STOCK_TRANSFER_MIGRATION_ID,
    QUANTITY_STOCK_BATCH_BACKUP_SUFFIX,
    QUANTITY_STOCK_BATCH_MIGRATION_ID,
    QUANTITY_STOCK_IDENTITY_BACKUP_SUFFIX,
    QUANTITY_STOCK_IDENTITY_MIGRATION_ID,
    QUANTITY_STOCK_RECIPE_ITEM_BACKUP_SUFFIX,
    QUANTITY_STOCK_RECIPE_ITEM_MIGRATION_ID,
    QUANTITY_STOCK_LISTING_ADJUSTMENT_BACKUP_SUFFIX,
    QUANTITY_STOCK_LISTING_ADJUSTMENT_MIGRATION_ID,
    QUANTITY_STOCK_LISTING_ADJUSTMENT_HARDENING_BACKUP_SUFFIX,
    QUANTITY_STOCK_LISTING_ADJUSTMENT_HARDENING_MIGRATION_ID,
    QUANTITY_STOCK_LISTING_INCREASE_BACKUP_SUFFIX,
    QUANTITY_STOCK_LISTING_INCREASE_MIGRATION_ID,
    QUANTITY_STOCK_LISTING_REALLOCATION_BACKUP_SUFFIX,
    QUANTITY_STOCK_LISTING_REALLOCATION_MIGRATION_ID,
    RECIPE_BACKUP_SUFFIX,
    RECIPE_MIGRATION_ID,
    apply_delivery_ledger_migrations,
    ensure_pre_migration_backup,
)


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
        ensure_pre_migration_backup(
            self.path,
            migration_id=PLAYEROK_SYNC_MIGRATION_ID,
            backup_suffix=PLAYEROK_SYNC_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=RECIPE_MIGRATION_ID,
            backup_suffix=RECIPE_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=ORDER_LINE_CONFIG_MIGRATION_ID,
            backup_suffix=ORDER_LINE_CONFIG_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=UNIFIED_RESERVATION_MIGRATION_ID,
            backup_suffix=UNIFIED_RESERVATION_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=ASSET_ID_KEY_MIGRATION_ID,
            backup_suffix=ASSET_ID_KEY_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=ASSET_ID_BLOCKING_STATE_MIGRATION_ID,
            backup_suffix=ASSET_ID_BLOCKING_STATE_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=RECIPIENT_SNAPSHOT_MIGRATION_ID,
            backup_suffix=RECIPIENT_SNAPSHOT_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=QUANTITY_STOCK_MIGRATION_ID,
            backup_suffix=QUANTITY_STOCK_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=QUANTITY_STOCK_AUDIT_MIGRATION_ID,
            backup_suffix=QUANTITY_STOCK_AUDIT_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=QUANTITY_STOCK_TRANSFER_MIGRATION_ID,
            backup_suffix=QUANTITY_STOCK_TRANSFER_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=QUANTITY_STOCK_BATCH_MIGRATION_ID,
            backup_suffix=QUANTITY_STOCK_BATCH_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=QUANTITY_STOCK_IDENTITY_MIGRATION_ID,
            backup_suffix=QUANTITY_STOCK_IDENTITY_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=QUANTITY_STOCK_RECIPE_ITEM_MIGRATION_ID,
            backup_suffix=QUANTITY_STOCK_RECIPE_ITEM_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=QUANTITY_STOCK_LISTING_ADJUSTMENT_MIGRATION_ID,
            backup_suffix=QUANTITY_STOCK_LISTING_ADJUSTMENT_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=QUANTITY_STOCK_LISTING_ADJUSTMENT_HARDENING_MIGRATION_ID,
            backup_suffix=QUANTITY_STOCK_LISTING_ADJUSTMENT_HARDENING_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=QUANTITY_STOCK_LISTING_INCREASE_MIGRATION_ID,
            backup_suffix=QUANTITY_STOCK_LISTING_INCREASE_BACKUP_SUFFIX,
        )
        ensure_pre_migration_backup(
            self.path,
            migration_id=QUANTITY_STOCK_LISTING_REALLOCATION_MIGRATION_ID,
            backup_suffix=QUANTITY_STOCK_LISTING_REALLOCATION_BACKUP_SUFFIX,
        )
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            apply_delivery_ledger_migrations(connection)
