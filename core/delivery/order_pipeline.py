from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable, Mapping

from core.database.db import Database
from core.delivery.reservation_service import (
    DeliveryReservationService,
    InventorySnapshot,
    OrderReservationResult,
)
from games.catalog import GAME_CATALOG
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
        self.card_reservations = AssetReservationStore(database.path)
        self.cs2_reservations = DeliveryReservationService(database)

    def _config_for_order(self, order_id: str) -> dict[str, Any]:
        with self.database.connect() as db:
            row = db.execute(
                """
                SELECT o.order_id,o.status,o.total_quantity,d.source_platform,
                       d.listing_id,d.config_version_id,c.delivery_quantity_per_deal,
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
            str(option.get("item_type") or option.get("delivery_type") or "").strip().casefold()
            in {"steam_card_set", "trading_card_set", "card_set"}
            and option.get("app_id", 753) == 753
            and str(option.get("context_id", 6)) == "6"
            for option in options
        )

    @staticmethod
    def _is_cs2_config(config: dict[str, Any]) -> bool:
        options = DeliveryOrderPipeline._selector_options(config["selectors"])
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
            canonical_reserved = {
                (str(row["bot_name"]).casefold(), str(row["asset_id"]))
                for row in db.execute(
                    "SELECT bot_name,asset_id FROM gd_resource_reservations WHERE state='active'"
                ).fetchall()
            }
        if has_lines:
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
                if (bot_name.casefold(), str(card.asset_id).strip()) not in canonical_reserved
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

        preflight = SteamCardOrderPreflight(self.card_reservations).prepare_multi_game(
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
        try:
            with self.database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
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
                    line_id = f"{order}:line:{app_id}"
                    selector = {
                        "game_id": "steam_cards",
                        "app_id": 753,
                        "context_id": 6,
                        "item_type": "steam_card_set",
                        "game_app_id": app_id,
                        "game_name": game_name,
                        "set_count": allocation.sets,
                    }
                    db.execute(
                        "INSERT INTO gd_order_lines "
                        "(order_line_id,order_id,line_number,item_type,item_name,item_selector_json,quantity) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (line_id, order, sum(1 for prior in allocations if prior < app_id) + 1, "steam_card_set", game_name,
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
                db.execute(
                    "UPDATE gd_orders SET status='reserved',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE order_id=?",
                    (order,),
                )
        except Exception:
            self.card_reservations.release(order)
            raise

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
