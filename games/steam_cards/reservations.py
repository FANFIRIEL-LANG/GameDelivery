from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from core.database.connection import connect_sqlite
from games.asset_ids import asset_id_key, validate_asset_id


class AssetReservationStore:
    """Transactional local reservations keyed by bot and asset ID; no trade sending."""

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS steam_asset_reservations (
                    bot_name TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    asset_id_key TEXT,
                    PRIMARY KEY (bot_name, asset_id)
                )
            """)
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(steam_asset_reservations)")}
            schema_ready = (
                "asset_id_key" in columns
                and connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_steam_reservation_asset_key'"
                ).fetchone() is not None
                and all(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger_name,)
                ).fetchone() is not None for trigger_name in (
                    "steam_asset_reservations_set_key_insert", "steam_asset_reservations_set_key_update",
                ))
            )
            if schema_ready:
                connection.commit()
                return
            if "asset_id_key" not in columns:
                connection.execute(
                    "ALTER TABLE steam_asset_reservations ADD COLUMN asset_id_key TEXT"
                )
            rows = connection.execute("SELECT rowid,bot_name,asset_id FROM steam_asset_reservations").fetchall()
            seen_keys: set[tuple[str, str]] = set()
            for row in rows:
                key = (str(row["bot_name"]).casefold(), asset_id_key(row["asset_id"]))
                if key in seen_keys:
                    raise sqlite3.IntegrityError("Legacy reservation table contains asset ID alias conflicts")
                seen_keys.add(key)
                connection.execute(
                    "UPDATE steam_asset_reservations SET asset_id_key=? WHERE rowid=?",
                    (key[1], row["rowid"]),
                )
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_steam_reservation_asset_key
                ON steam_asset_reservations(bot_name COLLATE NOCASE,asset_id_key)
            """)
            connection.execute("DROP TRIGGER IF EXISTS steam_asset_reservations_set_key_insert")
            connection.execute("DROP TRIGGER IF EXISTS steam_asset_reservations_set_key_update")
            connection.execute("""
                CREATE TRIGGER steam_asset_reservations_set_key_insert AFTER INSERT ON steam_asset_reservations BEGIN
                    UPDATE steam_asset_reservations SET asset_id_key=gd_asset_id_key(NEW.asset_id) WHERE rowid=NEW.rowid;
                END
            """)
            connection.execute("""
                CREATE TRIGGER steam_asset_reservations_set_key_update
                AFTER UPDATE OF asset_id,asset_id_key ON steam_asset_reservations
                WHEN NEW.asset_id_key IS NOT gd_asset_id_key(NEW.asset_id) BEGIN
                    UPDATE steam_asset_reservations SET asset_id_key=gd_asset_id_key(NEW.asset_id) WHERE rowid=NEW.rowid;
                END
            """)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.database_path)

    def reserve(self, order_id: str, assets: Iterable[tuple[str, str]]) -> dict[str, object]:
        order = order_id.strip()
        if not order:
            raise ValueError("order_id не должен быть пустым")
        normalized: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw_bot, raw_asset in assets:
            bot = raw_bot.strip()
            try:
                asset = validate_asset_id(raw_asset)
            except ValueError as exc:
                raise ValueError("Нужен непустой список корректных (bot_name, asset_id)") from exc
            key = (bot.casefold(), asset_id_key(asset))
            if key not in seen:
                seen.add(key)
                normalized.append((bot, asset))
        if not normalized or any(not bot or not asset for bot, asset in normalized):
            raise ValueError("Нужен непустой список корректных (bot_name, asset_id)")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            canonical_order = self._canonical_order_id(connection, order)
            canonical_for_order = self._canonical_assets_for_order(connection, canonical_order)
            requested_keys = {(bot.casefold(), asset_id_key(asset)) for bot, asset in normalized}
            canonical_keys = {(bot.casefold(), asset_id_key(asset)) for bot, asset in canonical_for_order}
            if canonical_keys and canonical_keys != requested_keys:
                return {
                    "reserved": False,
                    "order_id": order,
                    "assets": [],
                    "conflicts": [
                        {"bot_name": bot, "asset_id": asset, "reserved_by": order}
                        for bot, asset in canonical_for_order
                    ],
                }
            if canonical_keys and {
                (bot.casefold(), asset)
                for bot, asset in canonical_for_order
            } != {(bot.casefold(), asset) for bot, asset in normalized}:
                raise ValueError("Повторный alias Asset ID не совпадает с сохранённым manifest")
            conflicts = []
            matches_by_asset: dict[tuple[str, str], list[sqlite3.Row]] = {}
            for bot, asset in normalized:
                matches = [
                    row for row in connection.execute(
                        "SELECT bot_name, asset_id, order_id FROM steam_asset_reservations WHERE asset_id_key = ?",
                        (asset_id_key(asset),),
                    ).fetchall()
                    if str(row["bot_name"]).casefold() == bot.casefold()
                ]
                matches_by_asset[(bot.casefold(), asset_id_key(asset))] = matches
                if any(
                    self._canonical_order_id(connection, str(row["order_id"])).casefold()
                    == canonical_order.casefold()
                    and str(row["asset_id"]) != asset
                    for row in matches
                ) and self._manifest_has_different_alias(connection, canonical_order, bot, asset):
                    raise ValueError("Повторный alias Asset ID не совпадает с сохранённым manifest")
                other_orders = sorted({
                    self._canonical_order_id(connection, str(row["order_id"]))
                    for row in matches
                    if self._canonical_order_id(connection, str(row["order_id"])).casefold()
                    != canonical_order.casefold()
                })
                canonical_owners = self._canonical_owners(connection, bot, asset)
                other_orders.extend(
                    owner for owner in canonical_owners
                    if owner.casefold() != canonical_order.casefold()
                )
                other_orders = sorted(set(other_orders), key=str.casefold)
                if other_orders:
                    conflicts.append({
                        "bot_name": bot,
                        "asset_id": asset,
                        "reserved_by": ", ".join(other_orders),
                    })
            if conflicts:
                return {"reserved": False, "order_id": order, "assets": [], "conflicts": conflicts}

            # ASF bot names are matched case-insensitively. Collapse any legacy
            # case variants for this order, then persist one row with the latest
            # display spelling so order assets and reservations remain comparable.
            for bot, asset in normalized:
                matches = matches_by_asset[(bot.casefold(), asset_id_key(asset))]
                if matches and all(
                    str(row["bot_name"]).casefold() == bot.casefold()
                    and asset_id_key(row["asset_id"]) == asset_id_key(asset)
                    for row in matches
                ):
                    # Re-reserving the exact legacy set must not fire delete
                    # triggers and transiently release a canonical mirror.
                    if any(str(row["bot_name"]) != bot for row in matches):
                        connection.execute(
                            "UPDATE steam_asset_reservations SET bot_name=? "
                            "WHERE asset_id_key=? AND order_id=? AND bot_name=? COLLATE NOCASE",
                            (bot, asset_id_key(asset), order, matches[0]["bot_name"]),
                        )
                    continue
                for row in matches:
                    connection.execute(
                        "DELETE FROM steam_asset_reservations "
                        "WHERE bot_name = ? AND asset_id_key = ? AND order_id = ?",
                        (row["bot_name"], asset_id_key(asset), order),
                    )
                connection.execute(
                    "INSERT INTO steam_asset_reservations (bot_name, asset_id, order_id) "
                    "VALUES (?, ?, ?)",
                    (bot, asset, order),
                )
        return {"reserved": True, "order_id": order, "assets": normalized, "conflicts": []}

    def release(self, order_id: str) -> int:
        order = order_id.strip()
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM steam_asset_reservations WHERE order_id = ?", (order,))
            return cursor.rowcount

    def list_for_order(self, order_id: str) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT bot_name, asset_id FROM steam_asset_reservations
                WHERE order_id = ? ORDER BY bot_name, asset_id
            """, (order_id.strip(),)).fetchall()
        return [{"bot_name": row["bot_name"], "asset_id": row["asset_id"]} for row in rows]

    def list_reserved_assets(
        self, *, excluding_order_id: str | None = None
    ) -> tuple[tuple[str, str], ...]:
        """Read the canonical active set plus legacy rows during migration."""
        excluded_order = excluding_order_id.strip() if excluding_order_id else None
        with self._connect() as connection:
            if excluded_order is not None:
                excluded_order = self._canonical_order_id(connection, excluded_order)
            rows = list(connection.execute(
                "SELECT bot_name, asset_id, order_id FROM steam_asset_reservations "
                "ORDER BY bot_name, asset_id"
            ).fetchall())
            rows = [
                {
                    "bot_name": row["bot_name"],
                    "asset_id": row["asset_id"],
                    "order_id": self._canonical_order_id(connection, str(row["order_id"])),
                }
                for row in rows
            ]
            if self._table_exists(connection, "gd_resource_reservations"):
                canonical = connection.execute(
                    "SELECT bot_name,asset_id,owner_type,legacy_owner_id,order_line_id "
                    "FROM gd_resource_reservations WHERE state IN ('active','legacy_conflict')"
                ).fetchall()
                for row in canonical:
                    owner = self._canonical_owner_order(connection, row)
                    if excluded_order is None or owner is None or owner.casefold() != excluded_order.casefold():
                        rows.append({"bot_name": row["bot_name"], "asset_id": row["asset_id"], "order_id": owner or ""})
        result: dict[tuple[str, str], tuple[str, str]] = {}
        for row in rows:
            if excluded_order is not None and str(row["order_id"]).casefold() == excluded_order.casefold():
                continue
            bot, asset = str(row["bot_name"]), str(row["asset_id"])
            result.setdefault((bot.casefold(), asset_id_key(asset)), (bot, asset))
        return tuple(result[key] for key in sorted(result))

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    @classmethod
    def _manifest_has_different_alias(
        cls, connection: sqlite3.Connection, order_id: str, bot: str, asset: str
    ) -> bool:
        if not cls._table_exists(connection, "delivery_order_assets"):
            return False
        rows = connection.execute(
            "SELECT bot_name,asset_id FROM delivery_order_assets WHERE order_id=?",
            (order_id,),
        ).fetchall()
        return any(
            str(row["bot_name"]).casefold() == bot.casefold()
            and asset_id_key(row["asset_id"]) == asset_id_key(asset)
            and str(row["asset_id"]) != asset
            for row in rows
        )

    @classmethod
    def _canonical_owner_order(cls, connection: sqlite3.Connection, row) -> str | None:
        if row["owner_type"] == "order" and row["order_line_id"]:
            owner = connection.execute(
                "SELECT order_id FROM gd_order_lines WHERE order_line_id=?",
                (row["order_line_id"],),
            ).fetchone()
            return str(owner[0]) if owner else None
        if row["owner_type"] == "legacy":
            value = str(row["legacy_owner_id"] or "")
            prefix = "steam_asset_reservations:"
            if value.startswith(prefix):
                return cls._canonical_order_id(connection, value[len(prefix):])
        return None

    @classmethod
    def _canonical_order_id(cls, connection: sqlite3.Connection, order_id: str) -> str:
        """Resolve only explicit legacy delivery_orders mappings; leave other IDs intact."""
        if not cls._table_exists(connection, "gd_legacy_order_map"):
            return order_id
        row = connection.execute(
            "SELECT order_id FROM gd_legacy_order_map "
            "WHERE legacy_table='delivery_orders' AND legacy_order_id=?",
            (order_id,),
        ).fetchone()
        return str(row[0]) if row is not None else order_id

    @classmethod
    def _canonical_owners(cls, connection: sqlite3.Connection, bot: str, asset: str) -> list[str]:
        if not cls._table_exists(connection, "gd_resource_reservations"):
            return []
        rows = connection.execute(
            "SELECT bot_name,asset_id,owner_type,legacy_owner_id,order_line_id,state "
            "FROM gd_resource_reservations WHERE state IN ('active','legacy_conflict') "
            "AND bot_name=? COLLATE NOCASE AND asset_id_key=?",
            (bot, asset_id_key(asset)),
        ).fetchall()
        return [
            "<blocking legacy_conflict>"
            if row["state"] == "legacy_conflict"
            else cls._canonical_owner_order(connection, row) or "<canonical reservation>"
            for row in rows
        ]

    @classmethod
    def _canonical_assets_for_order(cls, connection: sqlite3.Connection, order_id: str) -> set[tuple[str, str]]:
        if not cls._table_exists(connection, "gd_resource_reservations"):
            return set()
        rows = connection.execute("""
            SELECT r.bot_name,r.asset_id,r.owner_type,r.legacy_owner_id,r.order_line_id
            FROM gd_resource_reservations r
            WHERE r.state='active' AND (
                (r.owner_type='legacy' AND r.legacy_owner_id=?)
                OR (r.owner_type='order' AND EXISTS (
                    SELECT 1 FROM gd_order_lines ol
                    WHERE ol.order_line_id=r.order_line_id AND ol.order_id=?
                ))
            )
        """, (f"steam_asset_reservations:{order_id}", order_id)).fetchall()
        return {(str(row["bot_name"]), str(row["asset_id"])) for row in rows}
