from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from games.steam_cards.selection import CardCopy


class InventorySnapshotStore:
    """Per-bot local inventory snapshots; never contacts Steam by itself."""

    LEGACY_BOT = "legacy"
    MAX_AGE = timedelta(hours=24)

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(steam_inventory_snapshot)"
                ).fetchall()
            }
            if columns and "bot_name" not in columns:
                connection.execute("ALTER TABLE steam_inventory_snapshot RENAME TO steam_inventory_snapshot_legacy")
                connection.execute("""
                    CREATE TABLE steam_inventory_snapshot (
                        bot_name TEXT NOT NULL,
                        asset_id TEXT NOT NULL,
                        game_name TEXT NOT NULL,
                        card_name TEXT NOT NULL,
                        is_trading_card INTEGER NOT NULL,
                        is_foil INTEGER NOT NULL,
                        is_tradable INTEGER,
                        icon_url TEXT NOT NULL DEFAULT '',
                        icon_url_large TEXT NOT NULL DEFAULT '',
                        market_hash_name TEXT NOT NULL DEFAULT '',
                        item_quality TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (bot_name, asset_id)
                    )
                """)
                connection.execute("""
                    INSERT INTO steam_inventory_snapshot
                        (bot_name, asset_id, game_name, card_name, is_trading_card, is_foil)
                    SELECT ?, asset_id, game_name, card_name, is_trading_card, is_foil
                    FROM steam_inventory_snapshot_legacy
                """, (self.LEGACY_BOT,))
                connection.execute("DROP TABLE steam_inventory_snapshot_legacy")
            else:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS steam_inventory_snapshot (
                        bot_name TEXT NOT NULL,
                        asset_id TEXT NOT NULL,
                        game_name TEXT NOT NULL,
                        card_name TEXT NOT NULL,
                        is_trading_card INTEGER NOT NULL,
                        is_foil INTEGER NOT NULL,
                        is_tradable INTEGER,
                        PRIMARY KEY (bot_name, asset_id)
                    )
                """)

            snapshot_columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(steam_inventory_snapshot)"
                ).fetchall()
            }
            if "is_tradable" not in snapshot_columns:
                connection.execute(
                    "ALTER TABLE steam_inventory_snapshot ADD COLUMN is_tradable INTEGER"
                )
            for column in ("icon_url", "icon_url_large", "market_hash_name", "item_quality"):
                if column not in snapshot_columns:
                    connection.execute(
                        f"ALTER TABLE steam_inventory_snapshot ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )

            meta_columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(steam_inventory_snapshot_meta)"
                ).fetchall()
            }
            if meta_columns and "bot_name" not in meta_columns:
                connection.execute("ALTER TABLE steam_inventory_snapshot_meta RENAME TO steam_inventory_snapshot_meta_legacy")
                connection.execute("""
                    CREATE TABLE steam_inventory_snapshot_meta (
                        bot_name TEXT PRIMARY KEY,
                        updated_at TEXT NOT NULL,
                        item_count INTEGER NOT NULL
                    )
                """)
                connection.execute("""
                    INSERT INTO steam_inventory_snapshot_meta (bot_name, updated_at, item_count)
                    SELECT ?, updated_at, item_count FROM steam_inventory_snapshot_meta_legacy
                """, (self.LEGACY_BOT,))
                connection.execute("DROP TABLE steam_inventory_snapshot_meta_legacy")
            else:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS steam_inventory_snapshot_meta (
                        bot_name TEXT PRIMARY KEY,
                        updated_at TEXT NOT NULL,
                        item_count INTEGER NOT NULL
                    )
                """)

    @staticmethod
    def _normalize(cards: Iterable[CardCopy]) -> dict[str, CardCopy]:
        normalized: dict[str, CardCopy] = {}
        for card in cards:
            asset_id = str(card.asset_id).strip()
            game_name = card.game_name.strip()
            card_name = card.card_name.strip()
            if not asset_id or not game_name or not card_name:
                continue
            normalized.setdefault(
                asset_id,
                CardCopy(
                    asset_id, game_name, card_name,
                    card.is_trading_card, card.is_foil, card.is_tradable,
                    card.icon_url, card.icon_url_large, card.market_hash_name, card.item_quality,
                ),
            )
        return normalized

    def replace(self, cards: Iterable[CardCopy], bot_name: str = "default") -> dict[str, object]:
        """Atomically replace one bot's snapshot after an explicit successful fetch."""
        bot = bot_name.strip()
        if not bot:
            raise ValueError("bot_name не должен быть пустым")
        normalized = self._normalize(cards)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM steam_inventory_snapshot WHERE bot_name = ?", (bot,))
            connection.executemany("""
                INSERT INTO steam_inventory_snapshot
                    (bot_name, asset_id, game_name, card_name, is_trading_card, is_foil, is_tradable,
                     icon_url, icon_url_large, market_hash_name, item_quality)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (
                    bot, c.asset_id, c.game_name, c.card_name,
                    int(c.is_trading_card), int(c.is_foil),
                    None if c.is_tradable is None else int(c.is_tradable),
                    c.icon_url, c.icon_url_large, c.market_hash_name, c.item_quality,
                )
                for c in normalized.values()
            ])
            connection.execute("""
                INSERT INTO steam_inventory_snapshot_meta (bot_name, updated_at, item_count)
                VALUES (?, ?, ?)
                ON CONFLICT(bot_name) DO UPDATE SET
                    updated_at = excluded.updated_at, item_count = excluded.item_count
            """, (bot, timestamp, len(normalized)))
        return {
            "bot_name": bot,
            "updated_at": timestamp,
            "item_count": len(normalized),
            "stale": not normalized,
        }

    def load(self, bot_name: str = "default") -> list[CardCopy]:
        bot = bot_name.strip()
        if not bot:
            raise ValueError("bot_name не должен быть пустым")
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT asset_id, game_name, card_name, is_trading_card, is_foil, is_tradable,
                       icon_url, icon_url_large, market_hash_name, item_quality
                FROM steam_inventory_snapshot WHERE bot_name = ?
                ORDER BY game_name, card_name, asset_id
            """, (bot,)).fetchall()
        return [CardCopy(
            asset_id=row["asset_id"], game_name=row["game_name"], card_name=row["card_name"],
            is_trading_card=bool(row["is_trading_card"]), is_foil=bool(row["is_foil"]),
            is_tradable=None if row["is_tradable"] is None else bool(row["is_tradable"]),
            icon_url=row["icon_url"], icon_url_large=row["icon_url_large"],
            market_hash_name=row["market_hash_name"], item_quality=row["item_quality"],
        ) for row in rows]

    def load_all(self) -> dict[str, list[CardCopy]]:
        with self._connect() as connection:
            bots = [row["bot_name"] for row in connection.execute(
                "SELECT bot_name FROM steam_inventory_snapshot_meta ORDER BY bot_name"
            ).fetchall()]
        return {bot: self.load(bot) for bot in bots}

    def status(self, bot_name: str = "default") -> dict[str, object]:
        bot = bot_name.strip()
        with self._connect() as connection:
            row = connection.execute("""
                SELECT updated_at, item_count FROM steam_inventory_snapshot_meta WHERE bot_name = ?
            """, (bot,)).fetchone()
            actual_count = connection.execute(
                "SELECT COUNT(*) FROM steam_inventory_snapshot WHERE bot_name = ?",
                (bot,),
            ).fetchone()[0]
        if row is None:
            return {"updated_at": None, "item_count": 0, "stale": True}

        try:
            updated_at = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00"))
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)
            timestamp_fresh = timedelta(0) <= age <= self.MAX_AGE
        except (TypeError, ValueError, OverflowError):
            timestamp_fresh = False

        item_count = int(row["item_count"])
        return {
            "updated_at": row["updated_at"],
            "item_count": item_count,
            "stale": (
                item_count <= 0
                or actual_count != item_count
                or not timestamp_fresh
            ),
        }
