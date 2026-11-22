from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from core.database.connection import connect_sqlite
from games.asset_ids import asset_id_key


MIGRATION_ID = "2026-11-delivery-ledger-v1"
BACKUP_SUFFIX = ".pre-gamedelivery-ledger-v1.bak"
PLAYEROK_SYNC_MIGRATION_ID = "2026-11-playerok-sync-v2"
PLAYEROK_SYNC_BACKUP_SUFFIX = ".pre-gamedelivery-playerok-sync-v2.bak"
RECIPE_MIGRATION_ID = "2026-11-delivery-recipes-v1"
RECIPE_BACKUP_SUFFIX = ".pre-gamedelivery-delivery-recipes-v1.bak"
ORDER_LINE_CONFIG_MIGRATION_ID = "2026-11-order-line-config-version-v1"
ORDER_LINE_CONFIG_BACKUP_SUFFIX = ".pre-gamedelivery-order-line-config-version-v1.bak"
UNIFIED_RESERVATION_MIGRATION_ID = "2026-11-unified-resource-reservations-v1"
UNIFIED_RESERVATION_BACKUP_SUFFIX = ".pre-gamedelivery-unified-resource-reservations-v1.bak"
ASSET_ID_KEY_MIGRATION_ID = "2026-11-asset-id-key-v1"
ASSET_ID_KEY_BACKUP_SUFFIX = ".pre-gamedelivery-asset-id-key-v1.bak"
ASSET_ID_BLOCKING_STATE_MIGRATION_ID = "2026-11-asset-id-blocking-state-v1"
ASSET_ID_BLOCKING_STATE_BACKUP_SUFFIX = ".pre-gamedelivery-asset-id-blocking-state-v1.bak"
RECIPIENT_SNAPSHOT_MIGRATION_ID = "2026-11-delivery-recipient-snapshot-v1"
RECIPIENT_SNAPSHOT_BACKUP_SUFFIX = ".pre-gamedelivery-delivery-recipient-snapshot-v1.bak"
OPERATOR_RECOVERY_AUDIT_MIGRATION_ID = "2026-11-delivery-operator-recovery-audit-v1"
OPERATOR_RECOVERY_AUDIT_BACKUP_SUFFIX = ".pre-gamedelivery-delivery-operator-recovery-audit-v1.bak"


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS gd_schema_migrations (
        migration_id TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gd_listings (
        listing_id TEXT PRIMARY KEY,
        source_platform TEXT NOT NULL,
        external_listing_id TEXT,
        title TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'unknown',
        auto_relist INTEGER NOT NULL DEFAULT 0 CHECK (auto_relist IN (0, 1)),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (source_platform, external_listing_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gd_config_versions (
        config_version_id TEXT PRIMARY KEY,
        listing_id TEXT NOT NULL REFERENCES gd_listings(listing_id) ON DELETE RESTRICT,
        version_number INTEGER NOT NULL CHECK (version_number > 0),
        delivery_quantity_per_deal INTEGER
            CHECK (delivery_quantity_per_deal IS NULL OR delivery_quantity_per_deal > 0),
        item_selectors_json TEXT NOT NULL,
        bot_pool_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (listing_id, version_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gd_deals (
        deal_id TEXT PRIMARY KEY,
        source_platform TEXT NOT NULL,
        external_deal_id TEXT NOT NULL,
        chat_id TEXT,
        listing_id TEXT NOT NULL REFERENCES gd_listings(listing_id) ON DELETE RESTRICT,
        config_version_id TEXT NOT NULL REFERENCES gd_config_versions(config_version_id) ON DELETE RESTRICT,
        platform_status TEXT NOT NULL DEFAULT 'unknown',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (source_platform, external_deal_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gd_orders (
        order_id TEXT PRIMARY KEY,
        deal_id TEXT NOT NULL UNIQUE REFERENCES gd_deals(deal_id) ON DELETE RESTRICT,
        status TEXT NOT NULL DEFAULT 'pending',
        total_quantity INTEGER NOT NULL CHECK (total_quantity >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gd_order_lines (
        order_line_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES gd_orders(order_id) ON DELETE RESTRICT,
        config_version_id TEXT REFERENCES gd_config_versions(config_version_id) ON DELETE RESTRICT,
        line_number INTEGER NOT NULL CHECK (line_number > 0),
        item_type TEXT NOT NULL,
        item_name TEXT NOT NULL DEFAULT '',
        item_selector_json TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK (quantity >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (order_id, line_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gd_delivery_legs (
        leg_id TEXT PRIMARY KEY,
        order_line_id TEXT NOT NULL REFERENCES gd_order_lines(order_line_id) ON DELETE RESTRICT,
        bot_name TEXT NOT NULL COLLATE NOCASE,
        app_id INTEGER NOT NULL CHECK (app_id > 0),
        context_id TEXT NOT NULL,
        asset_ids_json TEXT NOT NULL,
        trade_offer_id TEXT,
        status TEXT NOT NULL DEFAULT 'planned',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gd_delivery_attempts (
        attempt_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES gd_orders(order_id) ON DELETE RESTRICT,
        leg_id TEXT NOT NULL REFERENCES gd_delivery_legs(leg_id) ON DELETE RESTRICT,
        retry_generation INTEGER NOT NULL CHECK (retry_generation >= 0),
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'prepared',
        started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        finished_at TEXT,
        result_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE (order_id, leg_id, retry_generation),
        CHECK (
            idempotency_key =
            order_id || ':' || leg_id || ':' || CAST(retry_generation AS TEXT)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gd_resource_reservations (
        reservation_id TEXT PRIMARY KEY,
        owner_type TEXT NOT NULL CHECK (owner_type IN ('listing', 'order', 'legacy')),
        listing_id TEXT REFERENCES gd_listings(listing_id) ON DELETE RESTRICT,
        config_version_id TEXT REFERENCES gd_config_versions(config_version_id) ON DELETE RESTRICT,
        order_line_id TEXT REFERENCES gd_order_lines(order_line_id) ON DELETE RESTRICT,
        legacy_owner_id TEXT,
        listing_generation INTEGER NOT NULL DEFAULT 0 CHECK (listing_generation >= 0),
        game_id TEXT NOT NULL DEFAULT '',
        app_id INTEGER NOT NULL DEFAULT 753 CHECK (app_id >= 0),
        context_id TEXT NOT NULL DEFAULT '6',
        bot_name TEXT NOT NULL COLLATE NOCASE,
        asset_id TEXT NOT NULL,
        units INTEGER NOT NULL DEFAULT 1 CHECK (units > 0),
        state TEXT NOT NULL DEFAULT 'active'
            CHECK (state IN ('active', 'released', 'consumed', 'legacy_conflict')),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        CHECK (
            (owner_type = 'listing' AND listing_id IS NOT NULL
                AND order_line_id IS NULL AND legacy_owner_id IS NULL)
            OR
            (owner_type = 'order' AND listing_id IS NULL
                AND order_line_id IS NOT NULL AND legacy_owner_id IS NULL)
            OR
            (owner_type = 'legacy' AND listing_id IS NULL
                AND order_line_id IS NULL AND legacy_owner_id IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gd_legacy_order_map (
        legacy_table TEXT NOT NULL,
        legacy_order_id TEXT NOT NULL,
        order_id TEXT NOT NULL REFERENCES gd_orders(order_id) ON DELETE RESTRICT,
        PRIMARY KEY (legacy_table, legacy_order_id),
        UNIQUE (order_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gd_deals_chat ON gd_deals(chat_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_gd_orders_status ON gd_orders(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_gd_legs_offer ON gd_delivery_legs(trade_offer_id)",
    "CREATE INDEX IF NOT EXISTS idx_gd_attempts_status ON gd_delivery_attempts(status, started_at)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_gd_resource_active_global
        ON gd_resource_reservations(bot_name COLLATE NOCASE, asset_id)
        WHERE state = 'active'
    """,
    """
    CREATE TRIGGER IF NOT EXISTS gd_config_versions_no_update
    BEFORE UPDATE ON gd_config_versions
    BEGIN
        SELECT RAISE(ABORT, 'ConfigVersion is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS gd_config_versions_no_delete
    BEFORE DELETE ON gd_config_versions
    BEGIN
        SELECT RAISE(ABORT, 'ConfigVersion is immutable');
    END
    """,
)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _quote_sql_identifier(value: str) -> str:
    """Quote an identifier read from sqlite_master before using it in DDL."""
    return '"' + value.replace('"', '""') + '"'


def _migration_applied(
    connection: sqlite3.Connection, migration_id: str = MIGRATION_ID
) -> bool:
    if not _table_exists(connection, "gd_schema_migrations"):
        return False
    return connection.execute(
        "SELECT 1 FROM gd_schema_migrations WHERE migration_id=?", (migration_id,)
    ).fetchone() is not None


def _redact_backup_json(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            sensitive = (
                "token" in normalized
                or "password" in normalized
                or "cookie" in normalized
                or "secret" in normalized
                or ("trade" in normalized and "url" in normalized)
            )
            redacted[key] = "" if sensitive else _redact_backup_json(item)
        return redacted
    if isinstance(value, list):
        return [_redact_backup_json(item) for item in value]
    return value


def _sanitize_backup_secrets(connection: sqlite3.Connection) -> None:
    """Keep the rollback copy useful without duplicating Steam trade secrets."""
    tables = [
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    for table in tables:
        quoted_table = '"' + table.replace('"', '""') + '"'
        for column_info in connection.execute(f"PRAGMA table_info({quoted_table})").fetchall():
            column = str(column_info[1])
            normalized = column.casefold().replace("-", "_")
            is_sensitive = (
                normalized in {"recipient_trade_url", "steam_trade_url", "trade_url", "trade_token"}
                or "access_token" in normalized
                or "password" in normalized
                or "cookie" in normalized
                or normalized.endswith("_secret")
            )
            if is_sensitive:
                quoted_column = '"' + column.replace('"', '""') + '"'
                replacement = "" if bool(column_info[3]) else None
                connection.execute(
                    f"UPDATE {quoted_table} SET {quoted_column}=? WHERE {quoted_column} IS NOT NULL",
                    (replacement,),
                )
            elif normalized.endswith("_json") or normalized in {"payload", "payload_json"}:
                quoted_column = '"' + column.replace('"', '""') + '"'
                rows = connection.execute(
                    f"SELECT rowid, {quoted_column} FROM {quoted_table} WHERE {quoted_column} IS NOT NULL"
                ).fetchall()
                for row_id, raw in rows:
                    try:
                        parsed = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    redacted = _redact_backup_json(parsed)
                    encoded = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
                    if encoded != raw:
                        connection.execute(
                            f"UPDATE {quoted_table} SET {quoted_column}=? WHERE rowid=?",
                            (encoded, row_id),
                        )


def ensure_pre_migration_backup(
    database_path: str | Path,
    *,
    migration_id: str = MIGRATION_ID,
    backup_suffix: str = BACKUP_SUFFIX,
) -> Path | None:
    """Create one online backup before migration, with token-bearing fields redacted."""
    path = Path(database_path)
    if str(database_path) == ":memory:" or not path.exists() or path.stat().st_size == 0:
        return None
    backup_path = Path(str(path) + backup_suffix)
    if backup_path.exists():
        return backup_path
    temporary_path = Path(str(backup_path) + ".tmp")
    try:
        with connect_sqlite(path) as source:
            if _migration_applied(source, migration_id):
                return None
            with connect_sqlite(temporary_path) as destination:
                source.backup(destination)
        with connect_sqlite(temporary_path) as backup:
            backup.execute("PRAGMA secure_delete = ON")
            _sanitize_backup_secrets(backup)
            backup.commit()
            backup.execute("VACUUM")
            integrity = backup.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise sqlite3.DatabaseError("SQLite backup integrity check failed")
        try:
            temporary_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary_path, backup_path)
    finally:
        try:
            if temporary_path.exists():
                temporary_path.unlink()
        except OSError:
            pass
    return backup_path


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _json_object(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _legacy_listing_and_config(
    connection: sqlite3.Connection,
    *,
    listing_id: str,
    source_platform: str,
    item_selectors: dict[str, Any],
) -> str:
    connection.execute(
        """
        INSERT OR IGNORE INTO gd_listings
            (listing_id, source_platform, external_listing_id, title, status)
        VALUES (?, ?, NULL, 'Историческое объявление (миграция)', 'historical')
        """,
        (listing_id, source_platform),
    )
    config_id = f"{listing_id}:config:1"
    connection.execute(
        """
        INSERT OR IGNORE INTO gd_config_versions
            (config_version_id, listing_id, version_number,
             delivery_quantity_per_deal, item_selectors_json, bot_pool_json)
        VALUES (?, ?, 1, NULL, ?, '[]')
        """,
        (config_id, listing_id, json.dumps(item_selectors, ensure_ascii=False, sort_keys=True)),
    )
    return config_id


def _insert_legacy_order(
    connection: sqlite3.Connection,
    *,
    legacy_table: str,
    legacy_order_id: str,
    order_id: str,
    deal_id: str,
    listing_id: str,
    config_version_id: str,
    source_platform: str,
    external_deal_id: str,
    chat_id: str | None,
    platform_status: str,
    payload: dict[str, Any],
    order_status: str,
    quantity: int,
    item_type: str,
    item_name: str,
    selector: dict[str, Any],
    created_at: str | None = None,
) -> str:
    connection.execute(
        """
        INSERT OR IGNORE INTO gd_deals
            (deal_id, source_platform, external_deal_id, chat_id,
             listing_id, config_version_id, platform_status, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            deal_id, source_platform, external_deal_id, chat_id,
            listing_id, config_version_id, platform_status,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO gd_orders
            (order_id, deal_id, status, total_quantity, created_at)
        VALUES (?, ?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))
        """,
        (order_id, deal_id, order_status or "legacy_imported", quantity, created_at),
    )
    line_id = f"{order_id}:line:1"
    connection.execute(
        """
        INSERT OR IGNORE INTO gd_order_lines
            (order_line_id, order_id, line_number, item_type,
             item_name, item_selector_json, quantity)
        VALUES (?, ?, 1, ?, ?, ?, ?)
        """,
        (
            line_id, order_id, item_type or "legacy", item_name or "",
            json.dumps(selector, ensure_ascii=False, sort_keys=True), quantity,
        ),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO gd_legacy_order_map (legacy_table, legacy_order_id, order_id)
        VALUES (?, ?, ?)
        """,
        (legacy_table, legacy_order_id, order_id),
    )
    return line_id


def _backfill_core_orders(connection: sqlite3.Connection) -> None:
    columns = _columns(connection, "orders")
    required = {"id", "source", "external_id", "item_type", "item_name", "quantity", "status"}
    if not required.issubset(columns):
        return
    for row in connection.execute("SELECT * FROM orders ORDER BY id").fetchall():
        legacy_id = str(row["id"])
        source = str(row["source"] or "unknown")
        item_type = str(row["item_type"] or "legacy")
        title = str(row["item_name"] or "")
        listing_id = _stable_id("legacy-core-listing", source + "|" + item_type)
        config_id = _legacy_listing_and_config(
            connection, listing_id=listing_id, source_platform="legacy-core",
            item_selectors={"item_type": item_type, "item_name": title},
        )
        payload = _json_object(row["payload_json"] if "payload_json" in columns else "{}")
        raw_chat = payload.get("chat_id", payload.get("chatId"))
        chat_id = str(raw_chat) if raw_chat is not None else None
        _insert_legacy_order(
            connection, legacy_table="orders", legacy_order_id=legacy_id,
            order_id=f"legacy-core-order:{legacy_id}", deal_id=f"legacy-core-deal:{legacy_id}",
            listing_id=listing_id, config_version_id=config_id,
            source_platform="legacy-core:" + source,
            external_deal_id=str(row["external_id"] or legacy_id),
            chat_id=chat_id,
            platform_status=str(payload.get("platform_status", row["status"])),
            payload={"legacy_table": "orders", "legacy_id": legacy_id, "payload": payload},
            order_status=str(row["status"] or "legacy_imported"),
            quantity=max(0, int(row["quantity"])),
            item_type=item_type, item_name=title,
            selector={"item_type": item_type, "item_name": title},
            created_at=str(row["created_at"]) if "created_at" in columns else None,
        )


def _backfill_delivery_orders(connection: sqlite3.Connection) -> None:
    columns = _columns(connection, "delivery_orders")
    if not {"order_id", "status", "asset_count"}.issubset(columns):
        return
    has_assets = {"order_id", "bot_name", "asset_id"}.issubset(_columns(connection, "delivery_order_assets"))
    rows = connection.execute("SELECT * FROM delivery_orders ORDER BY order_id").fetchall()
    for row in rows:
        legacy_id = str(row["order_id"])
        platform = str(row["source_platform"] or "manual") if "source_platform" in columns else "manual"
        title = str(row["source_title"] or "") if "source_title" in columns else ""
        listing_id = _stable_id("legacy-delivery-listing", platform)
        selector = {"legacy": True, "source_platform": platform, "source_title": title}
        config_id = _legacy_listing_and_config(
            connection, listing_id=listing_id, source_platform=platform, item_selectors=selector
        )
        offer_id = str(row["trade_offer_id"] or "") if "trade_offer_id" in columns else ""
        payload = {
            "legacy_table": "delivery_orders",
            "legacy_order_id": legacy_id,
            "source_order_id": str(row["source_order_id"] or "") if "source_order_id" in columns else "",
            "source_buyer": str(row["source_buyer"] or "") if "source_buyer" in columns else "",
            "source_amount": str(row["source_amount"] or "") if "source_amount" in columns else "",
            "source_url": str(row["source_url"] or "") if "source_url" in columns else "",
            "trade_offer_id": offer_id,
            "trade_confirmation_status": (
                str(row["trade_confirmation_status"] or "") if "trade_confirmation_status" in columns else ""
            ),
        }
        order_id = f"legacy-delivery-order:{legacy_id}"
        line_id = _insert_legacy_order(
            connection, legacy_table="delivery_orders", legacy_order_id=legacy_id,
            order_id=order_id, deal_id=f"legacy-delivery-deal:{legacy_id}",
            listing_id=listing_id, config_version_id=config_id,
            source_platform=platform, external_deal_id=f"legacy-delivery:{legacy_id}",
            chat_id=None,
            platform_status=str(row["source_status"] or "unknown") if "source_status" in columns else "unknown",
            payload=payload,
            order_status=str(row["status"] or "legacy_imported"),
            quantity=max(0, int(row["asset_count"])),
            item_type="legacy_steam_cards", item_name=title, selector=selector,
            created_at=str(row["created_at"]) if "created_at" in columns else None,
        )
        if not has_assets:
            continue
        assets = connection.execute(
            """
            SELECT bot_name, asset_id FROM delivery_order_assets
            WHERE order_id = ? ORDER BY bot_name COLLATE NOCASE, asset_id
            """, (legacy_id,)
        ).fetchall()
        by_bot: dict[str, list[str]] = {}
        for asset in assets:
            bot = str(asset["bot_name"] or "").strip()
            asset_id = str(asset["asset_id"] or "").strip()
            if bot and asset_id:
                bucket = by_bot.setdefault(bot, [])
                if asset_id not in bucket:
                    bucket.append(asset_id)
        single_offer_mapping = len(by_bot) == 1 and bool(offer_id)
        for bot, asset_ids in by_bot.items():
            leg_id = f"{order_id}:leg:{_stable_id('bot', bot).split(':', 1)[1]}"
            leg_status = (
                "legacy_offer_id_preserved" if single_offer_mapping
                else "legacy_offer_mapping_unknown" if offer_id
                else "legacy_manifest_imported"
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO gd_delivery_legs
                    (leg_id, order_line_id, bot_name, app_id, context_id,
                     asset_ids_json, trade_offer_id, status)
                VALUES (?, ?, ?, 753, '6', ?, ?, ?)
                """,
                (leg_id, line_id, bot, json.dumps(asset_ids, ensure_ascii=False),
                 offer_id if single_offer_mapping else None, leg_status),
            )
            generation = 0
            attempt_status = "legacy_imported_unknown" if offer_id and not single_offer_mapping else "legacy_imported"
            connection.execute(
                """
                INSERT OR IGNORE INTO gd_delivery_attempts
                    (attempt_id, order_id, leg_id, retry_generation,
                     idempotency_key, status, result_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{leg_id}:attempt:0", order_id, leg_id, generation,
                    f"{order_id}:{leg_id}:{generation}", attempt_status,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )


def _backfill_legacy_reservations(connection: sqlite3.Connection) -> None:
    required = {"bot_name", "asset_id", "order_id"}
    if not required.issubset(_columns(connection, "steam_asset_reservations")):
        return
    rows = connection.execute(
        "SELECT bot_name, asset_id, order_id FROM steam_asset_reservations ORDER BY bot_name, asset_id"
    ).fetchall()
    for row in rows:
        bot = str(row["bot_name"] or "").strip()
        asset_id = str(row["asset_id"] or "").strip()
        legacy_order_id = str(row["order_id"] or "")
        if not bot or not asset_id:
            continue
        mapping = connection.execute(
            """
            SELECT l.order_line_id
            FROM gd_legacy_order_map m
            JOIN gd_order_lines l ON l.order_id = m.order_id
            WHERE m.legacy_table = 'delivery_orders' AND m.legacy_order_id = ?
            """, (legacy_order_id,)
        ).fetchone()
        reservation_id = _stable_id("legacy-reservation", bot.casefold() + "|" + asset_id)
        owner_type = "order" if mapping is not None else "legacy"
        try:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO gd_resource_reservations
                    (reservation_id, owner_type, order_line_id, legacy_owner_id,
                     game_id, app_id, context_id, bot_name, asset_id, units, state)
                VALUES (?, ?, ?, ?, 'steam_cards', 753, '6', ?, ?, 1, 'active')
                """,
                (
                    reservation_id, owner_type,
                    mapping["order_line_id"] if mapping is not None else None,
                    None if mapping is not None else f"steam_asset_reservations:{legacy_order_id}",
                    bot, asset_id,
                ),
            )
            if cursor.rowcount == 0:
                conflict_id = _stable_id(
                    "legacy-reservation-conflict", bot + "|" + asset_id + "|" + legacy_order_id
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO gd_resource_reservations
                        (reservation_id, owner_type, legacy_owner_id, game_id,
                         app_id, context_id, bot_name, asset_id, units, state)
                    VALUES (?, 'legacy', ?, 'steam_cards', 753, '6', ?, ?, 1, 'legacy_conflict')
                    """,
                    (conflict_id, f"steam_asset_reservations:{legacy_order_id}", bot, asset_id),
                )
        except sqlite3.IntegrityError:
            conflict_id = _stable_id("legacy-reservation-conflict", bot + "|" + asset_id + "|" + legacy_order_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO gd_resource_reservations
                    (reservation_id, owner_type, legacy_owner_id, game_id,
                     app_id, context_id, bot_name, asset_id, units, state)
                VALUES (?, 'legacy', ?, 'steam_cards', 753, '6', ?, ?, 1, 'legacy_conflict')
                """,
                (conflict_id, f"steam_asset_reservations:{legacy_order_id}", bot, asset_id),
            )


def _apply_playerok_sync_migration(connection: sqlite3.Connection) -> bool:
    if _migration_applied(connection, PLAYEROK_SYNC_MIGRATION_ID):
        return False
    if connection.in_transaction:
        raise sqlite3.OperationalError(
            "Playerok sync migration requires a connection without an active transaction"
        )

    previous_foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS playerok_sync_state (
                account_user_id TEXT PRIMARY KEY,
                baseline_completed INTEGER NOT NULL DEFAULT 0 CHECK (baseline_completed IN (0,1)),
                baseline_completed_at TEXT,
                last_sync_at TEXT
            )
        """)
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(gd_deals)")}
        if "playerok_account_id" not in columns:
            connection.execute("ALTER TABLE gd_deals ADD COLUMN playerok_account_id TEXT")
        if "is_historical" not in columns:
            connection.execute(
                "ALTER TABLE gd_deals ADD COLUMN is_historical INTEGER NOT NULL DEFAULT 0"
            )
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(gd_deals)")}
        if "source_listing_ref" not in columns:
            connection.execute(
                "ALTER TABLE gd_deals ADD COLUMN source_listing_ref TEXT NOT NULL DEFAULT ''"
            )
        if "listing_resolution_status" not in columns:
            connection.execute(
                "ALTER TABLE gd_deals ADD COLUMN listing_resolution_status TEXT NOT NULL DEFAULT 'resolved'"
            )

        duplicate = connection.execute("""
            SELECT playerok_account_id,external_deal_id,COUNT(*) AS copies
            FROM gd_deals
            WHERE source_platform='playerok'
              AND playerok_account_id IS NOT NULL AND trim(playerok_account_id)<>''
            GROUP BY playerok_account_id,external_deal_id
            HAVING COUNT(*)>1 LIMIT 1
        """).fetchone()
        if duplicate is not None:
            raise sqlite3.IntegrityError(
                "Playerok account/deal identity conflict; migration stopped without removing rows"
            )

        # Unknown historical ownership/baseline cannot authorize delivery.
        connection.execute("""
            UPDATE gd_deals SET is_historical=1
            WHERE source_platform='playerok' AND (
                playerok_account_id IS NULL OR trim(playerok_account_id)=''
                OR NOT EXISTS (
                    SELECT 1 FROM playerok_sync_state s
                    WHERE s.account_user_id=gd_deals.playerok_account_id
                      AND s.baseline_completed=1
                )
            )
        """)

        connection.execute("""
            CREATE TABLE gd_deals_v2 (
                deal_id TEXT PRIMARY KEY,
                source_platform TEXT NOT NULL,
                external_deal_id TEXT NOT NULL,
                chat_id TEXT,
                listing_id TEXT REFERENCES gd_listings(listing_id) ON DELETE RESTRICT,
                config_version_id TEXT REFERENCES gd_config_versions(config_version_id) ON DELETE RESTRICT,
                platform_status TEXT NOT NULL DEFAULT 'unknown',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                playerok_account_id TEXT,
                is_historical INTEGER NOT NULL DEFAULT 0 CHECK (is_historical IN (0,1)),
                source_listing_ref TEXT NOT NULL DEFAULT '',
                listing_resolution_status TEXT NOT NULL DEFAULT 'resolved'
                    CHECK (listing_resolution_status IN ('resolved','unresolved')),
                CHECK (
                    (listing_id IS NULL AND config_version_id IS NULL
                        AND listing_resolution_status='unresolved')
                    OR (listing_id IS NOT NULL AND listing_resolution_status='resolved')
                )
            )
        """)
        connection.execute("""
            INSERT INTO gd_deals_v2
                (deal_id,source_platform,external_deal_id,chat_id,listing_id,config_version_id,
                 platform_status,payload_json,created_at,playerok_account_id,is_historical,
                 source_listing_ref,listing_resolution_status)
            SELECT deal_id,source_platform,external_deal_id,chat_id,listing_id,config_version_id,
                   platform_status,payload_json,created_at,playerok_account_id,is_historical,
                   source_listing_ref,
                   CASE WHEN listing_id IS NULL THEN 'unresolved' ELSE 'resolved' END
            FROM gd_deals
        """)

        # Some existing installations have application triggers on other
        # tables that reference gd_deals. SQLite reparses those triggers during
        # the rename and fails while gd_deals is temporarily absent. Preserve
        # their exact SQL and restore them inside this same transaction.
        dependent_triggers = connection.execute("""
            SELECT name,sql FROM sqlite_master
            WHERE type='trigger' AND sql IS NOT NULL
              AND lower(sql) LIKE '%gd_deals%'
            ORDER BY name
        """).fetchall()
        for trigger in dependent_triggers:
            connection.execute(
                f"DROP TRIGGER {_quote_sql_identifier(str(trigger[0]))}"
            )
        connection.execute("DROP TABLE gd_deals")
        connection.execute("ALTER TABLE gd_deals_v2 RENAME TO gd_deals")
        connection.execute("""
            CREATE UNIQUE INDEX idx_gd_deals_playerok_account_external
            ON gd_deals(playerok_account_id,external_deal_id)
            WHERE source_platform='playerok'
              AND playerok_account_id IS NOT NULL AND trim(playerok_account_id)<>''
        """)
        connection.execute("""
            CREATE UNIQUE INDEX idx_gd_deals_non_playerok_external
            ON gd_deals(source_platform,external_deal_id)
            WHERE source_platform<>'playerok'
        """)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_gd_deals_chat ON gd_deals(chat_id, created_at)"
        )
        for trigger in dependent_triggers:
            connection.execute(str(trigger[1]))
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"Playerok sync migration found {len(violations)} foreign-key violation(s)"
            )
        connection.execute(
            "INSERT INTO gd_schema_migrations (migration_id) VALUES (?)",
            (PLAYEROK_SYNC_MIGRATION_ID,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        if previous_foreign_keys:
            connection.execute("PRAGMA foreign_keys = ON")
    if previous_foreign_keys and int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise sqlite3.OperationalError("Foreign-key enforcement could not be restored")
    return True


def _apply_recipient_snapshot_migration(connection: sqlite3.Connection) -> bool:
    """Add a nullable recipient snapshot; historical orders remain unverified."""
    if _migration_applied(connection, RECIPIENT_SNAPSHOT_MIGRATION_ID):
        return False
    connection.execute("BEGIN IMMEDIATE")
    try:
        if "recipient_steam_id64" not in _columns(connection, "gd_orders"):
            connection.execute("ALTER TABLE gd_orders ADD COLUMN recipient_steam_id64 TEXT")
        connection.execute(
            "INSERT INTO gd_schema_migrations(migration_id) VALUES(?)",
            (RECIPIENT_SNAPSHOT_MIGRATION_ID,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return True



def _apply_operator_recovery_audit_migration(connection: sqlite3.Connection) -> bool:
    """Persist append-only operator retry/release decisions with evidence metadata."""
    if _migration_applied(connection, OPERATOR_RECOVERY_AUDIT_MIGRATION_ID):
        return False
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS gd_delivery_operator_actions (
                action_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES gd_orders(order_id) ON DELETE RESTRICT,
                leg_id TEXT NOT NULL REFERENCES gd_delivery_legs(leg_id) ON DELETE RESTRICT,
                action_type TEXT NOT NULL CHECK(action_type IN ('retry','release')),
                retry_generation INTEGER,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )
        """)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_gd_delivery_operator_actions_order "
            "ON gd_delivery_operator_actions(order_id,created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_gd_delivery_operator_actions_leg "
            "ON gd_delivery_operator_actions(leg_id,created_at)"
        )
        connection.execute(
            "INSERT INTO gd_schema_migrations(migration_id) VALUES(?)",
            (OPERATOR_RECOVERY_AUDIT_MIGRATION_ID,),
        )
        violations = connection.execute(
            "PRAGMA foreign_key_check(gd_delivery_operator_actions)"
        ).fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"Operator recovery audit migration produced {len(violations)} foreign-key violation(s)"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return True


def apply_delivery_ledger_migrations(connection: sqlite3.Connection) -> bool:
    """Apply the canonical ledger, Playerok sync, recipe, and reservation migrations."""
    connection.create_function("gd_asset_id_key", 1, asset_id_key, deterministic=True)
    if not _migration_applied(connection, ASSET_ID_KEY_MIGRATION_ID):
        _preflight_asset_id_alias_conflicts(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise sqlite3.OperationalError("Foreign-key enforcement is required for the delivery ledger")
    changed = False
    if not _migration_applied(connection):
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            _backfill_core_orders(connection)
            _backfill_delivery_orders(connection)
            _backfill_legacy_reservations(connection)
            connection.execute("INSERT INTO gd_schema_migrations (migration_id) VALUES (?)", (MIGRATION_ID,))
            violations = []
            for table in (
                "gd_config_versions", "gd_deals", "gd_orders", "gd_order_lines",
                "gd_delivery_legs", "gd_delivery_attempts", "gd_resource_reservations",
                "gd_legacy_order_map",
            ):
                violations.extend(connection.execute(f"PRAGMA foreign_key_check({table})").fetchall())
            if violations:
                raise sqlite3.IntegrityError(
                    f"Delivery ledger migration created FK violations: {len(violations)}"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        changed = True
    changed = _apply_playerok_sync_migration(connection) or changed
    changed = _apply_recipe_migration(connection) or changed
    changed = _apply_order_line_config_migration(connection) or changed
    changed = _apply_unified_reservation_migration(connection) or changed
    changed = _apply_asset_id_key_migration(connection) or changed
    changed = _apply_asset_id_blocking_state_migration(connection) or changed
    changed = _apply_recipient_snapshot_migration(connection) or changed
    return _apply_operator_recovery_audit_migration(connection) or changed


def _preflight_asset_id_alias_conflicts(connection: sqlite3.Connection) -> None:
    """Inspect existing reservations before earlier migrations can backfill them."""
    conflicts: set[tuple[str, str, str]] = set()
    canonical_rows = []
    if _table_exists(connection, "gd_resource_reservations"):
        columns = _columns(connection, "gd_resource_reservations")
        if {"bot_name", "asset_id", "state"}.issubset(columns):
            canonical_rows = connection.execute(
                "SELECT rowid,bot_name,asset_id,state,owner_type,legacy_owner_id,order_line_id "
                "FROM gd_resource_reservations WHERE state IN ('active','legacy_conflict')"
            ).fetchall()
    legacy_rows = []
    if _table_exists(connection, "steam_asset_reservations"):
        columns = _columns(connection, "steam_asset_reservations")
        if {"bot_name", "asset_id", "order_id"}.issubset(columns):
            legacy_rows = connection.execute(
                "SELECT rowid,bot_name,asset_id,order_id FROM steam_asset_reservations"
            ).fetchall()

    grouped: dict[tuple[str, str, str], list[object]] = {}
    for table, rows in (("gd_resource_reservations", canonical_rows), ("steam_asset_reservations", legacy_rows)):
        for row in rows:
            try:
                key = asset_id_key(row["asset_id"])
            except ValueError as exc:
                raise sqlite3.IntegrityError(
                    f"{table} contains an invalid asset_id; reservation migration stopped before changes"
                ) from exc
            bot = str(row["bot_name"] or "").strip()
            if not bot:
                raise sqlite3.IntegrityError(
                    f"{table} contains an empty bot_name; reservation migration stopped before changes"
                )
            grouped.setdefault((table, bot.casefold(), key), []).append(row)
    for (table, bot, key), rows in grouped.items():
        if len(rows) > 1:
            conflicts.add((table, bot, key))

    # A pre-existing canonical/projection pair is accepted only when source ID
    # and owner links prove it is the same record. Otherwise a previous
    # migration must not materialize an additional owner before conflict review.
    for legacy in legacy_rows:
        key = asset_id_key(legacy["asset_id"])
        bot = str(legacy["bot_name"] or "").strip()
        order_id = str(legacy["order_id"] or "")
        matches = [
            row for row in canonical_rows
            if str(row["bot_name"] or "").casefold() == bot.casefold()
            and asset_id_key(row["asset_id"]) == key
        ]
        if not matches:
            continue  # An unbackfilled legacy row will be imported by migration.
        allowed = False
        if len(matches) == 1:
            candidate = matches[0]
            if candidate["state"] == "active" and candidate["asset_id"] == legacy["asset_id"]:
                allowed = (
                    candidate["owner_type"] == "legacy"
                    and candidate["legacy_owner_id"] == f"steam_asset_reservations:{order_id}"
                )
                if not allowed and candidate["owner_type"] == "order" and candidate["order_line_id"]:
                    if _table_exists(connection, "gd_order_lines"):
                        linked = connection.execute(
                            "SELECT order_id FROM gd_order_lines WHERE order_line_id=?",
                            (candidate["order_line_id"],),
                        ).fetchone()
                        if linked:
                            canonical_order = str(linked[0])
                            allowed = canonical_order == order_id
                            if not allowed and _table_exists(connection, "gd_legacy_order_map"):
                                allowed = connection.execute(
                                    "SELECT 1 FROM gd_legacy_order_map WHERE legacy_table='delivery_orders' "
                                    "AND legacy_order_id=? AND order_id=?",
                                    (order_id, canonical_order),
                                ).fetchone() is not None
        if matches and not allowed:
            conflicts.add(("cross_table", bot.casefold(), key))

    if conflicts:
        markers = sorted({
            hashlib.sha256((table + "|" + bot + "|" + key).encode("utf-8")).hexdigest()[:10]
            for table, bot, key in conflicts
        })
        raise sqlite3.IntegrityError(
            "Asset ID alias conflicts prevent reservation migration: "
            f"{len(conflicts)} blocking group(s); safe markers: {', '.join(markers[:8]) or 'unavailable'}. "
            "No migration changes were made. Resolve owners manually and retry."
        )


def _apply_asset_id_key_migration(connection: sqlite3.Connection) -> bool:
    """Add collision-safe comparison keys without rewriting stored asset IDs."""
    if _migration_applied(connection, ASSET_ID_KEY_MIGRATION_ID):
        return False
    connection.execute("BEGIN IMMEDIATE")
    try:
        for table in ("gd_resource_reservations", "steam_asset_reservations"):
            columns = _columns(connection, table)
            if "asset_id" not in columns:
                raise sqlite3.IntegrityError(f"{table} has no asset_id column")
            if "asset_id_key" not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN asset_id_key TEXT"
                )

        # Validate and compute all keys before changing rows. Collision reports
        # expose only deterministic short fingerprints, never raw account data.
        keyed: dict[str, list[tuple[str, str, str, str]]] = {}
        for table, states in (
            ("gd_resource_reservations", ("active", "legacy_conflict")),
            ("steam_asset_reservations", ()),
        ):
            rows = connection.execute(
                f"SELECT rowid,bot_name,asset_id,{('legacy_owner_id,owner_type,state' if table == 'gd_resource_reservations' else 'order_id')} "
                f"FROM {table} " + ("WHERE state IN ('active','legacy_conflict') " if states else "")
            ).fetchall()
            for row in rows:
                try:
                    key = asset_id_key(row["asset_id"])
                except ValueError as exc:
                    raise sqlite3.IntegrityError(
                        f"{table} contains an invalid asset_id; key migration rolled back"
                    ) from exc
                bot = str(row["bot_name"] or "").strip()
                if not bot:
                    raise sqlite3.IntegrityError(f"{table} contains an empty bot_name")
                owner = str(row["legacy_owner_id"] or "") if table == "gd_resource_reservations" else str(row["order_id"] or "")
                owner_type = str(row["owner_type"] or "") if table == "gd_resource_reservations" else ""
                state = str(row["state"] or "") if table == "gd_resource_reservations" else "active"
                keyed.setdefault(table, []).append((str(row["rowid"]), bot, key, owner + "|" + owner_type + "|" + state))

        conflicts: list[tuple[str, str, str]] = []
        # More than one blocking canonical row for a key cannot be collapsed.
        for table, values in keyed.items():
            groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
            for rowid, bot, key, owner_state in values:
                groups.setdefault((bot.casefold(), key), []).append((rowid, owner_state))
            for (bot_key, key), group in groups.items():
                if len(group) > 1:
                    conflicts.append((table, bot_key, key))

        # Every legacy projection must match exactly one active canonical row
        # by key, raw source ID, and explicit owner relationship. Alias-only
        # agreement is insufficient to rewrite or merge persisted identities.
        legacy_values = keyed.get("steam_asset_reservations", [])
        for rowid, bot, key, legacy_owner in legacy_values:
            order_id = legacy_owner.rsplit("|", 2)[0]
            source_id = connection.execute(
                "SELECT asset_id FROM steam_asset_reservations WHERE rowid=?", (rowid,)
            ).fetchone()[0]
            matches = connection.execute("""
                SELECT r.asset_id,r.state,r.owner_type,r.legacy_owner_id,r.order_line_id
                FROM gd_resource_reservations r
                WHERE r.bot_name=? COLLATE NOCASE AND gd_asset_id_key(r.asset_id)=?
                  AND r.state IN ('active','legacy_conflict')
            """, (bot, key)).fetchall()
            owner_matches = []
            for candidate in matches:
                same_legacy = (
                    candidate["owner_type"] == "legacy"
                    and candidate["legacy_owner_id"] == f"steam_asset_reservations:{order_id}"
                )
                same_order = False
                if candidate["owner_type"] == "order" and candidate["order_line_id"]:
                    same_order = connection.execute("""
                        SELECT 1 FROM gd_order_lines ol
                        WHERE ol.order_line_id=? AND (
                            ol.order_id=? OR EXISTS (
                                SELECT 1 FROM gd_legacy_order_map m
                                WHERE m.legacy_table='delivery_orders' AND m.legacy_order_id=?
                                  AND m.order_id=ol.order_id
                            )
                        ) LIMIT 1
                    """, (candidate["order_line_id"], order_id, order_id)).fetchone() is not None
                owner_matches.append((candidate, same_legacy or same_order))
            if (
                len(owner_matches) != 1
                or not owner_matches[0][1]
                or owner_matches[0][0]["state"] != "active"
                or owner_matches[0][0]["asset_id"] != source_id
            ):
                conflicts.append(("cross_table", bot.casefold(), key))

        if conflicts:
            markers = sorted({
                hashlib.sha256((table + "|" + bot + "|" + key).encode("utf-8")).hexdigest()[:10]
                for table, bot, key in conflicts
            })
            raise sqlite3.IntegrityError(
                "Asset ID alias conflicts prevent migration: "
                f"{len(conflicts)} blocking group(s); safe markers: {', '.join(markers[:8]) or 'unavailable'}. "
                "Resolve reservations manually; no rows were merged or changed."
            )

        for table in ("gd_resource_reservations", "steam_asset_reservations"):
            rows = connection.execute(f"SELECT rowid,asset_id FROM {table}").fetchall()
            for row in rows:
                connection.execute(
                    f"UPDATE {table} SET asset_id_key=? WHERE rowid=?",
                    (asset_id_key(row["asset_id"]), row["rowid"]),
                )

        connection.execute("DROP INDEX IF EXISTS idx_gd_resource_active_global")
        connection.execute("DROP INDEX IF EXISTS idx_gd_resource_active_asset_key")
        connection.execute("DROP INDEX IF EXISTS idx_steam_reservation_asset_key")
        connection.execute("""
            CREATE UNIQUE INDEX idx_gd_resource_active_asset_key
            ON gd_resource_reservations(bot_name COLLATE NOCASE,asset_id_key)
            WHERE state='active'
        """)
        connection.execute("""
            CREATE UNIQUE INDEX idx_steam_reservation_asset_key
            ON steam_asset_reservations(bot_name COLLATE NOCASE,asset_id_key)
        """)

        for trigger in (
            "gd_asset_reservations_set_key_insert", "gd_asset_reservations_set_key_update",
            "gd_asset_reservations_guard_insert", "gd_asset_reservations_guard_update",
            "steam_asset_reservations_set_key_insert", "steam_asset_reservations_set_key_update",
            "steam_asset_reservations_guard_insert", "steam_asset_reservations_to_ledger_insert",
            "steam_asset_reservations_to_ledger_delete", "gd_reservations_to_steam_compatibility_state",
            "steam_asset_reservations_guard_source_update",
            "gd_reservations_guard_projected_asset_update",
        ):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        trigger_statements = [
            """
            CREATE TRIGGER gd_asset_reservations_guard_insert
            BEFORE INSERT ON gd_resource_reservations
            WHEN NEW.state IN ('active','legacy_conflict') AND EXISTS (
                SELECT 1 FROM gd_resource_reservations r
                WHERE r.state IN ('active','legacy_conflict')
                  AND r.bot_name=NEW.bot_name COLLATE NOCASE
                  AND r.asset_id_key=gd_asset_id_key(NEW.asset_id)
            ) BEGIN SELECT RAISE(ABORT,'Resource already has a blocking reservation'); END
            """,
            """
            CREATE TRIGGER gd_asset_reservations_guard_update
            BEFORE UPDATE OF state,asset_id,bot_name ON gd_resource_reservations
            WHEN NEW.state IN ('active','legacy_conflict') AND EXISTS (
                SELECT 1 FROM gd_resource_reservations r
                WHERE r.reservation_id<>OLD.reservation_id
                  AND r.state IN ('active','legacy_conflict')
                  AND r.bot_name=NEW.bot_name COLLATE NOCASE
                  AND r.asset_id_key=gd_asset_id_key(NEW.asset_id)
            ) BEGIN SELECT RAISE(ABORT,'Resource already has a blocking reservation'); END
            """,
            """
            CREATE TRIGGER gd_asset_reservations_set_key_insert
            AFTER INSERT ON gd_resource_reservations BEGIN
                UPDATE gd_resource_reservations SET asset_id_key=gd_asset_id_key(NEW.asset_id)
                WHERE reservation_id=NEW.reservation_id;
            END
            """,
            """
            CREATE TRIGGER gd_asset_reservations_set_key_update
            AFTER UPDATE OF asset_id,asset_id_key ON gd_resource_reservations
            WHEN NEW.asset_id_key IS NOT gd_asset_id_key(NEW.asset_id) BEGIN
                UPDATE gd_resource_reservations SET asset_id_key=gd_asset_id_key(NEW.asset_id)
                WHERE reservation_id=NEW.reservation_id;
            END
            """,
            """
            CREATE TRIGGER steam_asset_reservations_set_key_insert
            AFTER INSERT ON steam_asset_reservations BEGIN
                UPDATE steam_asset_reservations SET asset_id_key=gd_asset_id_key(NEW.asset_id)
                WHERE rowid=NEW.rowid;
            END
            """,
            """
            CREATE TRIGGER steam_asset_reservations_set_key_update
            AFTER UPDATE OF asset_id,asset_id_key ON steam_asset_reservations
            WHEN NEW.asset_id_key IS NOT gd_asset_id_key(NEW.asset_id) BEGIN
                UPDATE steam_asset_reservations SET asset_id_key=gd_asset_id_key(NEW.asset_id)
                WHERE rowid=NEW.rowid;
            END
            """,
            """
            CREATE TRIGGER steam_asset_reservations_guard_insert
            BEFORE INSERT ON steam_asset_reservations
            WHEN EXISTS (
                SELECT 1 FROM gd_resource_reservations r
                WHERE r.state IN ('active','legacy_conflict') AND r.bot_name=NEW.bot_name COLLATE NOCASE
                  AND r.asset_id_key=gd_asset_id_key(NEW.asset_id)
                  AND NOT (
                    (r.owner_type='legacy' AND r.legacy_owner_id='steam_asset_reservations:'||NEW.order_id)
                    OR (r.owner_type='order' AND EXISTS (
                        SELECT 1 FROM gd_order_lines ol JOIN gd_orders o ON o.order_id=ol.order_id
                        WHERE ol.order_line_id=r.order_line_id AND o.order_id=NEW.order_id
                    ))
                    OR (r.owner_type='order' AND EXISTS (
                        SELECT 1 FROM gd_legacy_order_map m JOIN gd_order_lines ol ON ol.order_id=m.order_id
                        WHERE m.legacy_table='delivery_orders' AND m.legacy_order_id=NEW.order_id
                          AND ol.order_line_id=r.order_line_id
                    ))
                  )
            ) BEGIN SELECT RAISE(ABORT,'Resource already has a conflicting canonical reservation'); END
            """,
            """
            CREATE TRIGGER steam_asset_reservations_guard_source_update
            BEFORE UPDATE OF asset_id,order_id ON steam_asset_reservations
            WHEN EXISTS (
                SELECT 1 FROM gd_resource_reservations r
                WHERE r.bot_name=OLD.bot_name COLLATE NOCASE
                  AND r.asset_id_key=gd_asset_id_key(OLD.asset_id)
                  AND ((r.owner_type='legacy' AND r.legacy_owner_id='steam_asset_reservations:'||OLD.order_id)
                    OR (r.owner_type='order' AND EXISTS (
                        SELECT 1 FROM gd_order_lines ol JOIN gd_orders o ON o.order_id=ol.order_id
                        WHERE ol.order_line_id=r.order_line_id AND o.order_id=OLD.order_id
                    ))
                    OR (r.owner_type='order' AND EXISTS (
                        SELECT 1 FROM gd_legacy_order_map m JOIN gd_order_lines ol ON ol.order_id=m.order_id
                        WHERE m.legacy_table='delivery_orders' AND m.legacy_order_id=OLD.order_id
                          AND ol.order_line_id=r.order_line_id
                    )))
                  AND (NEW.asset_id<>r.asset_id OR NEW.order_id<>OLD.order_id)
            ) BEGIN SELECT RAISE(ABORT,'Projected reservation identity is immutable; release and reserve again'); END
            """,
            """
            CREATE TRIGGER gd_reservations_guard_projected_asset_update
            BEFORE UPDATE OF asset_id ON gd_resource_reservations
            WHEN NEW.asset_id<>OLD.asset_id AND EXISTS (
                SELECT 1 FROM steam_asset_reservations s
                WHERE s.bot_name=OLD.bot_name COLLATE NOCASE
                  AND s.asset_id_key=OLD.asset_id_key
                  AND ((OLD.owner_type='legacy' AND OLD.legacy_owner_id='steam_asset_reservations:'||s.order_id)
                    OR (OLD.owner_type='order' AND EXISTS (
                        SELECT 1 FROM gd_order_lines ol WHERE ol.order_line_id=OLD.order_line_id
                          AND (ol.order_id=s.order_id OR EXISTS (
                            SELECT 1 FROM gd_legacy_order_map m WHERE m.legacy_table='delivery_orders'
                              AND m.legacy_order_id=s.order_id AND m.order_id=ol.order_id
                          ))
                    )))
            ) BEGIN SELECT RAISE(ABORT,'Projected reservation asset_id is immutable; release and reserve again'); END
            """,
            """
            CREATE TRIGGER steam_asset_reservations_to_ledger_insert
            AFTER INSERT ON steam_asset_reservations
            WHEN NOT EXISTS (
                SELECT 1 FROM gd_resource_reservations r WHERE r.state='active'
                  AND r.bot_name=NEW.bot_name COLLATE NOCASE
                  AND r.asset_id_key=gd_asset_id_key(NEW.asset_id)
            ) BEGIN
                INSERT INTO gd_resource_reservations
                    (reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,
                     bot_name,asset_id,asset_id_key,units,state)
                VALUES('legacy-reservation:'||hex(NEW.order_id)||':'||hex(NEW.bot_name)||':'||hex(NEW.asset_id),
                    'legacy','steam_asset_reservations:'||NEW.order_id,'steam_cards',753,'6',
                    NEW.bot_name,NEW.asset_id,gd_asset_id_key(NEW.asset_id),1,'active');
            END
            """,
            """
            CREATE TRIGGER steam_asset_reservations_to_ledger_delete
            AFTER DELETE ON steam_asset_reservations BEGIN
                UPDATE gd_resource_reservations SET state='released',
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE state='active' AND bot_name=OLD.bot_name COLLATE NOCASE
                  AND asset_id_key=gd_asset_id_key(OLD.asset_id)
                  AND (
                    (owner_type='legacy' AND legacy_owner_id='steam_asset_reservations:'||OLD.order_id)
                    OR (owner_type='order' AND (
                        EXISTS (SELECT 1 FROM gd_order_lines ol
                                WHERE ol.order_line_id=gd_resource_reservations.order_line_id
                                  AND ol.order_id=OLD.order_id)
                        OR EXISTS (SELECT 1 FROM gd_legacy_order_map m JOIN gd_order_lines ol
                                   ON ol.order_id=m.order_id
                                   WHERE m.legacy_table='delivery_orders'
                                     AND m.legacy_order_id=OLD.order_id
                                     AND ol.order_line_id=gd_resource_reservations.order_line_id)
                    ))
                  );
            END
            """,
            """
            CREATE TRIGGER gd_reservations_to_steam_compatibility_state
            AFTER UPDATE OF state ON gd_resource_reservations
            WHEN OLD.state='active' AND NEW.state IN ('released','consumed') BEGIN
                DELETE FROM steam_asset_reservations WHERE bot_name=NEW.bot_name COLLATE NOCASE
                  AND asset_id_key=NEW.asset_id_key AND (
                    (NEW.owner_type='legacy' AND NEW.legacy_owner_id='steam_asset_reservations:'||order_id)
                    OR (NEW.owner_type='order' AND EXISTS (
                        SELECT 1 FROM gd_order_lines ol WHERE ol.order_line_id=NEW.order_line_id
                          AND (ol.order_id=steam_asset_reservations.order_id OR EXISTS (
                            SELECT 1 FROM gd_legacy_order_map m WHERE m.legacy_table='delivery_orders'
                              AND m.legacy_order_id=steam_asset_reservations.order_id AND m.order_id=ol.order_id
                          ))
                    ))
                  );
            END
            """,
        ]
        for statement in trigger_statements:
            connection.execute(statement)
        connection.execute("INSERT INTO gd_schema_migrations(migration_id) VALUES(?)", (ASSET_ID_KEY_MIGRATION_ID,))
        violations = connection.execute("PRAGMA foreign_key_check(gd_resource_reservations)").fetchall()
        if violations:
            raise sqlite3.IntegrityError(f"Asset ID key migration produced {len(violations)} foreign-key violation(s)")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return True


def _apply_asset_id_blocking_state_migration(connection: sqlite3.Connection) -> bool:
    """Upgrade key-enabled databases so every blocking state is guarded."""
    if _migration_applied(connection, ASSET_ID_BLOCKING_STATE_MIGRATION_ID):
        return False
    if not _migration_applied(connection, ASSET_ID_KEY_MIGRATION_ID):
        raise sqlite3.IntegrityError("Asset ID key migration must precede blocking-state guards")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TRIGGER IF EXISTS gd_asset_reservations_guard_insert")
        connection.execute("DROP TRIGGER IF EXISTS gd_asset_reservations_guard_update")
        connection.execute("""
            CREATE TRIGGER gd_asset_reservations_guard_insert
            BEFORE INSERT ON gd_resource_reservations
            WHEN NEW.state IN ('active','legacy_conflict') AND EXISTS (
                SELECT 1 FROM gd_resource_reservations r
                WHERE r.state IN ('active','legacy_conflict')
                  AND r.bot_name=NEW.bot_name COLLATE NOCASE
                  AND r.asset_id_key=gd_asset_id_key(NEW.asset_id)
            ) BEGIN SELECT RAISE(ABORT,'Resource already has a blocking reservation'); END
        """)
        connection.execute("""
            CREATE TRIGGER gd_asset_reservations_guard_update
            BEFORE UPDATE OF state,asset_id,bot_name ON gd_resource_reservations
            WHEN NEW.state IN ('active','legacy_conflict') AND EXISTS (
                SELECT 1 FROM gd_resource_reservations r
                WHERE r.reservation_id<>OLD.reservation_id
                  AND r.state IN ('active','legacy_conflict')
                  AND r.bot_name=NEW.bot_name COLLATE NOCASE
                  AND r.asset_id_key=gd_asset_id_key(NEW.asset_id)
            ) BEGIN SELECT RAISE(ABORT,'Resource already has a blocking reservation'); END
        """)
        connection.execute(
            "INSERT INTO gd_schema_migrations(migration_id) VALUES(?)",
            (ASSET_ID_BLOCKING_STATE_MIGRATION_ID,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return True


def _apply_unified_reservation_migration(connection: sqlite3.Connection) -> bool:
    """Make the legacy Steam reservation table a transactional compatibility projection."""
    if _migration_applied(connection, UNIFIED_RESERVATION_MIGRATION_ID):
        return False
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS steam_asset_reservations (
                bot_name TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY (bot_name, asset_id)
            )
        """)
        required = {"bot_name", "asset_id", "order_id"}
        if not required.issubset(_columns(connection, "steam_asset_reservations")):
            raise sqlite3.IntegrityError("Legacy Steam reservation table has an unsupported schema")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS gd_reservation_events (
                event_id TEXT PRIMARY KEY,
                reservation_id TEXT NOT NULL REFERENCES gd_resource_reservations(reservation_id) ON DELETE RESTRICT,
                previous_state TEXT,
                new_state TEXT NOT NULL,
                changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )
        """)
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS gd_reservation_events_after_insert
            AFTER INSERT ON gd_resource_reservations
            BEGIN
                INSERT INTO gd_reservation_events(event_id,reservation_id,previous_state,new_state)
                VALUES(lower(hex(randomblob(16))),NEW.reservation_id,NULL,NEW.state);
            END
        """)
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS gd_reservation_events_after_state_change
            AFTER UPDATE OF state ON gd_resource_reservations
            WHEN OLD.state<>NEW.state
            BEGIN
                INSERT INTO gd_reservation_events(event_id,reservation_id,previous_state,new_state)
                VALUES(lower(hex(randomblob(16))),NEW.reservation_id,OLD.state,NEW.state);
            END
        """)

        # Copy every pre-existing legacy reservation into the canonical active
        # set. If the same physical asset already belongs to a different owner,
        # retain an explicit conflict marker and keep it unavailable.
        for row in connection.execute(
            "SELECT bot_name,asset_id,order_id FROM steam_asset_reservations ORDER BY bot_name,asset_id"
        ).fetchall():
            bot = str(row["bot_name"] or "").strip()
            asset = str(row["asset_id"] or "").strip()
            owner_id = str(row["order_id"] or "")
            if not bot or not asset or not owner_id:
                raise sqlite3.IntegrityError("Legacy reservation contains an incomplete resource identity")
            active = connection.execute(
                "SELECT reservation_id,owner_type,legacy_owner_id,order_line_id "
                "FROM gd_resource_reservations WHERE state='active' "
                "AND bot_name=? COLLATE NOCASE AND asset_id=? LIMIT 1",
                (bot, asset),
            ).fetchone()
            if active is not None:
                same_legacy_owner = (
                    active["owner_type"] == "legacy"
                    and active["legacy_owner_id"] == f"steam_asset_reservations:{owner_id}"
                )
                same_order_owner = False
                if active["owner_type"] == "order" and active["order_line_id"]:
                    current_owner = connection.execute(
                        "SELECT order_id FROM gd_order_lines WHERE order_line_id=?",
                        (active["order_line_id"],),
                    ).fetchone()
                    same_order_owner = bool(
                        current_owner and (
                            str(current_owner[0]) == owner_id
                            or connection.execute(
                                "SELECT 1 FROM gd_legacy_order_map "
                                "WHERE legacy_table='delivery_orders' AND legacy_order_id=? AND order_id=?",
                                (owner_id, str(current_owner[0])),
                            ).fetchone()
                        )
                    )
                if same_legacy_owner or same_order_owner:
                    continue
                conflict_id = _stable_id(
                    "legacy-reservation-conflict", bot.casefold() + "|" + asset + "|" + owner_id
                )
                connection.execute("""
                    INSERT OR IGNORE INTO gd_resource_reservations
                        (reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,
                         bot_name,asset_id,units,state)
                    VALUES(?,'legacy',?,'steam_cards',753,'6',?,?,1,'legacy_conflict')
                """, (conflict_id, f"steam_asset_reservations:{owner_id}", bot, asset))
                continue
            reservation_id = _stable_id(
                "legacy-reservation", owner_id + "|" + bot.casefold() + "|" + asset
            )
            connection.execute("""
                INSERT INTO gd_resource_reservations
                    (reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,
                     bot_name,asset_id,units,state)
                VALUES(?,'legacy',?,'steam_cards',753,'6',?,?,1,'active')
                ON CONFLICT(reservation_id) DO UPDATE SET
                    state='active',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
            """, (reservation_id, f"steam_asset_reservations:{owner_id}", bot, asset))

        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS steam_asset_reservations_guard_insert
            BEFORE INSERT ON steam_asset_reservations
            WHEN EXISTS (
                SELECT 1 FROM gd_resource_reservations r
                WHERE r.state='active' AND r.bot_name=NEW.bot_name COLLATE NOCASE
                  AND r.asset_id=NEW.asset_id
                  AND NOT (
                    (r.owner_type='legacy' AND r.legacy_owner_id='steam_asset_reservations:'||NEW.order_id)
                    OR (r.owner_type='order' AND EXISTS (
                        SELECT 1 FROM gd_order_lines ol JOIN gd_orders o ON o.order_id=ol.order_id
                        WHERE ol.order_line_id=r.order_line_id AND o.order_id=NEW.order_id
                    ))
                    OR (r.owner_type='order' AND EXISTS (
                        SELECT 1 FROM gd_legacy_order_map m JOIN gd_order_lines ol ON ol.order_id=m.order_id
                        WHERE m.legacy_table='delivery_orders' AND m.legacy_order_id=NEW.order_id
                          AND ol.order_line_id=r.order_line_id
                    ))
                  )
            )
            BEGIN
                SELECT RAISE(ABORT,'Resource already has an active canonical reservation');
            END
        """)
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS steam_asset_reservations_to_ledger_insert
            AFTER INSERT ON steam_asset_reservations
            WHEN NOT EXISTS (
                SELECT 1 FROM gd_resource_reservations r
                WHERE r.state='active' AND r.bot_name=NEW.bot_name COLLATE NOCASE
                  AND r.asset_id=NEW.asset_id
            )
            BEGIN
                INSERT INTO gd_resource_reservations
                    (reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,
                     bot_name,asset_id,units,state)
                VALUES(
                    'legacy-reservation:'||hex(NEW.order_id)||':'||hex(NEW.bot_name)||':'||hex(NEW.asset_id),
                    'legacy','steam_asset_reservations:'||NEW.order_id,'steam_cards',753,'6',
                    NEW.bot_name,NEW.asset_id,1,'active'
                )
                ON CONFLICT(reservation_id) DO UPDATE SET
                    state='active',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now');
            END
        """)
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS steam_asset_reservations_to_ledger_delete
            AFTER DELETE ON steam_asset_reservations
            BEGIN
                UPDATE gd_resource_reservations SET state='released',
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE state='active' AND bot_name=OLD.bot_name COLLATE NOCASE AND asset_id=OLD.asset_id
                  AND (
                    (owner_type='legacy' AND legacy_owner_id='steam_asset_reservations:'||OLD.order_id)
                    OR (owner_type='order' AND (
                        EXISTS (SELECT 1 FROM gd_order_lines ol JOIN gd_orders o ON o.order_id=ol.order_id
                                WHERE ol.order_line_id=gd_resource_reservations.order_line_id
                                  AND o.order_id=OLD.order_id)
                        OR EXISTS (SELECT 1 FROM gd_legacy_order_map m JOIN gd_order_lines ol ON ol.order_id=m.order_id
                                   WHERE m.legacy_table='delivery_orders' AND m.legacy_order_id=OLD.order_id
                                     AND ol.order_line_id=gd_resource_reservations.order_line_id)
                    ))
                  );
            END
        """)
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS gd_reservations_to_steam_compatibility_state
            AFTER UPDATE OF state ON gd_resource_reservations
            WHEN OLD.state='active' AND NEW.state IN ('released','consumed')
            BEGIN
                DELETE FROM steam_asset_reservations
                WHERE bot_name=NEW.bot_name COLLATE NOCASE AND asset_id=NEW.asset_id
                  AND (
                    (NEW.owner_type='legacy' AND NEW.legacy_owner_id='steam_asset_reservations:'||order_id)
                    OR (NEW.owner_type='order' AND (
                        EXISTS (SELECT 1 FROM gd_order_lines ol JOIN gd_orders o ON o.order_id=ol.order_id
                                WHERE ol.order_line_id=NEW.order_line_id AND o.order_id=steam_asset_reservations.order_id)
                        OR EXISTS (SELECT 1 FROM gd_legacy_order_map m JOIN gd_order_lines ol ON ol.order_id=m.order_id
                                   WHERE m.legacy_table='delivery_orders' AND ol.order_line_id=NEW.order_line_id
                                     AND m.legacy_order_id=steam_asset_reservations.order_id)
                    ))
                  );
            END
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_steam_asset_reservations_order ON steam_asset_reservations(order_id)")
        connection.execute(
            "INSERT INTO gd_schema_migrations(migration_id) VALUES(?)",
            (UNIFIED_RESERVATION_MIGRATION_ID,),
        )
        violations = connection.execute("PRAGMA foreign_key_check(gd_resource_reservations)").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"Unified reservation migration produced {len(violations)} foreign-key violation(s)"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return True


def _apply_recipe_migration(connection: sqlite3.Connection) -> bool:
    """Add typed recipe columns to immutable ConfigVersion rows without rewriting them."""
    if _migration_applied(connection, RECIPE_MIGRATION_ID):
        return False
    connection.execute("BEGIN IMMEDIATE")
    try:
        columns = _columns(connection, "gd_config_versions")
        if "recipe_schema_version" not in columns:
            connection.execute(
                "ALTER TABLE gd_config_versions ADD COLUMN recipe_schema_version "
                "INTEGER CHECK (recipe_schema_version IS NULL OR recipe_schema_version > 0)"
            )
        if "recipe_items_json" not in columns:
            connection.execute(
                "ALTER TABLE gd_config_versions ADD COLUMN recipe_items_json TEXT NOT NULL DEFAULT '[]'"
            )
        connection.execute(
            "INSERT INTO gd_schema_migrations (migration_id) VALUES (?)",
            (RECIPE_MIGRATION_ID,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return True


def _apply_order_line_config_migration(connection: sqlite3.Connection) -> bool:
    """Backfill and enforce the immutable ConfigVersion owner of every OrderLine."""
    if _migration_applied(connection, ORDER_LINE_CONFIG_MIGRATION_ID):
        return False
    connection.execute("BEGIN IMMEDIATE")
    try:
        columns = _columns(connection, "gd_order_lines")
        if "config_version_id" not in columns:
            connection.execute(
                "ALTER TABLE gd_order_lines ADD COLUMN config_version_id TEXT "
                "REFERENCES gd_config_versions(config_version_id) ON DELETE RESTRICT"
            )
        connection.execute("""
            UPDATE gd_order_lines
            SET config_version_id=(
                SELECT d.config_version_id
                FROM gd_orders o JOIN gd_deals d ON d.deal_id=o.deal_id
                WHERE o.order_id=gd_order_lines.order_id
            )
            WHERE config_version_id IS NULL
        """)
        missing = connection.execute("""
            SELECT ol.order_line_id
            FROM gd_order_lines ol
            LEFT JOIN gd_orders o ON o.order_id=ol.order_id
            LEFT JOIN gd_deals d ON d.deal_id=o.deal_id
            WHERE ol.config_version_id IS NULL OR d.config_version_id IS NULL
               OR ol.config_version_id<>d.config_version_id
            LIMIT 1
        """).fetchone()
        if missing is not None:
            raise sqlite3.IntegrityError(
                "Existing OrderLine cannot be safely linked to its deal ConfigVersion"
            )
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS gd_order_lines_config_version_insert
            BEFORE INSERT ON gd_order_lines
            WHEN NEW.config_version_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM gd_orders o JOIN gd_deals d ON d.deal_id=o.deal_id
                WHERE o.order_id=NEW.order_id
                  AND d.config_version_id=NEW.config_version_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'OrderLine ConfigVersion must match its deal');
            END
        """)
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS gd_order_lines_config_version_update
            BEFORE UPDATE OF order_id,config_version_id ON gd_order_lines
            WHEN NEW.config_version_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM gd_orders o JOIN gd_deals d ON d.deal_id=o.deal_id
                WHERE o.order_id=NEW.order_id
                  AND d.config_version_id=NEW.config_version_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'OrderLine ConfigVersion must match its deal');
            END
        """)
        connection.execute(
            "INSERT INTO gd_schema_migrations (migration_id) VALUES (?)",
            (ORDER_LINE_CONFIG_MIGRATION_ID,),
        )
        violations = connection.execute("PRAGMA foreign_key_check(gd_order_lines)").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"OrderLine ConfigVersion migration produced {len(violations)} foreign-key violation(s)"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return True


def migrate_delivery_ledger(database_path: str | Path) -> bool:
    ensure_pre_migration_backup(database_path)
    ensure_pre_migration_backup(
        database_path,
        migration_id=PLAYEROK_SYNC_MIGRATION_ID,
        backup_suffix=PLAYEROK_SYNC_BACKUP_SUFFIX,
    )
    ensure_pre_migration_backup(
        database_path,
        migration_id=RECIPE_MIGRATION_ID,
        backup_suffix=RECIPE_BACKUP_SUFFIX,
    )
    ensure_pre_migration_backup(
        database_path,
        migration_id=ORDER_LINE_CONFIG_MIGRATION_ID,
        backup_suffix=ORDER_LINE_CONFIG_BACKUP_SUFFIX,
    )
    ensure_pre_migration_backup(
        database_path,
        migration_id=RECIPIENT_SNAPSHOT_MIGRATION_ID,
        backup_suffix=RECIPIENT_SNAPSHOT_BACKUP_SUFFIX,
    )
    ensure_pre_migration_backup(
        database_path,
        migration_id=OPERATOR_RECOVERY_AUDIT_MIGRATION_ID,
        backup_suffix=OPERATOR_RECOVERY_AUDIT_BACKUP_SUFFIX,
    )
    with connect_sqlite(database_path) as connection:
        return apply_delivery_ledger_migrations(connection)
