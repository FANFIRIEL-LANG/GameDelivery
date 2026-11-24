from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from core.database.db import Database
from games.asset_ids import asset_id_key, validate_asset_id
from core.delivery.stock_identity import StockItemIdentity


STOCK_OBSERVATION_MAX_AGE = timedelta(minutes=5)


def is_fresh_stock_observation(observed_at: Any, *, now: datetime | None = None) -> bool:
    """Return True only for a valid stock observation no older than five minutes."""
    try:
        timestamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        age = current.astimezone(timezone.utc) - timestamp.astimezone(timezone.utc)
        return timedelta(0) <= age <= STOCK_OBSERVATION_MAX_AGE
    except (TypeError, ValueError, OverflowError):
        return False


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

    @staticmethod
    def _identity_payload(identity: StockItemIdentity | None) -> str | None:
        if identity is None:
            return None
        normalized = identity.normalized()
        return json.dumps({
            "game_id": normalized.game_id,
            "app_id": normalized.app_id,
            "context_id": normalized.context_id,
            "market_hash_name": normalized.market_hash_name,
            "name": normalized.name,
            "item_type": normalized.item_type,
            "class_id": normalized.class_id,
            "quality": normalized.quality,
            "exterior": normalized.exterior,
            "rarity": normalized.rarity,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _validate_observation_metadata(
        resource: StockResource,
        identity: StockItemIdentity | None,
        is_tradable: bool | None,
    ) -> tuple[str | None, int | None]:
        if identity is not None:
            if not isinstance(identity, StockItemIdentity):
                raise ValueError("identity должна быть StockItemIdentity или None")
            identity = identity.normalized()
            if (identity.game_id, identity.app_id, identity.context_id) != (
                resource.game_id, resource.app_id, resource.context_id
            ):
                raise ValueError("Identity не соответствует физическому scope ресурса")
        if is_tradable is not None and type(is_tradable) is not bool:
            raise ValueError("is_tradable должен быть bool или None")
        return (
            QuantityStockLedger._identity_payload(identity),
            None if is_tradable is None else int(is_tradable),
        )

    def _observe_in_transaction(
        self,
        connection: sqlite3.Connection,
        resource: StockResource,
        amount: int,
        *,
        observed_at: str,
        identity: StockItemIdentity | None,
        is_tradable: bool | None,
    ) -> dict[str, Any]:
        identity_json, tradable_value = self._validate_observation_metadata(
            resource, identity, is_tradable
        )
        observation_id = self._observation_id(resource)
        canonical_key = asset_id_key(resource.asset_id)
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
                SET observed_amount=?,revision=?,availability_state=?,observed_at=?,
                    identity_json=?,is_tradable=?
                WHERE observation_id=?
                """,
                (amount, revision, state, observed_at, identity_json,
                 tradable_value, observation_id),
            )
        else:
            connection.execute(
                """
                INSERT INTO gd_stock_observations
                    (observation_id,game_id,app_id,context_id,bot_name,asset_id,
                     asset_id_key,observed_amount,revision,availability_state,observed_at,
                     identity_json,is_tradable)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (observation_id, resource.game_id, resource.app_id, resource.context_id,
                 resource.bot_name, resource.asset_id, canonical_key, amount, revision,
                 state, observed_at, identity_json, tradable_value),
            )
        row = connection.execute(
            "SELECT * FROM gd_stock_observations WHERE observation_id=?",
            (observation_id,),
        ).fetchone()
        return dict(row)

    def observe(
        self,
        resource: StockResource,
        amount: int,
        *,
        observed_at: str | None = None,
        identity: StockItemIdentity | None = None,
        is_tradable: bool | None = None,
    ) -> dict[str, Any]:
        resource = resource.normalized()
        amount = _positive_int(amount, "amount")
        timestamp = observed_at or datetime.now(timezone.utc).isoformat()
        # Validate the metadata before opening the write transaction.
        self._validate_observation_metadata(resource, identity, is_tradable)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._observe_in_transaction(
                connection, resource, amount, observed_at=timestamp,
                identity=identity, is_tradable=is_tradable,
            )

    def observe_snapshot(
        self,
        game_id: str,
        bot_name: str,
        items,
        *,
        observed_at: str | None = None,
    ) -> dict[str, int | str]:
        """Commit one parser-validated complete ASF inventory snapshot.

        Missing assets are marked unavailable in the same transaction; an
        incomplete/invalid parser response must never be passed to this method.
        Steam card sets remain on their separate inventory/reservation path.
        """
        from games.catalog import get_game

        game = get_game(game_id)
        if game.item_domain == "trading_cards":
            raise ValueError("Steam Cards используют отдельную подсистему складского учёта")
        bot = str(bot_name or "").strip()
        if not bot:
            raise ValueError("Имя бота не должно быть пустым")
        if not isinstance(items, (list, tuple)):
            raise ValueError("Полный snapshot должен быть списком InventoryItem")
        timestamp = observed_at or datetime.now(timezone.utc).isoformat()
        prepared = []
        seen: set[str] = set()
        for item in items:
            try:
                item_game = str(item.game_id).strip()
                item_bot = str(item.bot_name).strip()
                resource = StockResource(
                    game_id=item_game,
                    app_id=game.app_id,
                    context_id=str(game.context_id),
                    bot_name=item_bot,
                    asset_id=item.asset_id,
                ).normalized()
                amount = _positive_int(item.amount, "amount")
                identity = StockItemIdentity(
                    game_id=item_game,
                    app_id=game.app_id,
                    context_id=str(game.context_id),
                    market_hash_name=item.market_hash_name,
                    name=item.name,
                    item_type=item.item_type,
                    class_id=item.class_id,
                    quality=item.quality,
                    exterior=item.exterior,
                    rarity=item.rarity,
                ).normalized()
                tradable = item.is_tradable
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError(f"Некорректный InventoryItem в полном snapshot: {exc}") from exc
            if item_game != game.game_id or item_bot.casefold() != bot.casefold():
                raise ValueError("Snapshot содержит предмет другой игры или бота")
            if tradable is not None and type(tradable) is not bool:
                raise ValueError("is_tradable должен быть bool или None")
            observation_id = self._observation_id(resource)
            if observation_id in seen:
                raise ValueError("Snapshot содержит повторяющийся физический Asset ID")
            seen.add(observation_id)
            prepared.append((resource, amount, identity, tradable))

        scope = (game.game_id, game.app_id, str(game.context_id), bot)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT observation_id,revision FROM gd_stock_observations
                WHERE game_id=? AND app_id=? AND context_id=? AND bot_name=? COLLATE NOCASE
                """,
                scope,
            ).fetchall()
            missing = [
                row for row in existing
                if row["observation_id"] not in seen
            ]
            for row in missing:
                connection.execute(
                    """
                    UPDATE gd_stock_observations
                    SET revision=?,availability_state='unavailable',observed_at=?
                    WHERE observation_id=?
                    """,
                    (int(row["revision"]) + 1, timestamp, row["observation_id"]),
                )
            observed = []
            for resource, amount, identity, tradable in prepared:
                observed.append(self._observe_in_transaction(
                    connection, resource, amount, observed_at=timestamp,
                    identity=identity, is_tradable=tradable,
                ))
        return {
            "game_id": game.game_id,
            "bot_name": bot,
            "observed_count": len(observed),
            "unavailable_count": len(missing),
            "conflict_count": sum(row["availability_state"] == "conflict" for row in observed),
        }

    def eligible_resources(self, spec: object) -> list[StockResource]:
        """Return only fresh, exactly matching Steam assets with confirmed tradability."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT game_id,app_id,context_id,bot_name,asset_id,identity_json,is_tradable,observed_at
                FROM gd_stock_observations
                WHERE availability_state='usable' AND identity_json IS NOT NULL
                  AND is_tradable=1
                ORDER BY bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key
                """
            ).fetchall()
        matches: list[StockResource] = []
        for row in rows:
            if not is_fresh_stock_observation(row["observed_at"]):
                continue
            try:
                raw_identity = json.loads(row["identity_json"])
                if not isinstance(raw_identity, dict):
                    continue
                identity = StockItemIdentity(**raw_identity)
                if not identity.matches_spec(spec):
                    continue
                resource = StockResource(
                    row["game_id"], int(row["app_id"]), row["context_id"],
                    row["bot_name"], row["asset_id"],
                ).normalized()
                if identity.allows_bot(resource.bot_name, spec):
                    matches.append(resource)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return matches

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
            if (observation["availability_state"] != "usable"
                or not is_fresh_stock_observation(observation["observed_at"])):
                raise ValueError("Наблюдение склада недоступно или устарело для резервирования")
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
                "SELECT observed_amount,revision,availability_state,observed_at "
                "FROM gd_stock_observations WHERE observation_id=?", (observation_id,)
            ).fetchone()
            if (observation is None or observation["availability_state"] != "usable"
                or not is_fresh_stock_observation(observation["observed_at"])):
                return 0
            used = self._blocking_units(connection, observation_id, int(observation["revision"]))
            return max(0, int(observation["observed_amount"]) - used)
