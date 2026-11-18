from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable, Mapping
from datetime import datetime, timezone

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.reservation_service import (
    DeliveryReservationService,
    InventorySnapshot,
    OrderReservationResult,
    PlannedRecipeLine,
    PlannedResource,
)
from core.delivery.playerok_eligibility import require_canonical_playerok_delivery_eligible
from core.delivery.recipes import (
    DigitalTextUnitSpec,
    RecipeValidationError,
    SteamAssetSpec,
    SteamCardSetSpec,
    legacy_materialized_order_line,
    normalize_legacy_cs2_selector_options,
)
from games.catalog import GAME_CATALOG
from games.asset_ids import asset_id_key, validate_asset_id
from games.steam_cards.order_preflight import SteamCardOrderPreflight, MultiGameOrderPreflight
from games.steam_cards.recipient_badge import RecipientBadge
from games.steam_cards.reservations import AssetReservationStore
from games.steam_cards.selection import CardCopy


NO_CARDS_MESSAGE = (
    "Подходящих карт для вашего аккаунта нет, ожидаем продавца в чате для решения проблемы"
)


class DeliveryOrderPipeline:
    """Bridge listing-backed Playerok orders into the canonical delivery ledger.

    Marketplace/browser I/O is deliberately outside this service. Card orders
    require verified buyer badge/inventory data supplied by a trusted provider;
    unverified or missing data is rejected by SteamCardOrderPreflight.
    """

    def __init__(self, database: Database):
        self.database = database
        self.database.initialize()
        self.ledger = DeliveryLedger(database)
        self.card_reservations = AssetReservationStore(database.path)
        self.cs2_reservations = DeliveryReservationService(database)

    def _config_for_order(self, order_id: str) -> dict[str, Any]:
        with self.database.connect() as db:
            require_canonical_playerok_delivery_eligible(db, order_id)
            row = db.execute(
                """
                SELECT o.order_id,o.status,o.total_quantity,d.source_platform,
                       d.listing_id,d.config_version_id,c.recipe_schema_version,c.delivery_quantity_per_deal,
                       c.item_selectors_json,c.bot_pool_json
                FROM gd_orders o
                JOIN gd_deals d ON d.deal_id=o.deal_id
                JOIN gd_config_versions c ON c.config_version_id=d.config_version_id
                WHERE o.order_id=?
                """,
                (order_id.strip(),),
            ).fetchone()
        if row is None:
            raise KeyError(f"Канонический заказ {order_id} не найден")
        try:
            selectors = json.loads(row["item_selectors_json"])
            bot_pool = json.loads(row["bot_pool_json"])
        except (TypeError, ValueError) as exc:
            raise ValueError("ConfigVersion содержит некорректный JSON") from exc
        if not isinstance(selectors, (dict, list)) or not isinstance(bot_pool, list):
            raise ValueError("ConfigVersion содержит некорректные селекторы или пул ботов")
        return {**dict(row), "selectors": selectors, "bot_pool": bot_pool}

    @staticmethod
    def _selector_options(raw: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
        values = [raw] if isinstance(raw, dict) else raw
        if not values or any(not isinstance(value, dict) for value in values):
            raise ValueError("item_selectors должен быть объектом или списком объектов")
        return values

    @staticmethod
    def _is_card_config(config: dict[str, Any]) -> bool:
        options = DeliveryOrderPipeline._selector_options(config["selectors"])
        return any(
            (
                str(option.get("item_type") or option.get("delivery_type") or "").strip().casefold()
                in {"steam_card_set", "trading_card_set", "card_set"}
                or (option.get("delivery_mode") == "steam_cards"
                    and option.get("app_id") == 753)
            )
            and option.get("app_id", 753) == 753
            and str(option.get("context_id", 6)) == "6"
            for option in options
        )

    @staticmethod
    def _is_cs2_config(config: dict[str, Any]) -> bool:
        raw = config["selectors"]
        try:
            options = normalize_legacy_cs2_selector_options(raw)
        except RecipeValidationError:
            return False
        if options is None:
            options = DeliveryOrderPipeline._selector_options(raw)
        return bool(options) and all(
            str(option.get("game_id") or "").strip().casefold() == "cs2"
            and option.get("app_id") == 730
            and str(option.get("context_id")) == "2"
            for option in options
        )

    @staticmethod
    def _stable_id(prefix: str, value: str) -> str:
        return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"

    def _record_card_shortage(self, order_id: str, config: dict[str, Any], reason: str) -> tuple[str, ...]:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE gd_orders SET status='insufficient_stock', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE order_id=?",
                (order_id,),
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS gd_delivery_events (
                    event_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL REFERENCES gd_orders(order_id) ON DELETE RESTRICT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    UNIQUE(order_id,event_type)
                )
                """
            )
            deal = db.execute(
                "SELECT deal_id,external_deal_id,chat_id FROM gd_deals WHERE deal_id="
                "(SELECT deal_id FROM gd_orders WHERE order_id=?)",
                (order_id,),
            ).fetchone()
            common = {
                "deal_id": deal["external_deal_id"] if deal else "",
                "chat_id": deal["chat_id"] if deal else "",
                "reason": reason,
            }
            events = (
                ("buyer_notification", {**common, "message": NO_CARDS_MESSAGE}),
                ("telegram_notification", {**common, "event": "insufficient_card_sets"}),
            )
            ids = []
            for event_type, payload in events:
                event_id = self._stable_id("event", order_id + ":" + event_type)
                db.execute(
                    "INSERT OR IGNORE INTO gd_delivery_events(event_id,order_id,event_type,payload_json) "
                    "VALUES(?,?,?,?)",
                    (event_id, order_id, event_type,
                     json.dumps(payload, ensure_ascii=False, sort_keys=True)),
                )
                ids.append(event_id)
        if deal is not None and str(deal["chat_id"] or "").strip():
            # Queue the buyer response for the embedded Playerok browser (Part B).
            # No direct/private Playerok mutation is performed from this pipeline.
            from integrations.telegram.outbox import BuyerPlatformMessageOutbox
            BuyerPlatformMessageOutbox(self.database).enqueue(
                str(deal["deal_id"]), str(deal["chat_id"]), NO_CARDS_MESSAGE
            )
        return tuple(ids)

    def prepare_steam_card_order(
        self,
        *,
        order_id: str,
        inventories: Mapping[str, Iterable[CardCopy]],
        recipient_badges: Iterable[RecipientBadge],
        badge_data_verified: bool,
        buyer_complete_sets_by_app_id: Mapping[int, int],
        buyer_inventory_verified: bool,
        seller_inventory_verified: bool,
        card_hashes_by_app_id: Mapping[int, Iterable[str]],
        card_definitions_verified: bool,
    ) -> MultiGameOrderPreflight:
        """Allocate exactly ConfigVersion sets, then persist canonical legs/reserves."""
        order = order_id.strip()
        if not order:
            raise ValueError("order_id обязателен")
        config = self._config_for_order(order)
        if not self._is_card_config(config):
            raise ValueError("Заказ не настроен как Steam Trading Cards 753/6")
        quantity = config["delivery_quantity_per_deal"]
        if type(quantity) is not int or quantity < 1:
            raise ValueError("ConfigVersion не содержит положительное количество полных наборов")
        if config["status"] in {"DELIVERED", "CANCELLED", "UNKNOWN", "DECLINED"}:
            raise ValueError(f"Заказ заблокирован для подготовки: {config['status']}")
        with self.database.connect() as db:
            has_lines = db.execute(
                "SELECT 1 FROM gd_order_lines WHERE order_id=? LIMIT 1", (order,)
            ).fetchone()
            seed_line = legacy_materialized_order_line(
                db, order, str(config["config_version_id"])
            ) if has_lines else None
            canonical_reserved = {
                (str(row["bot_name"]).casefold(), asset_id_key(row["asset_id"]))
                for row in db.execute(
                    "SELECT bot_name,asset_id FROM gd_resource_reservations WHERE state IN ('active','legacy_conflict')"
                ).fetchall()
            }
        if has_lines and seed_line is None:
            raise ValueError("Для заказа уже созданы OrderLine; повторное планирование запрещено")

        allowed_bots = {
            str(name).strip().casefold()
            for name in config["bot_pool"]
            if isinstance(name, str) and name.strip()
        }
        filtered: dict[str, list[CardCopy]] = {}
        for bot, cards in inventories.items():
            bot_name = str(bot).strip()
            if bot_name.casefold() not in allowed_bots:
                continue
            filtered[bot_name] = [
                card for card in cards
                if (bot_name.casefold(), asset_id_key(card.asset_id)) not in canonical_reserved
            ]

        catalog_names = {
            game.name.strip().casefold(): game.app_id
            for game in GAME_CATALOG if game.module_id != "steam_cards"
        }
        game_app_ids_by_name: dict[str, int] = {}
        game_by_app: dict[int, str] = {
            game.app_id: game.name
            for game in GAME_CATALOG if game.module_id != "steam_cards"
        }
        for cards in filtered.values():
            for card in cards:
                if not card.is_trading_card:
                    continue
                game_name = str(card.game_name or "").strip()
                game_key = game_name.casefold()
                app_id = card.game_app_id
                if app_id is None:
                    app_id = catalog_names.get(game_key)
                if app_id is None:
                    continue
                if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
                    raise ValueError(f"ASF вернул некорректный AppID игры для {game_name or card.asset_id}")
                known_app = game_app_ids_by_name.get(game_name)
                known_name = game_by_app.get(app_id)
                if known_app is not None and known_app != app_id:
                    raise ValueError(f"ASF вернул несколько AppID для игры {game_name}")
                if known_name is not None and known_name.casefold() != game_key:
                    raise ValueError(f"ASF связал AppID {app_id} с несколькими названиями игр")
                game_app_ids_by_name[game_name] = app_id
                game_by_app[app_id] = game_name

        preflight = SteamCardOrderPreflight(self.card_reservations).plan_multi_game(
            order_id=order,
            requested_sets=quantity,
            inventories=filtered,
            game_app_ids_by_name=game_app_ids_by_name,
            recipient_badges=recipient_badges,
            badge_data_verified=badge_data_verified,
            buyer_complete_sets_by_app_id=buyer_complete_sets_by_app_id,
            buyer_inventory_verified=buyer_inventory_verified,
            seller_inventory_verified=seller_inventory_verified,
            card_hashes_by_app_id=card_hashes_by_app_id,
            card_definitions_verified=card_definitions_verified,
        )
        if not preflight.ready_for_manual_review:
            shortage = preflight.message.startswith("Точное количество наборов недоступно:")
            if shortage:
                self._record_card_shortage(order, config, preflight.message)
            else:
                with self.database.connect() as db:
                    db.execute(
                        "UPDATE gd_orders SET status='preflight_blocked', "
                        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE order_id=?",
                        (order,),
                    )
            return preflight

        allocations = {allocation.app_id: allocation for allocation in preflight.allocations}
        with self.database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                require_canonical_playerok_delivery_eligible(db, order)
                current = db.execute(
                    "SELECT status FROM gd_orders WHERE order_id=?", (order,)
                ).fetchone()
                if current is None or current["status"] in {"DELIVERED", "CANCELLED", "UNKNOWN"}:
                    raise ValueError("Статус заказа изменился; резервация отменена")
                for app_id, allocation in sorted(allocations.items()):
                    game_name = game_by_app.get(app_id)
                    if not game_name:
                        raise ValueError(f"Для AppID {app_id} нет определения игры")
                    game_sets = tuple(
                        selected for selected in preflight.selected_sets
                        if selected.game_name.casefold() == game_name.casefold()
                    )
                    if len(game_sets) != allocation.sets:
                        raise ValueError("Состав выбранных наборов не совпадает с allocation")
                    line_id = str(seed_line["order_line_id"]) if seed_line else f"{order}:line:{app_id}"
                    selector = {
                        "game_id": "steam_cards",
                        "app_id": 753,
                        "context_id": 6,
                        "item_type": "steam_card_set",
                        "game_app_id": app_id,
                        "game_name": game_name,
                        "set_count": allocation.sets,
                    }
                    if seed_line is None:
                        db.execute(
                            "INSERT INTO gd_order_lines "
                            "(order_line_id,order_id,config_version_id,line_number,item_type,item_name,item_selector_json,quantity) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (line_id, order, str(config["config_version_id"]),
                             sum(1 for prior in allocations if prior < app_id) + 1,
                             "steam_card_set", game_name,
                             json.dumps(selector, ensure_ascii=False, sort_keys=True), allocation.sets),
                        )
                    by_bot: dict[str, list[str]] = {}
                    for selected_set in game_sets:
                        for asset in selected_set.asset_ids:
                            by_bot.setdefault(asset.bot_name, []).append(asset.asset_id)
                    for bot_name, raw_assets in sorted(by_bot.items(), key=lambda value: value[0].casefold()):
                        assets = list(dict.fromkeys(str(asset).strip() for asset in raw_assets))
                        if not assets or len(assets) != len(raw_assets):
                            raise ValueError("В выбранных полных наборах найдены повторяющиеся asset ID")
                        leg_id = self._stable_id("leg", order + f":{app_id}:" + bot_name.casefold())
                        db.execute(
                            "INSERT INTO gd_delivery_legs "
                            "(leg_id,order_line_id,bot_name,app_id,context_id,asset_ids_json,status) "
                            "VALUES(?,?,?,?,?,?, 'reserved')",
                            (leg_id, line_id, bot_name, 753, "6",
                             json.dumps(assets, ensure_ascii=False)),
                        )
                        for asset_id in assets:
                            reservation_id = self._stable_id(
                                "reservation", order + ":" + bot_name.casefold() + ":" + asset_id
                            )
                            db.execute(
                                "INSERT INTO gd_resource_reservations "
                                "(reservation_id,owner_type,order_line_id,game_id,app_id,context_id,"
                                "bot_name,asset_id,units,state) VALUES(?,'order',?,'steam_cards',753,'6',?,?,1,'active')",
                                (reservation_id, line_id, bot_name, asset_id),
                            )
                            # Legacy send/review code still reads this table;
                            # it is a compatibility projection of the canonical
                            # reservation and is written in the same transaction.
                            db.execute(
                                "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) "
                                "VALUES(?,?,?)",
                                (bot_name, asset_id, order),
                            )
                db.execute(
                    "UPDATE gd_orders SET status='reserved',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE order_id=?",
                    (order,),
                )
        return preflight

    def prepare_cs2_order(
        self,
        *,
        order_id: str,
        snapshots: Mapping[str, InventorySnapshot],
    ) -> OrderReservationResult:
        """Reserve ConfigVersion-selected CS2 730/2 assets across the configured bot pool."""
        config = self._config_for_order(order_id)
        if not self._is_cs2_config(config):
            raise ValueError("Заказ не настроен как CS2 730/2")
        return self.cs2_reservations.reserve_order(
            order_id=order_id,
            config_version_id=str(config["config_version_id"]),
            snapshots=snapshots,
        )

    def prepare_recipe_order(
        self,
        *,
        order_id: str,
        snapshots: Mapping[str, InventorySnapshot],
        recipient_badges: Iterable[RecipientBadge] = (),
        badge_data_verified: bool = False,
        buyer_complete_sets_by_app_id: Mapping[int, int] | None = None,
        buyer_inventory_verified: bool = False,
        seller_inventory_verified: bool = False,
        card_hashes_by_app_id: Mapping[int, Iterable[str]] | None = None,
        card_definitions_verified: bool = False,
        game_app_ids_by_name: Mapping[str, int] | None = None,
        now: datetime | None = None,
    ) -> OrderReservationResult:
        """Plan every RecipeItem, then reserve the complete order in one ledger transaction.

        This is preparation only: it does not call ASF/Steam or dispatch DeliveryLegs.
        Buyer/card metadata is supplied by the existing verified preflight provider.
        """
        order = str(order_id or "").strip()
        if not order or not isinstance(snapshots, Mapping):
            raise ValueError("order_id и snapshots обязательны")
        config = self._config_for_order(order)
        config_id = str(config["config_version_id"])
        # Materialization is idempotent and remains a separate short transaction;
        # no inventory or reservation state is changed during this step.
        self.ledger.materialize_recipe_order_lines(order, allow_legacy=True)
        current_time = self.cs2_reservations._aware(now or datetime.now(timezone.utc))
        with self.database.connect() as connection:
            recipe = self.ledger.recipe_for_config_version(connection, config_id, allow_legacy=True)
            line_rows = connection.execute(
                "SELECT order_line_id,line_number,config_version_id FROM gd_order_lines "
                "WHERE order_id=? ORDER BY line_number",
                (order,),
            ).fetchall()
        if len(line_rows) != len(recipe.items):
            raise ValueError("Число OrderLine не совпадает с закреплённым Recipe")
        lines_by_position = {int(row["line_number"]): str(row["order_line_id"]) for row in line_rows}
        if any(str(row["config_version_id"] or "") != config_id for row in line_rows):
            raise ValueError("OrderLine привязана к другой ConfigVersion")

        reserved = set(self.card_reservations.list_reserved_assets(excluding_order_id=order))
        reserved_keys = {(bot.casefold(), asset_id_key(asset)) for bot, asset in reserved}
        planned: dict[str, PlannedRecipeLine] = {}
        failed_required: list[str] = []
        card_items = [item for item in recipe.items if item.kind == "steam_card_set"]

        for item in recipe.items:
            line_id = lines_by_position.get(item.position)
            if not line_id:
                raise ValueError("Для RecipeItem отсутствует соответствующая OrderLine")
            resources: list[PlannedResource] = []
            if isinstance(item.spec, SteamCardSetSpec):
                if item.spec.game_name is None and len(card_items) > 1:
                    reason = "В рецепте несколько Steam Card позиций без game_name; распределение неоднозначно."
                    if item.required:
                        failed_required.append(reason)
                    planned[line_id] = PlannedRecipeLine(item.item_id, ())
                    continue
                allowed = item.spec.allowed_bot_pool or tuple(config["bot_pool"])
                if not allowed:
                    reason = f"Для RecipeItem {item.item_id} не задан разрешённый пул ботов."
                    if item.required:
                        failed_required.append(reason)
                    planned[line_id] = PlannedRecipeLine(item.item_id, ())
                    continue
                card_inventories: dict[str, list[CardCopy]] = {}
                seen_contexts: set[tuple[str, int, str]] = set()
                for key, snapshot in snapshots.items():
                    bot_name = str(snapshot.bot_name).strip()
                    if bot_name.casefold() not in {value.casefold() for value in allowed}:
                        continue
                    if not self.cs2_reservations._snapshot_key_matches(key, snapshot):
                        continue
                    if not self.cs2_reservations._snapshot_is_fresh(snapshot, current_time):
                        continue
                    if (snapshot.app_id, str(snapshot.context_id)) != (753, "6"):
                        continue
                    slot = (bot_name.casefold(), snapshot.app_id, str(snapshot.context_id))
                    if slot in seen_contexts:
                        raise ValueError("Для Steam Cards получено несколько снимков одного bot/context")
                    seen_contexts.add(slot)
                    cards = [
                        value for value in snapshot.items
                        if isinstance(value, CardCopy)
                        and (item.spec.game_name is None or value.game_name.strip().casefold() == item.spec.game_name.casefold())
                        and (bot_name.casefold(), asset_id_key(value.asset_id)) not in reserved_keys
                    ]
                    if cards:
                        card_inventories[bot_name] = cards
                result = SteamCardOrderPreflight(self.card_reservations).plan_multi_game(
                    order_id=order,
                    requested_sets=item.quantity_per_order,
                    inventories=card_inventories,
                    recipient_badges=tuple(recipient_badges),
                    badge_data_verified=badge_data_verified,
                    buyer_complete_sets_by_app_id=buyer_complete_sets_by_app_id or {},
                    buyer_inventory_verified=buyer_inventory_verified,
                    seller_inventory_verified=seller_inventory_verified,
                    card_hashes_by_app_id=card_hashes_by_app_id or {},
                    card_definitions_verified=card_definitions_verified,
                    game_app_ids_by_name=game_app_ids_by_name,
                    additional_reserved_assets=tuple(reserved_keys),
                )
                if result.ready_for_manual_review:
                    card_sizes = {selected_set.cards_per_set for selected_set in result.selected_sets}
                    if len(card_sizes) != 1:
                        raise ValueError("Steam Card selection содержит неодинаковый размер наборов")
                    card_count = next(iter(card_sizes))
                    card_units = tuple(
                        tuple(
                            PlannedResource(
                                asset.bot_name, validate_asset_id(asset.asset_id), "steam_cards", 753, "6", True,
                                asset.card_hash_name,
                            )
                            for asset in selected_set.asset_ids
                        )
                        for selected_set in result.selected_sets
                    )
                    planned[line_id] = PlannedRecipeLine(
                        item.item_id, card_units, steam_card_count_per_set=card_count,
                        steam_card_preflight=result,
                    )
                    resources = [resource for unit in card_units for resource in unit]
            elif isinstance(item.spec, SteamAssetSpec):
                allowed = item.spec.allowed_bot_pool or tuple(config["bot_pool"])
                selector = {
                    "game_id": item.spec.game_id,
                    "app_id": item.spec.app_id,
                    "context_id": item.spec.context_id,
                    **dict(item.spec.selector),
                }
                available_snapshots = {}
                for snapshot_key, snapshot in snapshots.items():
                    filtered_items = tuple(
                        value for value in snapshot.items
                        if (self.cs2_reservations._normal(
                                self.cs2_reservations._get(value, "bot_name", snapshot.bot_name)),
                                asset_id_key(validate_asset_id(self.cs2_reservations._get(value, "asset_id")))) not in reserved_keys
                    )
                    available_snapshots[snapshot_key] = InventorySnapshot(
                        snapshot.bot_name, snapshot.game_id, snapshot.app_id, snapshot.context_id,
                        snapshot.refreshed_at, filtered_items, snapshot.asf_verified,
                    )
                candidates = self.cs2_reservations._select_assets(
                    selector, item.quantity_per_order, available_snapshots, tuple(allowed), current_time
                ) if allowed else ()
                units = tuple(
                    (PlannedResource(bot, asset, item.spec.game_id, app_id, context_id),)
                    for bot, asset, app_id, context_id in candidates[:item.quantity_per_order]
                ) if len(candidates) >= item.quantity_per_order else ()
                planned[line_id] = PlannedRecipeLine(item.item_id, units)
                resources = [unit[0] for unit in units]
            elif isinstance(item.spec, DigitalTextUnitSpec):
                # No digital stock source exists yet. Required rows fail closed;
                # optional rows are explicitly omitted from this reservation plan.
                resources = []
                planned[line_id] = PlannedRecipeLine(item.item_id, ())
            else:
                raise RecipeValidationError("Для RecipeItem нет поддерживаемого планировщика ресурсов")

            if not resources and item.required:
                failed_required.append(
                    f"Обязательная RecipeItem {item.item_id} не может быть полностью обеспечена."
                )
            if line_id not in planned:
                if isinstance(item.spec, SteamCardSetSpec):
                    planned[line_id] = PlannedRecipeLine(item.item_id, ())
                elif isinstance(item.spec, SteamAssetSpec):
                    planned[line_id] = PlannedRecipeLine(
                        item.item_id,
                        tuple((resource,) for resource in resources),
                    )
                else:
                    planned[line_id] = PlannedRecipeLine(item.item_id, ())
            reserved_keys.update((resource.bot_name.casefold(), asset_id_key(resource.asset_id)) for resource in resources)

        if failed_required:
            return OrderReservationResult(
                order, "preflight_blocked", (), (), (), " ".join(failed_required)
            )
        if not any(line_plan.units for line_plan in planned.values()):
            return OrderReservationResult(
                order, "preflight_blocked", (), (), (),
                "В Recipe нет обязательных физических ресурсов для безопасного резервирования.",
            )
        return self.cs2_reservations.reserve_recipe_plan(
            order_id=order,
            config_version_id=config_id,
            planned_by_line=planned,
        )


    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel only a canonical order that has never crossed the ASF dispatch boundary."""
        order = str(order_id or "").strip()
        if not order:
            raise ValueError("order_id обязателен")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,deal_id FROM gd_orders WHERE order_id=?", (order,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Канонический заказ {order} не найден")
            if row["status"] not in {"pending", "preflight_blocked", "insufficient_stock", "reserved"}:
                raise ValueError(
                    "Отменить можно только заказ до начала отправки. "
                    "Для UNKNOWN/SENT/PROCESSING требуется сверка фактических Steam offers."
                )
            attempted = db.execute(
                "SELECT 1 FROM gd_delivery_attempts WHERE order_id=? LIMIT 1", (order,)
            ).fetchone()
            if attempted:
                raise ValueError("Для заказа уже создавалась попытка ASF; автоматическое снятие резерва запрещено.")
            legs = db.execute(
                "SELECT l.leg_id,l.status FROM gd_delivery_legs l "
                "JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id WHERE ol.order_id=?",
                (order,),
            ).fetchall()
            if any(leg["status"] not in {"reserved", "planned"} for leg in legs):
                raise ValueError("Одна из DeliveryLeg уже изменила состояние; автоматическая отмена заблокирована.")
            db.execute(
                "UPDATE gd_delivery_legs SET status='CANCELLED', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE leg_id IN (SELECT l.leg_id FROM gd_delivery_legs l "
                "JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id WHERE ol.order_id=?)",
                (order,),
            )
            released = db.execute(
                "UPDATE gd_resource_reservations SET state='released', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE order_line_id IN (SELECT order_line_id FROM gd_order_lines WHERE order_id=?) "
                "AND state='active'",
                (order,),
            ).rowcount
            db.execute(
                "UPDATE gd_orders SET status='CANCELLED', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE order_id=?",
                (order,),
            )
        # Card preflight retains a compatibility reservation in the legacy table.
        # Release it only after the canonical transaction has committed.
        released_legacy = self.card_reservations.release(order)
        return {
            "order_id": order,
            "status": "CANCELLED",
            "released_canonical_reservations": released,
            "released_compatibility_reservations": released_legacy,
        }
