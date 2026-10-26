from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from games.steam_cards.selection import CardCopy


class InventorySnapshotStore:
    """Persistent local inventory snapshot; never contacts Steam by itself."""

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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS steam_inventory_snapshot (
                    asset_id TEXT PRIMARY KEY,
                    game_name TEXT NOT NULL,
                    card_name TEXT NOT NULL,
                    is_trading_card INTEGER NOT NULL,
                    is_foil INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS steam_inventory_snapshot_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    updated_at TEXT NOT NULL,
                    item_count INTEGER NOT NULL
                );
                """
            )

    def replace(self, cards: Iterable[CardCopy]) -> dict[str, object]:
        """Replace the snapshot atomically after a successful explicit inventory fetch."""
        normalized: dict[str, CardCopy] = {}
        for card in cards:
            asset_id = str(card.asset_id).strip()
            game_name = card.game_name.strip()
            card_name = card.card_name.strip()
            if not asset_id or not game_name or not card_name:
                continue
            normalized.setdefault(
                asset_id,
                CardCopy(asset_id, game_name, card_name, card.is_trading_card, card.is_foil),
            )
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM steam_inventory_snapshot")
            connection.executemany(
                """
                INSERT INTO steam_inventory_snapshot
                    (asset_id, game_name, card_name, is_trading_card, is_foil)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (c.asset_id, c.game_name, c.card_name, int(c.is_trading_card), int(c.is_foil))
                    for c in normalized.values()
                ],
            )
            connection.execute(
                """
                INSERT INTO steam_inventory_snapshot_meta (id, updated_at, item_count)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    item_count = excluded.item_count
                """,
                (timestamp, len(normalized)),
            )
        return {"updated_at": timestamp, "item_count": len(normalized), "stale": False}

    def load(self) -> list[CardCopy]:
        """Return the saved snapshot without making any network requests."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT asset_id, game_name, card_name, is_trading_card, is_foil
                FROM steam_inventory_snapshot ORDER BY game_name, card_name, asset_id
                """
            ).fetchall()
        return [
            CardCopy(
                asset_id=row["asset_id"],
                game_name=row["game_name"],
                card_name=row["card_name"],
                is_trading_card=bool(row["is_trading_card"]),
                is_foil=bool(row["is_foil"]),
            )
            for row in rows
        ]

    def status(self) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT updated_at, item_count FROM steam_inventory_snapshot_meta WHERE id = 1"
            ).fetchone()
        if row is None:
            return {"updated_at": None, "item_count": 0, "stale": True}
        return {"updated_at": row["updated_at"], "item_count": row["item_count"], "stale": False}
