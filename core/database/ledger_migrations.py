from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from core.database.connection import connect_sqlite


MIGRATION_ID = "2026-11-delivery-ledger-v1"
BACKUP_SUFFIX = ".pre-gamedelivery-ledger-v1.bak"


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


def _migration_applied(connection: sqlite3.Connection) -> bool:
    if not _table_exists(connection, "gd_schema_migrations"):
        return False
    return connection.execute(
        "SELECT 1 FROM gd_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)
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


def ensure_pre_migration_backup(database_path: str | Path) -> Path | None:
    """Create one online backup before migration, with token-bearing fields redacted."""
    path = Path(database_path)
    if str(database_path) == ":memory:" or not path.exists() or path.stat().st_size == 0:
        return None
    backup_path = Path(str(path) + BACKUP_SUFFIX)
    if backup_path.exists():
        return backup_path
    temporary_path = Path(str(backup_path) + ".tmp")
    try:
        with connect_sqlite(path) as source:
            if _migration_applied(source):
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


def apply_delivery_ledger_migrations(connection: sqlite3.Connection) -> bool:
    """Apply the one-time additive v1 ledger migration to an open DB."""
    connection.execute("PRAGMA foreign_keys = ON")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise sqlite3.OperationalError("Foreign-key enforcement is required for the delivery ledger")
    if _migration_applied(connection):
        return False
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
    return True


def migrate_delivery_ledger(database_path: str | Path) -> bool:
    ensure_pre_migration_backup(database_path)
    with connect_sqlite(database_path) as connection:
        return apply_delivery_ledger_migrations(connection)
