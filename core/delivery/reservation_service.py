from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from core.database.db import Database
from games.cs2.inventory_view import (
    CS2InventoryInstance,
    RestrictionStatus,
    RestrictionType,
)
from games.steam_cards.owned_selection import select_owned_complete_sets
from games.steam_cards.selection import CardCopy


@dataclass(frozen=True)
class InventorySnapshot:
    """One complete ASF inventory snapshot; timestamp is the successful refresh time."""

    bot_name: str
    game_id: str
    app_id: int
    context_id: str
    refreshed_at: datetime
    items: tuple[Any, ...]


@dataclass(frozen=True)
class PlannedLeg:
    bot_name: str
    app_id: int
    context_id: str
    asset_ids: tuple[str, ...]


@dataclass(frozen=True)
class OrderReservationResult:
    order_id: str
    status: str
    legs: tuple[PlannedLeg, ...]
    reserved_assets: tuple[tuple[str, str], ...]
    notification_event_ids: tuple[str, ...]
    message: str

    @property
    def reserved(self) -> bool:
        return self.status == "reserved"


class DeliveryReservationService:
    """Plan a whole order and atomically reserve all its assets in the shared ledger.

    This service is deliberately pre-trade: it creates no ASF request and no Steam offer.
    Inventory snapshots must have been fetched from ASF by the caller.
    """

    EVENT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS gd_delivery_events (
        event_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES gd_orders(order_id) ON DELETE RESTRICT,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(order_id, event_type)
    )
    """

    def __init__(self, database: Database, *, max_snapshot_age: timedelta = timedelta(minutes=5)):
        if max_snapshot_age <= timedelta(0):
            raise ValueError("max_snapshot_age должен быть положительным")
        self.database = database
        self.max_snapshot_age = max_snapshot_age
        self.database.initialize()
        with self.database.connect() as connection:
            connection.execute(self.EVENT_SCHEMA)

    @staticmethod
    def _normal(value: Any) -> str:
        return str(value).strip().casefold()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Время снимка должно содержать часовой пояс")
        return value.astimezone(timezone.utc)

    def _snapshot_is_fresh(self, snapshot: InventorySnapshot, now: datetime) -> bool:
        refreshed = self._aware(snapshot.refreshed_at)
        return refreshed <= now and now - refreshed <= self.max_snapshot_age

    @staticmethod
    def _get(item: Any, field: str, default: Any = None) -> Any:
        if isinstance(item, Mapping):
            return item.get(field, default)
        return getattr(item, field, default)

    @classmethod
    def _is_tradable(cls, item: Any) -> bool:
        # CS2 carries explicit restriction metadata in addition to the ASF tradable flag.
        if isinstance(item, CS2InventoryInstance):
            return (
                item.is_tradable is True
                and item.restriction_type is RestrictionType.NONE
                and item.restriction_status is RestrictionStatus.AVAILABLE
                and item.tradable_after is None
                and item.amount is not None
                and item.amount > 0
            )
        # CardCopy and generic snapshot rows must explicitly prove True.
        return cls._get(item, "is_tradable") is True and cls._get(item, "trade_locked", False) is False

    @staticmethod
    def _card_selector(selectors: Mapping[str, Any]) -> bool:
        item_type = str(selectors.get("item_type", "")).strip().casefold()
        delivery_type = str(selectors.get("delivery_type", "")).strip().casefold()
        return item_type in {"steam_card_set", "trading_card_set", "card_set"} or delivery_type == "steam_card_set"

    @classmethod
    def _matches_selector(cls, item: Any, selectors: Mapping[str, Any], snapshot: InventorySnapshot) -> bool:
        aliases = {
            "market_hash_name": "market_hash_name",
            "name": "name",
            "item_type": "item_type",
            "class_id": "class_id",
            "quality": "quality",
            "exterior": "exterior",
            "rarity": "rarity",
        }
        for selector_key, item_key in aliases.items():
            expected = selectors.get(selector_key)
            if expected not in (None, "") and cls._normal(cls._get(item, item_key, "")) != cls._normal(expected):
                return False
        expected_game = selectors.get("game_id")
        if expected_game and cls._normal(snapshot.game_id) != cls._normal(expected_game):
            return False
        return True

    def _select_assets(
        self,
        selectors: Mapping[str, Any],
        quantity: int,
        snapshots: Mapping[str, InventorySnapshot],
        bot_pool: tuple[str, ...],
        now: datetime,
    ) -> tuple[tuple[str, str, int, str], ...]:
        """Return all matching (bot, asset, app, context) candidates; caller enforces quantity."""
        allowed = {self._normal(bot): bot for bot in bot_pool}
        fresh: dict[str, InventorySnapshot] = {}
        for key, snapshot in snapshots.items():
            bot = self._normal(snapshot.bot_name)
            if bot not in allowed or self._normal(key) != bot:
                continue
            if self._snapshot_is_fresh(snapshot, now):
                fresh[bot] = snapshot

        if self._card_selector(selectors):
            game_name = str(selectors.get("game_name") or selectors.get("name") or "").strip()
            if not game_name:
                raise ValueError("Для Steam Card set selector требуется game_name")
            card_inventories: dict[str, list[CardCopy]] = {}
            for bot, snapshot in fresh.items():
                if snapshot.app_id != 753 or str(snapshot.context_id) != "6":
                    continue
                cards = [
                    item for item in snapshot.items
                    if isinstance(item, CardCopy)
                    and item.is_trading_card
                    and not item.is_foil
                    and item.is_tradable is True
                    and item.game_name.strip().casefold() == game_name.casefold()
                ]
                card_inventories[snapshot.bot_name] = cards
            try:
                sets = select_owned_complete_sets(card_inventories, game_name, quantity)
            except ValueError:
                return ()
            chosen = [
                (asset.bot_name, asset.asset_id, 753, "6")
                for owned_set in sets
                for asset in owned_set.asset_ids
            ]
            return tuple(chosen)

        game_id = str(selectors.get("game_id") or "").strip()
        app_id = selectors.get("app_id")
        context_id = str(selectors.get("context_id", "")).strip()
        if not game_id or isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0 or not context_id:
            raise ValueError("Для предметного selector обязательны game_id, app_id и context_id")

        candidates: list[tuple[str, str, int, str]] = []
        seen: set[tuple[str, str]] = set()
        for bot in bot_pool:
            snapshot = fresh.get(self._normal(bot))
            if snapshot is None or snapshot.app_id != app_id or str(snapshot.context_id) != context_id:
                continue
            if self._normal(snapshot.game_id) != self._normal(game_id):
                continue
            for item in snapshot.items:
                asset_id = str(self._get(item, "asset_id", "")).strip()
                owner = str(self._get(item, "bot_name", snapshot.bot_name)).strip()
                if not asset_id or self._normal(owner) != self._normal(snapshot.bot_name):
                    continue
                item_game = self._get(item, "game_id", snapshot.game_id)
                item_app = self._get(item, "appid", app_id)
                item_context = str(self._get(item, "context_id", context_id))
                if (
                    self._normal(item_game) != self._normal(snapshot.game_id)
                    or item_app != app_id
                    or item_context != context_id
                ):
                    continue
                key = (self._normal(owner), asset_id)
                if key in seen or not self._is_tradable(item):
                    continue
                if not self._matches_selector(item, selectors, snapshot):
                    continue
                seen.add(key)
                candidates.append((snapshot.bot_name, asset_id, app_id, context_id))
        candidates.sort(key=lambda row: (row[0].casefold(), row[1]))
        return tuple(candidates)

    @staticmethod
    def _stable_id(prefix: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}:{digest}"

    def _record_insufficient(
        self, order_id: str, chat_id: str | None, config_version_id: str, reason: str
    ) -> tuple[str, ...]:
        event_ids = []
        payload = json.dumps({
            "reason": reason,
            "chat_id": chat_id,
            "config_version_id": config_version_id,
            "status": "insufficient_stock",
        }, ensure_ascii=False, sort_keys=True)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE gd_orders SET status='insufficient_stock', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE order_id=?",
                (order_id,),
            )
            for event_type in ("buyer_notification", "telegram_notification"):
                event_id = self._stable_id("event", order_id + ":" + event_type)
                connection.execute(
                    "INSERT OR IGNORE INTO gd_delivery_events "
                    "(event_id, order_id, event_type, payload_json) VALUES (?, ?, ?, ?)",
                    (event_id, order_id, event_type, payload),
                )
                event_ids.append(event_id)
        return tuple(event_ids)

    def reserve_order(
        self,
        *,
        order_id: str,
        config_version_id: str,
        snapshots: Mapping[str, InventorySnapshot],
        now: datetime | None = None,
    ) -> OrderReservationResult:
        order = order_id.strip()
        config_id = config_version_id.strip()
        if not order or not config_id:
            raise ValueError("order_id и config_version_id обязательны")
        current_time = self._aware(now or datetime.now(timezone.utc))

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            order_row = connection.execute(
                "SELECT o.order_id, o.status, d.chat_id, d.config_version_id "
                "FROM gd_orders o JOIN gd_deals d ON d.deal_id=o.deal_id "
                "WHERE o.order_id=?", (order,)
            ).fetchone()
            if order_row is None:
                raise KeyError(f"Заказ {order} не найден")
            if order_row["config_version_id"] != config_id:
                raise ValueError("ConfigVersion не совпадает со снимком сделки")
            if order_row["status"] == "reserved":
                rows = connection.execute(
                    "SELECT bot_name, app_id, context_id, asset_ids_json FROM gd_delivery_legs "
                    "WHERE order_line_id IN (SELECT order_line_id FROM gd_order_lines WHERE order_id=?) "
                    "ORDER BY bot_name", (order,)
                ).fetchall()
                legs = tuple(PlannedLeg(
                    row["bot_name"], row["app_id"], row["context_id"],
                    tuple(json.loads(row["asset_ids_json"])),
                ) for row in rows)
                assets = tuple((leg.bot_name, asset) for leg in legs for asset in leg.asset_ids)
                return OrderReservationResult(order, "reserved", legs, assets, (), "Заказ уже зарезервирован.")
            config = connection.execute(
                "SELECT listing_id, delivery_quantity_per_deal, item_selectors_json, bot_pool_json "
                "FROM gd_config_versions WHERE config_version_id=?", (config_id,)
            ).fetchone()
            if config is None:
                raise KeyError(f"ConfigVersion {config_id} не найден")
            selectors_raw = json.loads(config["item_selectors_json"])
            if not isinstance(selectors_raw, dict):
                raise ValueError("Планировщик ожидает один объект item selector")
            quantity = config["delivery_quantity_per_deal"]
            if quantity is None or quantity < 1:
                raise ValueError("ConfigVersion не содержит точного delivery_quantity_per_deal")
            bot_pool = tuple(json.loads(config["bot_pool_json"]))
            if not bot_pool:
                raise ValueError("ConfigVersion не содержит разрешённого пула ботов")
            selected = self._select_assets(selectors_raw, quantity, snapshots, bot_pool, current_time)
            if len(selected) < quantity:
                reason = (
                    f"Недостаточно свежих tradable предметов: требуется {quantity}, "
                    f"доступно {len(selected)}."
                )
                connection.rollback()
            else:
                line_id = f"{order}:line:1"
                if connection.execute(
                    "SELECT 1 FROM gd_order_lines WHERE order_id=?", (order,)
                ).fetchone():
                    raise ValueError("Для заказа уже существуют строки; повторное планирование запрещено")
                connection.execute(
                    "INSERT INTO gd_order_lines "
                    "(order_line_id, order_id, line_number, item_type, item_name, item_selector_json, quantity) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?)",
                    (line_id, order, str(selectors_raw.get("item_type", selectors_raw.get("delivery_type", "item"))),
                     str(selectors_raw.get("market_hash_name") or selectors_raw.get("game_name") or selectors_raw.get("name") or ""),
                     json.dumps(selectors_raw, ensure_ascii=False, sort_keys=True), quantity),
                )
                grouped: dict[str, list[tuple[str, int, str]]] = {}
                for bot, asset_id, app_id, context_id in selected:
                    conflict = connection.execute(
                        "SELECT 1 FROM gd_resource_reservations WHERE state='active' "
                        "AND bot_name=? COLLATE NOCASE AND asset_id=?",
                        (bot, asset_id),
                    ).fetchone()
                    if conflict:
                        connection.rollback()
                        reason = f"Ресурс {bot}:{asset_id} уже зарезервирован другим заказом."
                        break
                    grouped.setdefault(bot, []).append((asset_id, app_id, context_id))
                else:
                    for bot, entries in grouped.items():
                        leg_id = self._stable_id("leg", order + ":" + bot.casefold())
                        asset_ids = [entry[0] for entry in entries]
                        connection.execute(
                            "INSERT INTO gd_delivery_legs "
                            "(leg_id, order_line_id, bot_name, app_id, context_id, asset_ids_json, status) "
                            "VALUES (?, ?, ?, ?, ?, ?, 'reserved')",
                            (leg_id, line_id, bot, entries[0][1], entries[0][2],
                             json.dumps(asset_ids, ensure_ascii=False)),
                        )
                        for asset_id, app_id, context_id in entries:
                            reservation_id = self._stable_id("reservation", order + ":" + bot.casefold() + ":" + asset_id)
                            connection.execute(
                                "INSERT INTO gd_resource_reservations "
                                "(reservation_id, owner_type, order_line_id, game_id, app_id, context_id, "
                                "bot_name, asset_id, units, state) "
                                "VALUES (?, 'order', ?, ?, ?, ?, ?, ?, 1, 'active')",
                                (reservation_id, line_id, selectors_raw.get("game_id", "steam_cards"),
                                 app_id, context_id, bot, asset_id),
                            )
                    if connection.in_transaction:
                        connection.execute(
                            "UPDATE gd_orders SET status='reserved', "
                            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE order_id=?",
                            (order,),
                        )
                        connection.commit()
                        legs = tuple(
                            PlannedLeg(bot, entries[0][1], entries[0][2], tuple(item[0] for item in entries))
                            for bot, entries in grouped.items()
                        )
                        assets = tuple((bot, asset_id) for bot, entries in grouped.items() for asset_id, _, _ in entries)
                        return OrderReservationResult(order, "reserved", legs, assets, (), "Все ресурсы атомарно зарезервированы.")
                    # The transaction was rolled back on a reservation conflict.
                    reason = reason if "reason" in locals() else "Конфликт глобального резерва."

        event_ids = self._record_insufficient(order, order_row["chat_id"], config_id, reason)
        return OrderReservationResult(order, "insufficient_stock", (), (), event_ids, reason)
