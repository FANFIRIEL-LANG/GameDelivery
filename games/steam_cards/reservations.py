from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable


class AssetReservationStore:
    """Transactional local reservations keyed by bot and asset ID; no trade sending."""

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS steam_asset_reservations (
                    bot_name TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    PRIMARY KEY (bot_name, asset_id)
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def reserve(self, order_id: str, assets: Iterable[tuple[str, str]]) -> dict[str, object]:
        order = order_id.strip()
        if not order:
            raise ValueError("order_id не должен быть пустым")
        normalized = list(dict.fromkeys((bot.strip(), str(asset).strip()) for bot, asset in assets))
        if not normalized or any(not bot or not asset for bot, asset in normalized):
            raise ValueError("Нужен непустой список корректных (bot_name, asset_id)")
        if any(not asset.isdigit() or int(asset) <= 0 for _bot, asset in normalized):
            raise ValueError("Steam Asset ID должен быть положительным целым числом")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            conflicts = []
            for bot, asset in normalized:
                row = connection.execute(
                    "SELECT order_id FROM steam_asset_reservations WHERE bot_name = ? AND asset_id = ?",
                    (bot, asset),
                ).fetchone()
                if row is not None and row["order_id"] != order:
                    conflicts.append({"bot_name": bot, "asset_id": asset, "reserved_by": row["order_id"]})
            if conflicts:
                return {"reserved": False, "order_id": order, "assets": [], "conflicts": conflicts}
            connection.executemany("""
                INSERT INTO steam_asset_reservations (bot_name, asset_id, order_id)
                VALUES (?, ?, ?)
                ON CONFLICT(bot_name, asset_id) DO NOTHING
            """, [(bot, asset, order) for bot, asset in normalized])
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
