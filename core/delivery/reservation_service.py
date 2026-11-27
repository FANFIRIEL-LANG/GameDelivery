from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.digital_codes import DigitalCodeError, DigitalCodeStore
from core.delivery.playerok_eligibility import (
    PlayerokPaidPreparation,
    create_playerok_order_for_preparation,
    require_canonical_playerok_delivery_eligible,
)
from core.delivery.listing_stock import InsufficientListingStock, ListingStockService
from core.delivery.recipes import (
    DigitalTextUnitSpec,
    RecipeItem,
    SteamAssetSpec,
    SteamCardSetSpec,
    normalize_legacy_cs2_selector_options,
)
from core.delivery.recipes import legacy_materialized_order_line
from games.cs2.inventory_view import CS2InventoryInstance
from games.asset_ids import asset_id_key, validate_asset_id
from games.steam_cards.order_preflight import MultiGameOrderPreflight, has_verified_card_selection
from games.steam_cards.owned_selection import select_owned_complete_sets
from games.steam_cards.recipient_badge import RecipientBadge, evaluate_recipient_badge_capacity
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
    asf_verified: bool


@dataclass(frozen=True)
class PlannedLeg:
    bot_name: str
    app_id: int
    context_id: str
    asset_ids: tuple[str, ...]
    order_line_id: str = ""
    asset_amounts: Mapping[str, int] | None = None


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


class ResourceReservationConflict(ValueError):
    """A preplanned physical resource was claimed before commit."""


@dataclass(frozen=True)
class PlannedResource:
    """One physical resource selected outside the SQLite transaction."""

    bot_name: str
    asset_id: str
    game_id: str
    app_id: int
    context_id: str
    steam_cards_compatibility: bool = False
    steam_card_hash_name: str | None = None
    units: int = 1
    stock_allocation_ids: tuple[str, ...] = ()

