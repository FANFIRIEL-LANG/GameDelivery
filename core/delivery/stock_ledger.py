from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.database.db import Database
from games.asset_ids import asset_id_key, validate_asset_id


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} должен быть положительным целым числом")
    return value


@dataclass(frozen=True)
class StockResource:
    game_id: str
    app_id: int
    context_id: str
    bot_name: str
    asset_id: str

    def normalized(self) -> "StockResource":
        game = str(self.game_id or "").strip()
        context = str(self.context_id or "").strip()
        bot = str(self.bot_name or "").strip()
        asset = validate_asset_id(self.asset_id)
        if not game or not context or not bot or not asset:
            raise ValueError("Полный физический ключ ресурса обязателен")
        app = _positive_int(self.app_id, "app_id")
        return StockResource(game, app, context, bot, asset)


class QuantityStockLedger:
    """Transactional stock observations and quantity allocations; never sends trades."""

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _observation_id(resource: StockResource) -> str:
        key = "|".join((
            resource.game_id, str(resource.app_id), resource.context_id,
            resource.bot_name.casefold(), asset_id_key(resource.asset_id),
        ))
        return "stock:" + hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def _blocking_units(connection: sqlite3.Connection, observation_id: str, revision: int) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations
            WHERE observation_id=? AND (
                state IN ('active','pending','unknown','legacy_conflict')
                OR (state='consumed' AND consumed_revision>=?)
            )
            """,
            (observation_id, revision),
        ).fetchone()
        return int(row[0])

    def observe(
        self, resource: StockResource, amount: int, *, observed_at: str | None = None
    ) -> dict[str, Any]:
        resource = resource.normalized()
        amount = _positive_int(amount, "amount")
        timestamp = observed_at or datetime.now(timezone.utc).isoformat()
        observation_id = self._observation_id(resource)
        canonical_key = asset_id_key(resource.asset_id)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT revision FROM gd_stock_observations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            revision = int(previous["revision"]) + 1 if previous else 1
            blocking_units = self._blocking_units(connection, observation_id, revision)
            legacy = connection.execute(
                """
                SELECT 1 FROM gd_resource_reservations
                WHERE state IN ('active','legacy_conflict')
                  AND game_id=? AND app_id=? AND context_id=?
                  AND bot_name=? COLLATE NOCASE AND asset_id_key=?
                UNION ALL
                SELECT 1 FROM steam_asset_reservations
                WHERE ?='steam_cards' AND ?=753 AND ?='6'
                  AND bot_name=? COLLATE NOCASE AND gd_asset_id_key(asset_id)=?
                LIMIT 1
                """,
                (resource.game_id, resource.app_id, resource.context_id,
                 resource.bot_name, canonical_key, resource.game_id,
                 resource.app_id, resource.context_id, resource.bot_name, canonical_key),
            ).fetchone()
            state = "conflict" if legacy or blocking_units > amount else "usable"
            if previous:
                connection.execute(
                    """
                    UPDATE gd_stock_observations
                    SET observed_amount=?,revision=?,availability_state=?,observed_at=?
                    WHERE observation_id=?
                    """,
                    (amount, revision, state, timestamp, observation_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO gd_stock_observations
                        (observation_id,game_id,app_id,context_id,bot_name,asset_id,
                         asset_id_key,observed_amount,revision,availability_state,observed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (observation_id, resource.game_id, resource.app_id, resource.context_id,
                     resource.bot_name, resource.asset_id, canonical_key, amount, revision,
                     state, timestamp),
                )
            row = connection.execute(
                "SELECT * FROM gd_stock_observations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            return dict(row)

    def allocate(
        self, *, allocation_id: str, resource: StockResource, units: int,
        owner_type: str, listing_id: str | None = None,
        config_version_id: str | None = None, order_line_id: str | None = None,
        legacy_owner_id: str | None = None, state: str = "active",
    ) -> dict[str, Any]:
        resource = resource.normalized()
        units = _positive_int(units, "units")
        allocation = str(allocation_id or "").strip()
        if not allocation:
            raise ValueError("allocation_id обязателен")
        if owner_type not in {"listing", "order", "legacy_blocker"}:
            raise ValueError("Неподдерживаемый тип владельца allocation")
        if state not in {"active", "pending", "unknown", "legacy_conflict", "released"}:
            raise ValueError("Недопустимое начальное состояние allocation")
        if owner_type == "listing" and not listing_id:
            raise ValueError("listing_id обязателен для listing allocation")
        if owner_type == "order" and not order_line_id:
            raise ValueError("order_line_id обязателен для order allocation")
        if owner_type == "legacy_blocker" and not legacy_owner_id:
            raise ValueError("legacy_owner_id обязателен для legacy blocker")
        observation_id = self._observation_id(resource)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            observation = connection.execute(
                "SELECT * FROM gd_stock_observations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            if observation is None:
                raise ValueError("Сначала требуется полное наблюдение инвентаря")
            if observation["availability_state"] != "usable":
                raise ValueError("Наблюдение склада недоступно для резервирования")
            connection.execute(
                """
                INSERT INTO gd_stock_allocations
                    (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                     asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                     order_line_id,legacy_owner_id,units,state)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (allocation, observation_id, resource.game_id, resource.app_id,
                 resource.context_id, resource.bot_name, resource.asset_id,
                 asset_id_key(resource.asset_id), owner_type, listing_id,
                 config_version_id, order_line_id, legacy_owner_id, units, state),
            )
            row = connection.execute(
                "SELECT * FROM gd_stock_allocations WHERE allocation_id=?", (allocation,)
            ).fetchone()
            return dict(row)

    def free_units(self, resource: StockResource) -> int:
        resource = resource.normalized()
        observation_id = self._observation_id(resource)
        with self.database.connect() as connection:
            # Pin both reads to one SQLite snapshot so concurrent writers cannot
            # mix the observed amount and allocation sum from different revisions.
            connection.execute("BEGIN")
            observation = connection.execute(
                "SELECT observed_amount,revision,availability_state "
                "FROM gd_stock_observations WHERE observation_id=?", (observation_id,)
            ).fetchone()
            if observation is None or observation["availability_state"] != "usable":
                return 0
            used = self._blocking_units(connection, observation_id, int(observation["revision"]))
            return max(0, int(observation["observed_amount"]) - used)