@dataclass(frozen=True)
class PlannedRecipeLine:
    """Selected physical resources grouped by one logical RecipeItem unit."""

    recipe_item_id: str
    units: tuple[tuple[PlannedResource, ...], ...]
    steam_card_count_per_set: int | None = None
    # Exact verified selector output, retained so the ledger boundary can compare
    # planned resources to the existing authoritative preflight result.
    steam_card_preflight: MultiGameOrderPreflight | None = None
    use_listing_stock: bool = False


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

    @classmethod
    def _snapshot_key_matches(cls, key: Any, snapshot: InventorySnapshot) -> bool:
        bot = cls._normal(snapshot.bot_name)
        if isinstance(key, str):
            return cls._normal(key) == bot
        return (
            isinstance(key, tuple) and len(key) == 3
            and cls._normal(key[0]) == bot
            and key[1] == snapshot.app_id
            and str(key[2]) == str(snapshot.context_id)
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Время снимка должно содержать часовой пояс")
        return value.astimezone(timezone.utc)

    def _snapshot_is_fresh(self, snapshot: InventorySnapshot, now: datetime) -> bool:
        if snapshot.asf_verified is not True:
            return False
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
            # ASF's tradable flag is authoritative for the current inventory snapshot.
            # Steam does not always expose the optional restriction fields separately:
            # UNKNOWN metadata is not itself a block when tradable=True is explicit.
            if item.is_tradable is not True or item.amount is None or item.amount <= 0:
                return False
            if item.tradable_after is not None:
                return False
            restriction_type = getattr(item.restriction_type, "value", item.restriction_type)
            restriction_status = getattr(item.restriction_status, "value", item.restriction_status)
            # ASF tradable=True is authoritative when optional restriction metadata
            # is UNKNOWN or reports only a market purchase cooldown. Explicit trade
            # protection and restricted states remain blocked.
            if restriction_type not in (
                None, "", "none", "unknown", "market_purchase_cooldown"
            ):
                return False
            if restriction_status == "restricted":
                return False
            if restriction_status not in ("available", "unknown", None, ""):
                return False
            return True
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
            generic_types = {"item", "cs2_item", "steam_item", "steam_card_set", "trading_card_set", "card_set"}
            if selector_key == "item_type" and cls._normal(expected) in generic_types:
                continue
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
        fresh: dict[tuple[str, int, str], InventorySnapshot] = {}
        for key, snapshot in snapshots.items():
            bot = self._normal(snapshot.bot_name)
            raw_context = str(snapshot.context_id)
            key_matches = self._snapshot_key_matches(key, snapshot)
            if bot not in allowed or not key_matches:
                continue
            if self._snapshot_is_fresh(snapshot, now):
                slot = (bot, snapshot.app_id, raw_context)
                if slot in fresh and fresh[slot] is not snapshot:
                    raise ValueError("Для одного бота и inventory context получено несколько снимков")
                fresh[slot] = snapshot

        if self._card_selector(selectors):
            game_name = str(selectors.get("game_name") or selectors.get("name") or "").strip()
            if not game_name:
                raise ValueError("Для Steam Card set selector требуется game_name")
            card_inventories: dict[str, list[CardCopy]] = {}
            for (bot, app_id, context_id), snapshot in fresh.items():
                if app_id != 753 or context_id != "6":
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
            snapshot = fresh.get((self._normal(bot), app_id, context_id))
            if snapshot is None:
                continue
            if self._normal(snapshot.game_id) != self._normal(game_id):
                continue
            for item in snapshot.items:
                asset_id = validate_asset_id(self._get(item, "asset_id"))
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
                key = (self._normal(owner), asset_id_key(asset_id))
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

    def _record_failure(
        self, order_id: str, chat_id: str | None, config_version_id: str,
        reason: str, status: str,
    ) -> tuple[str, ...]:
        event_ids = []
        payload = json.dumps({
            "reason": reason,
            "chat_id": chat_id,
            "config_version_id": config_version_id,
            "status": status,
        }, ensure_ascii=False, sort_keys=True)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE gd_orders SET status=?, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE order_id=?",
                (status, order_id),
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
        recipient_badges: tuple[RecipientBadge, ...] | list[RecipientBadge] = (),
        badge_data_verified: bool = False,
    ) -> OrderReservationResult:
        order = order_id.strip()
        config_id = config_version_id.strip()
        if not order or not config_id:
            raise ValueError("order_id и config_version_id обязательны")
        current_time = self._aware(now or datetime.now(timezone.utc))

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            require_canonical_playerok_delivery_eligible(connection, order)
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
            if isinstance(selectors_raw, dict):
                selector_options = normalize_legacy_cs2_selector_options(selectors_raw)
                if selector_options is None:
                    selector_options = [selectors_raw]
            elif isinstance(selectors_raw, list) and selectors_raw and all(
                isinstance(value, dict) for value in selectors_raw
            ):
                selector_options = selectors_raw
            else:
                raise ValueError("item_selectors должен быть объектом или непустым списком объектов")
            quantity = config["delivery_quantity_per_deal"]
            if quantity is None or quantity < 1:
                raise ValueError("ConfigVersion не содержит точного delivery_quantity_per_deal")
            bot_pool = tuple(json.loads(config["bot_pool_json"]))
            if not bot_pool:
                raise ValueError("ConfigVersion не содержит разрешённого пула ботов")
            selected: tuple[tuple[str, str, int, str], ...] = ()
            selected_selector: Mapping[str, Any] | None = None
            best_available = 0
            failure_status = "insufficient_stock"
            card_selector = any(self._card_selector(option) for option in selector_options)
            badge_failure = None
            if card_selector:
                game_name = next(
                    str(option.get("game_name") or option.get("name") or "").strip()
                    for option in selector_options if self._card_selector(option)
                )
                badge_capacity = evaluate_recipient_badge_capacity(
                    game_name, quantity, recipient_badges,
                    badge_data_verified=badge_data_verified,
                )
                if not badge_capacity.can_proceed:
                    failure_status = "preflight_blocked"
                    badge_failure = (
                        "Проверка значка получателя заблокировала заказ: "
                        + badge_capacity.reason
                    )
            if badge_failure:
                reason = badge_failure
                connection.rollback()
            else:
                for selector in selector_options:
                    candidates = self._select_assets(selector, quantity, snapshots, bot_pool, current_time)
                    if len(candidates) > best_available:
                        best_available = len(candidates)
                    if len(candidates) >= quantity:
                        # A normal inventory order requests an exact number of
                        # physical items. Card-set selection already returns all
                        # cards belonging to the requested number of complete sets.
                        selected = (
                            candidates
                            if self._card_selector(selector)
                            else candidates[:quantity]
                        )
                        selected_selector = selector
                        break
            if not badge_failure and selected_selector is None:
                reason = (
                    f"Недостаточно свежих tradable предметов: требуется {quantity}, "
                    f"доступно не более {best_available}."
                )
                connection.rollback()
            if selected_selector is None:
                pass
            else:
                selectors_raw = selected_selector
                existing_lines = connection.execute(
                    "SELECT 1 FROM gd_order_lines WHERE order_id=?", (order,)
                ).fetchone()
                seed_line = legacy_materialized_order_line(
                    connection, order, config_id
                ) if existing_lines else None
                if existing_lines and seed_line is None:
                    raise ValueError("Для заказа уже существуют строки; повторное планирование запрещено")
                line_id = str(seed_line["order_line_id"]) if seed_line else f"{order}:line:1"
                if seed_line is None:
                    connection.execute(
                        "INSERT INTO gd_order_lines "
                        "(order_line_id, order_id, config_version_id, line_number, item_type, item_name, item_selector_json, quantity) "
                        "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                        (line_id, order, config_id,
                         str(selectors_raw.get("item_type", selectors_raw.get("delivery_type", "item"))),
                         str(selectors_raw.get("market_hash_name") or selectors_raw.get("game_name") or selectors_raw.get("name") or ""),
                         json.dumps(selectors_raw, ensure_ascii=False, sort_keys=True), quantity),
                    )
                grouped: dict[str, list[tuple[str, int, str]]] = {}
                for bot, asset_id, app_id, context_id in selected:
                    conflict = connection.execute(
                    "SELECT 1 FROM gd_resource_reservations WHERE state IN ('active','legacy_conflict') "
                    "AND bot_name=? COLLATE NOCASE AND asset_id_key=?",
                    (bot, asset_id_key(asset_id)),
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

        event_ids = self._record_failure(
            order, order_row["chat_id"], config_id, reason, failure_status
        )
        return OrderReservationResult(order, failure_status, (), (), event_ids, reason)

    def reserve_recipe_plan(
        self,
        *,
        order_id: str,
        config_version_id: str,
        planned_by_line: Mapping[str, PlannedRecipeLine],
        paid_context: PlayerokPaidPreparation | None = None,
    ) -> OrderReservationResult:
        """Atomically commit every preplanned required Recipe resource and leg.

        All inventory/card analysis is completed by the caller before this method
        opens ``BEGIN IMMEDIATE``. The transaction rechecks order/version/line
        ownership and global resource uniqueness before inserting anything.
        """
        order = str(order_id or "").strip()
        config_id = str(config_version_id or "").strip()
        if not order or not config_id or not isinstance(planned_by_line, Mapping):
            raise ValueError("order_id, config_version_id и plan обязательны")
        grouped: dict[tuple[str, str, int, str, str, bool], dict[str, int]] = {}
        stock_allocation_ids_seen: set[str] = set()
        stock_backed_assets: set[tuple[tuple[str, str, int, str, str, bool], str]] = set()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if paid_context is not None:
                if paid_context.order_id != order or paid_context.config_version_id != config_id:
                    raise ValueError("Paid preparation context не совпадает с reservation plan")
                recipe = DeliveryLedger.recipe_for_config_version(
                    connection, config_id, allow_legacy=True
                )
                create_playerok_order_for_preparation(
                    connection, paid_context,
                    sum(item.quantity_per_order for item in recipe.items),
                )
            require_canonical_playerok_delivery_eligible(connection, order)
            order_row = connection.execute(
                "SELECT o.status,d.config_version_id,d.listing_id,d.source_platform FROM gd_orders o "
                "JOIN gd_deals d ON d.deal_id=o.deal_id WHERE o.order_id=?",
                (order,),
            ).fetchone()
            if order_row is None:
                raise KeyError(f"Заказ {order} не найден")
            if str(order_row["config_version_id"] or "") != config_id:
                raise ValueError("ConfigVersion изменилась после планирования")
            if order_row["status"] == "reserved":
                rows = connection.execute("""
                    SELECT l.leg_id,l.order_line_id,l.bot_name,l.app_id,l.context_id,
                           l.asset_ids_json,l.asset_amounts_json
                    FROM gd_delivery_legs l JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id
                    WHERE ol.order_id=? ORDER BY ol.line_number,l.bot_name COLLATE NOCASE
                """, (order,)).fetchall()
                legs = tuple(PlannedLeg(
                    row["bot_name"], int(row["app_id"]), str(row["context_id"]),
                    tuple(json.loads(row["asset_ids_json"])), str(row["order_line_id"]),
                    json.loads(row["asset_amounts_json"]) if row["asset_amounts_json"] else None,
                ) for row in rows)
                assets = tuple((leg.bot_name, asset) for leg in legs for asset in leg.asset_ids)
                return OrderReservationResult(order, "reserved", legs, assets, (), "Заказ уже зарезервирован.")
            if order_row["status"] not in {"pending", "insufficient_stock", "preflight_blocked"}:
                raise ValueError(f"Заказ имеет недопустимый статус для резерва: {order_row['status']}")

            # OrderLine/manifest creation joins this exact transaction. A failed
            # reservation therefore cannot leave a partial manifest behind.
            DeliveryLedger.materialize_recipe_order_lines_in_transaction(
                connection, order, config_id, allow_legacy=True
            )
            recipe = DeliveryLedger.recipe_for_config_version(
                connection, config_id, allow_legacy=True
            )
            items_by_position = {item.position: item for item in recipe.items}
            if len(items_by_position) != len(recipe.items):
                raise ValueError("Recipe содержит повторяющиеся позиции")
            lines = {
                str(row["order_line_id"]): row
                for row in connection.execute(
                    "SELECT order_line_id,order_id,config_version_id,line_number,item_type,item_name,"
                    "item_selector_json,quantity FROM gd_order_lines WHERE order_id=?",
                    (order,),
                ).fetchall()
            }
            if len(lines) != len(recipe.items):
                raise ValueError("Набор OrderLine не совпадает с Recipe закреплённой ConfigVersion")
            item_by_line: dict[str, RecipeItem] = {}
            for line_id, line in lines.items():
                item = items_by_position.get(int(line["line_number"]))
                if (
                    item is None
                    or str(line["order_id"]) != order
                    or str(line["config_version_id"] or "") != config_id
                    or line["item_type"] != item.kind
                    or line["item_name"] != item.display_name
                    or line["quantity"] != item.quantity_per_order
                ):
                    raise ValueError("OrderLine не соответствует Recipe или ConfigVersion сделки")
                try:
                    snapshot = json.loads(line["item_selector_json"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("OrderLine содержит некорректный Recipe snapshot") from exc
                if not isinstance(snapshot, dict) or snapshot != {
                    "config_version_id": config_id,
                    "recipe_item_id": item.item_id,
                    "recipe_item": item.to_dict(),
                }:
                    raise ValueError("RecipeItem snapshot OrderLine не совпадает с ConfigVersion")
                item_by_line[line_id] = item

            if set(planned_by_line) - set(lines):
                raise ValueError("План ссылается на OrderLine другого заказа или неизвестную строку")
            missing_required = {
                line_id for line_id, item in item_by_line.items()
                if item.required and line_id not in planned_by_line
            }
            if missing_required:
                raise ValueError("План не содержит все обязательные RecipeItem")
            config = connection.execute(
                "SELECT bot_pool_json FROM gd_config_versions WHERE config_version_id=?",
                (config_id,),
            ).fetchone()
            if config is None:
                raise ValueError("ConfigVersion отсутствует")
            try:
                config_bot_pool = json.loads(config["bot_pool_json"])
            except (TypeError, ValueError) as exc:
                raise ValueError("ConfigVersion содержит некорректный bot_pool") from exc
            if not isinstance(config_bot_pool, list) or any(
                not isinstance(bot, str) or not bot.strip() for bot in config_bot_pool
            ):
                raise ValueError("ConfigVersion содержит некорректный bot_pool")

            effective_planned = dict(planned_by_line)
            for line_id, line_plan in tuple(effective_planned.items()):
                if not isinstance(line_plan, PlannedRecipeLine) or not line_plan.use_listing_stock:
                    continue
                item = item_by_line.get(line_id)
                if not isinstance(item.spec if item else None, SteamAssetSpec):
                    raise ValueError("Listing stock transfer разрешён только для Steam Asset RecipeItem")
                if line_plan.units or line_plan.steam_card_preflight is not None or line_plan.steam_card_count_per_set is not None:
                    raise ValueError("Listing stock transfer не должен смешиваться с ручным планом или Steam Cards")
                transferred = ListingStockService(self.database).transfer_to_order(
                    transfer_event_id=self._stable_id("stock-transfer", f"{order}:{line_id}:{item.item_id}"),
                    listing_id=str(order_row["listing_id"]),
                    config_version_id=config_id,
                    order_line_id=line_id,
                    units=item.quantity_per_order,
                    recipe_item_id=item.item_id,
                    connection=connection,
                )
                aggregated: dict[tuple[str, int, str, str, str], dict[str, Any]] = {}
                for allocation in transferred:
                    key = (
                        str(allocation["bot_name"]).casefold(),
                        int(allocation["app_id"]),
                        str(allocation["context_id"]),
                        str(allocation["game_id"]).casefold(),
                        asset_id_key(allocation["asset_id"]),
                    )
                    entry = aggregated.setdefault(key, {
                        "bot_name": str(allocation["bot_name"]),
                        "asset_id": str(allocation["asset_id"]),
                        "game_id": str(allocation["game_id"]),
                        "app_id": int(allocation["app_id"]),
                        "context_id": str(allocation["context_id"]),
                        "units": 0,
                        "allocation_ids": [],
                    })
                    entry["units"] += int(allocation["units"])
                    entry["allocation_ids"].append(str(allocation["allocation_id"]))
                transferred_resources = tuple(
                    (PlannedResource(
                        entry["bot_name"], entry["asset_id"], entry["game_id"],
                        entry["app_id"], entry["context_id"], False, None,
                        entry["units"], tuple(sorted(entry["allocation_ids"])),
                    ),)
                    for _, entry in sorted(aggregated.items(), key=lambda pair: pair[0])
                )
                if sum(unit[0].units for unit in transferred_resources) != item.quantity_per_order:
                    raise ValueError("Перенесённые listing allocations не совпадают с количеством RecipeItem")
                effective_planned[line_id] = PlannedRecipeLine(item.item_id, transferred_resources)

            flattened: list[tuple[str, PlannedResource]] = []
            seen: set[tuple[str, str]] = set()
            for line_id, line_plan in effective_planned.items():
                if not isinstance(line_id, str) or not line_id.strip() or not isinstance(line_plan, PlannedRecipeLine):
                    raise ValueError("План должен связывать каждую OrderLine с RecipeItem и его units")
                item = item_by_line[line_id]
                if line_plan.recipe_item_id != item.item_id or not isinstance(line_plan.units, tuple):
                    raise ValueError("Планируемые ресурсы относятся к другому RecipeItem")
                if line_plan.use_listing_stock:
                    raise ValueError("Listing stock plan не был перенесён в order allocation")
                if item.required and not line_plan.units and not isinstance(item.spec, DigitalTextUnitSpec):
                    raise ValueError("Обязательная RecipeItem не имеет ресурсов")
                stock_backed_line = (
                    isinstance(item.spec, SteamAssetSpec)
                    and any(
                        resource.stock_allocation_ids
                        for unit in line_plan.units for resource in unit
                    )
                )
                if isinstance(item.spec, SteamAssetSpec) and line_plan.units:
                    if stock_backed_line:
                        if any(
                            not resource.stock_allocation_ids
                            for unit in line_plan.units for resource in unit
                        ) or sum(
                            resource.units for unit in line_plan.units for resource in unit
                        ) != item.quantity_per_order:
                            raise ValueError("Listing allocation units не совпадают с количеством Steam Asset RecipeItem")
                    elif (
                        len(line_plan.units) != item.quantity_per_order
                        or any(
                            resource.units != 1 or resource.stock_allocation_ids
                            for unit in line_plan.units for resource in unit
                        )
                    ):
                        raise ValueError("Steam Asset quantity plan должен содержать по одному Asset на единицу")
                elif line_plan.units and len(line_plan.units) != item.quantity_per_order:
                    raise ValueError("Количество планируемых units не совпадает с RecipeItem.quantity")

                allowed_bots = (
                    item.spec.allowed_bot_pool
                    if isinstance(item.spec, (SteamAssetSpec, SteamCardSetSpec))
                    else ()
                ) or tuple(config_bot_pool)
                allowed_keys = {bot.strip().casefold() for bot in allowed_bots}
                if (
                    line_plan.units and not allowed_keys
                    and not stock_backed_line and item.kind != "steam_card_set"
                ):
                    raise ValueError("RecipeItem не имеет допустимого bot_pool")
                # For Steam Cards, an empty configured pool is intentional:
                # the buyer-specific preflight has already selected exact cards
                # from verified 753/6 bot snapshots, and the checks below bind
                # every reserved resource to that immutable preflight result.
                if isinstance(item.spec, SteamCardSetSpec):
                    preflight = line_plan.steam_card_preflight
                    if line_plan.units and (
                        not isinstance(preflight, MultiGameOrderPreflight)
                        or not has_verified_card_selection(preflight)
                        or preflight.ready_for_manual_review is not True
                        or preflight.order_id != order
                        or preflight.requested_sets != len(line_plan.units)
                        or len(preflight.selected_sets) != len(line_plan.units)
                    ):
                        raise ValueError("Steam Card plan требует успешный selector preflight с полным составом")
                    if not line_plan.units and preflight is not None:
                        raise ValueError("Пустой Steam Card plan не должен содержать selector preflight")
                    if line_plan.steam_card_count_per_set is not None and (
                        type(line_plan.steam_card_count_per_set) is not int
                        or line_plan.steam_card_count_per_set < 1
                    ):
                        raise ValueError("Steam Card plan содержит некорректное количество карт")
                    for unit_index, unit in enumerate(line_plan.units):
                        selected_set = preflight.selected_sets[unit_index]
                        expected_hashes = selected_set.card_hash_names
                        if (
                            not isinstance(expected_hashes, tuple)
                            or not expected_hashes
                            or not isinstance(selected_set.game_name, str)
                            or not selected_set.game_name.strip()
                            or (item.spec.game_name is not None
                                and selected_set.game_name.strip().casefold() != item.spec.game_name.casefold())
                            or any(not isinstance(value, str) or not value.strip() for value in expected_hashes)
                            or len({value.strip() for value in expected_hashes}) != len(expected_hashes)
                        ):
                            raise ValueError("Steam Card preflight содержит некорректный состав набора")
                        expected_hashes = tuple(value.strip() for value in expected_hashes)
                        if (
                            not isinstance(unit, tuple)
                            or len(unit) != len(expected_hashes)
                            or selected_set.cards_per_set != len(expected_hashes)
                            or (line_plan.steam_card_count_per_set is not None
                                and line_plan.steam_card_count_per_set != len(expected_hashes))
                        ):
                            raise ValueError("Steam Card unit не соответствует полному составу из preflight")
                        unit_seen: set[tuple[str, str]] = set()
                        unit_bots: set[str] = set()
                        unit_hashes: list[str] = []
                        planned_cards: set[tuple[str, str, str]] = set()
                        for resource in unit:
                            if not isinstance(resource, PlannedResource):
                                raise ValueError("План содержит неподдерживаемый Steam Card ресурс")
                            if (
                                resource.game_id != "steam_cards"
                                or resource.app_id != item.spec.app_id
                                or str(resource.context_id) != item.spec.context_id
                                or resource.steam_cards_compatibility is not True
                            ):
                                raise ValueError("Steam Card ресурс не соответствует RecipeItem")
                            if not isinstance(resource.steam_card_hash_name, str) or not resource.steam_card_hash_name.strip():
                                raise ValueError("Steam Card ресурс не связан с MarketHashName из preflight")
                            bot = resource.bot_name.strip()
                            asset = self._validated_asset_id(resource.asset_id)
                            key = (bot.casefold(), asset_id_key(asset))
                            if not bot or not asset or (allowed_keys and bot.casefold() not in allowed_keys):
                                raise ValueError("Steam Card ресурс вне допустимого bot_pool")
                            if key in seen or key in unit_seen:
                                raise ValueError("Один физический asset выбран более чем для одной позиции")
                            unit_seen.add(key)
                            unit_bots.add(bot.casefold())
                            unit_hashes.append(resource.steam_card_hash_name.strip())
                            planned_cards.add((bot.casefold(), asset, resource.steam_card_hash_name.strip()))
                            seen.add(key)
                            flattened.append((line_id, PlannedResource(
                                bot, asset, resource.game_id, resource.app_id,
                                str(resource.context_id), True, resource.steam_card_hash_name.strip(),
                            )))
                        if len(unit_bots) != 1:
                            raise ValueError("Steam Card unit должен принадлежать одному боту")
                        if len(set(unit_hashes)) != len(unit_hashes) or set(unit_hashes) != set(expected_hashes):
                            raise ValueError("Steam Card unit не содержит ровно по одной карте каждого типа preflight")
                        selected_cards = {
                            (asset.bot_name.strip().casefold(), self._validated_asset_id(asset.asset_id), asset.card_hash_name.strip())
                            for asset in selected_set.asset_ids
                            if isinstance(asset.card_hash_name, str) and asset.card_hash_name.strip()
                        }
                        if len(selected_cards) != len(selected_set.asset_ids) or planned_cards != selected_cards:
                            raise ValueError("Steam Card plan не совпадает с assets успешного selector preflight")
                elif isinstance(item.spec, SteamAssetSpec):
                    if line_plan.steam_card_count_per_set is not None or line_plan.steam_card_preflight is not None:
                        raise ValueError("Steam asset plan не должен содержать Steam Card метаданные")
                    for unit in line_plan.units:
                        if not isinstance(unit, tuple) or len(unit) != 1:
                            raise ValueError("Каждая Steam asset quantity должна содержать один ресурс")
                        resource = unit[0]
                        if not isinstance(resource, PlannedResource):
                            raise ValueError("План содержит неподдерживаемый Steam asset ресурс")
                        bot = resource.bot_name.strip()
                        asset = self._validated_asset_id(resource.asset_id)
                        if (
                            not bot or not asset or (allowed_keys and bot.casefold() not in allowed_keys)
                            or resource.game_id.casefold() != item.spec.game_id.casefold()
                            or type(resource.app_id) is not int or resource.app_id != item.spec.app_id
                            or str(resource.context_id) != item.spec.context_id
                            or resource.steam_cards_compatibility
                            or type(resource.units) is not int or resource.units < 1
                        ):
                            raise ValueError("Steam asset ресурс не соответствует RecipeItem, Amount или bot_pool")
                        allocation_ids = tuple(resource.stock_allocation_ids)
                        if stock_backed_line:
                            if not allocation_ids or len(set(allocation_ids)) != len(allocation_ids):
                                raise ValueError("Stock-backed resource должен иметь уникальные allocation IDs")
                            marks = ",".join("?" for _ in allocation_ids)
                            allocation_rows = connection.execute(
                                f"SELECT * FROM gd_stock_allocations WHERE allocation_id IN ({marks})",
                                allocation_ids,
                            ).fetchall()
                            if len(allocation_rows) != len(allocation_ids):
                                raise ValueError("Listing stock allocation отсутствует")
                            if any(
                                row["owner_type"] != "order"
                                or row["state"] != "active"
                                or row["order_line_id"] != line_id
                                or row["recipe_item_id"] != item.item_id
                                or row["bot_name"].casefold() != bot.casefold()
                                or row["asset_id_key"] != asset_id_key(asset)
                                or row["game_id"].casefold() != item.spec.game_id.casefold()
                                or int(row["app_id"]) != item.spec.app_id
                                or str(row["context_id"]) != item.spec.context_id
                                for row in allocation_rows
                            ):
                                raise ValueError("Listing stock allocation не совпадает с OrderLine/RecipeItem/Asset")
                            if sum(int(row["units"]) for row in allocation_rows) != resource.units:
                                raise ValueError("Amount не совпадает с суммой listing stock allocations")
                            if stock_allocation_ids_seen.intersection(allocation_ids):
                                raise ValueError("Один listing allocation повторно включён в DeliveryLeg")
                            stock_allocation_ids_seen.update(allocation_ids)
                        elif resource.units != 1 or allocation_ids:
                            raise ValueError("Multi-unit Steam Asset требует listing stock allocation")
                        key = (bot.casefold(), asset_id_key(asset))
                        if key in seen:
                            raise ValueError("Один физический asset выбран более чем для одной позиции")
                        seen.add(key)
                        flattened.append((line_id, PlannedResource(
                            bot, asset, resource.game_id, resource.app_id,
                            str(resource.context_id), False, None,
                            resource.units, allocation_ids,
                        )))
                elif isinstance(item.spec, DigitalTextUnitSpec):
                    if line_plan.units or line_plan.steam_card_count_per_set is not None or line_plan.steam_card_preflight is not None:
                        raise ValueError("DigitalTextUnit не должен содержать физические ресурсы")
                    if not item.stock_source_id:
                        raise ValueError("DigitalTextUnit требует product stock_source_id")
                else:
                    raise ValueError("Для RecipeItem нет поддерживаемого валидатора ресурсов")
            digital_lines = [
                (line_id, item) for line_id, item in item_by_line.items()
                if isinstance(item.spec, DigitalTextUnitSpec)
            ]
            if not flattened and not digital_lines:
                raise ValueError("Recipe plan не содержит резервируемых ресурсов")

            for line_id, resource in flattened:
                conflicts = connection.execute("""
                    SELECT owner_type AS conflict_owner FROM gd_resource_reservations
                    WHERE state IN ('active','legacy_conflict') AND bot_name=? COLLATE NOCASE AND asset_id_key=?
                    UNION ALL
                    SELECT 'legacy' AS conflict_owner FROM steam_asset_reservations
                    WHERE bot_name=? COLLATE NOCASE AND asset_id_key=?
                      AND order_id<>?
                      AND NOT EXISTS (
                          SELECT 1 FROM gd_legacy_order_map m
                          WHERE m.legacy_table='delivery_orders'
                            AND m.legacy_order_id=steam_asset_reservations.order_id
                            AND m.order_id=?
                      )
                """, (
                    resource.bot_name, asset_id_key(resource.asset_id),
                    resource.bot_name, asset_id_key(resource.asset_id), order, order,
                )).fetchall()
                if conflicts:
                    if all(str(row["conflict_owner"]) == "order" for row in conflicts):
                        # A ListingStock transfer may already have been staged earlier
                        # in this transaction. Roll it back before persisting only the
                        # immutable manifest and blocked status.
                        connection.rollback()
                        connection.execute("BEGIN IMMEDIATE")
                        if paid_context is not None:
                            recipe = DeliveryLedger.recipe_for_config_version(
                                connection, config_id, allow_legacy=True
                            )
                            create_playerok_order_for_preparation(
                                connection, paid_context,
                                sum(item.quantity_per_order for item in recipe.items),
                            )
                        DeliveryLedger.materialize_recipe_order_lines_in_transaction(
                            connection, order, config_id, allow_legacy=True
                        )
                        changed = connection.execute(
                            "UPDATE gd_orders SET status='preflight_blocked', "
                            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                            "WHERE order_id=? AND status IN ('pending','insufficient_stock','preflight_blocked')",
                            (order,),
                        ).rowcount
                        if changed != 1:
                            raise ValueError("Статус заказа изменился во время reservation; конфликт не сохранён")
                        return OrderReservationResult(
                            order, "preflight_blocked", (), (), (),
                            "Один из выбранных ресурсов уже имеет активный резерв.",
                        )
                    raise ResourceReservationConflict(
                        "Один из выбранных ресурсов уже имеет активный резерв"
                    )
                key = (
                    line_id, resource.bot_name, resource.app_id, str(resource.context_id),
                    str(resource.game_id), resource.steam_cards_compatibility,
                )
                grouped.setdefault(key, {})[resource.asset_id] = resource.units
                if resource.stock_allocation_ids:
                    for allocation_id in resource.stock_allocation_ids:
                        stock_backed_assets.add((key, resource.asset_id))


            # Digital codes are non-physical resources and use the same order transaction.
            digital_legs: list[PlannedLeg] = []
            for line_id, item in digital_lines:
                try:
                    code_ids = DigitalCodeStore.reserve_for_order_line(
                        connection, product_id=str(item.stock_source_id),
                        order_id=order, order_line_id=line_id,
                        quantity=item.quantity_per_order,
                    )
                except DigitalCodeError:
                    if item.required:
                        raise
                    continue
                leg_id = self._stable_id("leg", f"{order}:{line_id}:digital-code:1:code")
                connection.execute(
                    "INSERT INTO gd_delivery_legs (leg_id,order_line_id,bot_name,app_id,context_id,asset_ids_json,status) VALUES(?,?,?,?,?,?, 'reserved')",
                    (leg_id, line_id, "digital-code", 1, "code", json.dumps(list(code_ids))),
                )
                digital_legs.append(PlannedLeg("digital-code", 1, "code", code_ids, line_id))
            if not flattened and not digital_legs:
                raise DigitalCodeError("В Recipe нет обязательных физических ресурсов; доступных цифровых кодов также нет.")

            # Listing-level Steam Card quota is a count of complete sets, not physical cards.
            # Claim it in this same transaction as the verified card reservations.
            steam_card_sets = sum(
                len(effective_planned[line_id].units)
                for line_id, item in item_by_line.items()
                if isinstance(item.spec, SteamCardSetSpec) and line_id in effective_planned
            )
            if steam_card_sets and str(order_row["source_platform"]).strip().casefold() == "playerok":
                limit_row = connection.execute(
                    "SELECT total_sets FROM gd_steam_card_listing_limits WHERE listing_id=?",
                    (str(order_row["listing_id"]),),
                ).fetchone()
                if limit_row is None:
                    raise ValueError(
                        "Для объявления Steam Cards не задано общее количество наборов. "
                        "Сохраните ConfigVersion с установленным лимитом объявления."
                    )
                prior_claim = connection.execute(
                    "SELECT listing_id,config_version_id,sets,state FROM gd_steam_card_listing_claims "
                    "WHERE order_id=?",
                    (order,),
                ).fetchone()
                if prior_claim is not None:
                    if (
                        str(prior_claim["listing_id"]) != str(order_row["listing_id"])
                        or str(prior_claim["config_version_id"]) != config_id
                        or int(prior_claim["sets"]) != steam_card_sets
                        or str(prior_claim["state"]) != "reserved"
                    ):
                        raise ValueError("Для заказа уже существует несовместимый резерв лимита Steam Cards.")
                else:
                    claimed_row = connection.execute(
                        "SELECT COALESCE(SUM(sets),0) AS claimed FROM gd_steam_card_listing_claims "
                        "WHERE listing_id=? AND state='reserved'",
                        (str(order_row["listing_id"]),),
                    ).fetchone()
                    claimed = int(claimed_row["claimed"] if claimed_row else 0)
                    available = max(0, int(limit_row["total_sets"]) - claimed)
                    if available < steam_card_sets:
                        raise InsufficientListingStock(steam_card_sets, available)
                    connection.execute(
                        "INSERT INTO gd_steam_card_listing_claims(order_id,listing_id,config_version_id,sets,state) "
                        "VALUES(?,?,?,?,'reserved')",
                        (order, str(order_row["listing_id"]), config_id, steam_card_sets),
                    )

            # Quantity-stock-backed assets remain active in gd_stock_allocations.
            # This ledger supports several orders reserving disjoint units of one stack.
            legs: list[PlannedLeg] = []
            assets: list[tuple[str, str]] = []
            for (line_id, bot, app_id, context_id, game_id, card_compat), asset_units in sorted(
                grouped.items(), key=lambda entry: (lines[entry[0][0]]["line_number"], entry[0][1].casefold())
            ):
                asset_ids = tuple(asset_units)
                amount_manifest = dict(asset_units)
                group_key = (line_id, bot, app_id, context_id, game_id, card_compat)
                has_stock_amounts = any((group_key, asset_id) in stock_backed_assets for asset_id in asset_ids)
                amount_json = (
                    json.dumps(amount_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if has_stock_amounts or any(amount != 1 for amount in amount_manifest.values())
                    else None
                )
                leg_id = self._stable_id("leg", f"{order}:{line_id}:{bot.casefold()}:{app_id}:{context_id}")
                connection.execute("""
                    INSERT INTO gd_delivery_legs
                        (leg_id,order_line_id,bot_name,app_id,context_id,
                         asset_ids_json,asset_amounts_json,status)
                    VALUES(?,?,?,?,?,?,?, 'reserved')
                """, (
                    leg_id, line_id, bot, app_id, context_id,
                    json.dumps(asset_ids), amount_json,
                ))
                for asset_id in asset_ids:
                    reservation_id = self._stable_id(
                        "reservation", f"{order}:{line_id}:{bot.casefold()}:{app_id}:{context_id}:{asset_id}"
                    )
                    if (group_key, asset_id) not in stock_backed_assets:
                        connection.execute("""
                            INSERT INTO gd_resource_reservations
                                (reservation_id,owner_type,order_line_id,game_id,app_id,context_id,
                                 bot_name,asset_id,units,state)
                            VALUES(?,'order',?,?,?,?,?,?,?,'active')
                        """, (
                            reservation_id, line_id, game_id, app_id, context_id, bot,
                            asset_id, amount_manifest[asset_id],
                        ))
                    if card_compat:
                        connection.execute("""
                            INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id)
                            VALUES(?,?,?)
                        """, (bot, asset_id, order))
                    assets.append((bot, asset_id))
                legs.append(PlannedLeg(
                    bot, app_id, context_id, asset_ids, line_id,
                    amount_manifest if amount_json is not None else None,
                ))
            legs.extend(digital_legs)
            connection.execute(
                "UPDATE gd_orders SET status='reserved',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE order_id=?",
                (order,),
            )
        return OrderReservationResult(
            order, "reserved", tuple(legs), tuple(assets), (),
            "Все обязательные Recipe ресурсы атомарно зарезервированы.",
        )

    @staticmethod
    def _validated_asset_id(value: Any) -> str:
        return validate_asset_id(value)
