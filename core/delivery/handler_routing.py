from __future__ import annotations

import json
from typing import Any

from core.delivery.ledger import DeliveryLedger
from core.delivery.recipes import RecipeItem, SteamAssetSpec


class DeliveryHandlerRouteError(ValueError):
    """A leg has no verified immutable Recipe route."""


def load_leg_handler_request(
    connection, *, order_id: str, leg_id: str, allow_legacy_reconcile: bool = False
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT l.leg_id,l.bot_name,l.app_id,l.context_id,ol.order_id,ol.config_version_id,ol.line_number,ol.item_type,
               ol.item_name,ol.item_selector_json,ol.quantity,d.config_version_id AS deal_config_id,c.bot_pool_json
        FROM gd_delivery_legs l
        JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id
        JOIN gd_orders o ON o.order_id=ol.order_id
        JOIN gd_deals d ON d.deal_id=o.deal_id
        JOIN gd_config_versions c ON c.config_version_id=ol.config_version_id
        WHERE l.leg_id=? AND o.order_id=?
        """,
        (leg_id, order_id),
    ).fetchone()
    if row is None:
        raise DeliveryHandlerRouteError("DeliveryLeg отсутствует в заказе")
    try:
        leg_app_id = int(row["app_id"])
    except (TypeError, ValueError) as exc:
        raise DeliveryHandlerRouteError("DeliveryLeg contains invalid AppID") from exc
    leg_context_id = str(row["context_id"])
    config_id = str(row["config_version_id"] or "")
    if not config_id or config_id != str(row["deal_config_id"] or ""):
        raise DeliveryHandlerRouteError("OrderLine не закреплён за ConfigVersion сделки")
    try:
        recipe = DeliveryLedger.recipe_for_config_version(
            connection, config_id, allow_legacy=True
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DeliveryHandlerRouteError("ConfigVersion не подтверждает маршрут DeliveryLeg") from exc
    try:
        snapshot = json.loads(row["item_selector_json"])
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "config_version_id", "recipe_item_id", "recipe_item"
        }:
            raise ValueError("неверная структура Recipe snapshot")
        item = RecipeItem.from_dict(snapshot["recipe_item"])
        expected = next(candidate for candidate in recipe.items if candidate.position == item.position)
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        if allow_legacy_reconcile and row["item_type"] == "cs2_item" and len(recipe.items) == 1:
            item = recipe.items[0]
            if (
                item.kind == "steam_asset"
                and item.executor_key == "steam_trade_offer"
                and isinstance(item.spec, SteamAssetSpec)
                and item.spec.game_id == "cs2"
                and item.spec.app_id == leg_app_id
                and item.spec.context_id == leg_context_id
                and str(row["bot_name"]).casefold()
                in {bot.casefold() for bot in item.spec.allowed_bot_pool}
            ):
                return {
                    "order_id": str(order_id), "leg_id": str(leg_id),
                    "item_type": item.kind, "delivery_method": item.executor_key,
                    "recipe_item_id": item.item_id, "config_version_id": config_id,
                    "app_id": leg_app_id, "context_id": leg_context_id,
                    "legacy_reconcile_only": True,
                }
        raise DeliveryHandlerRouteError("Recipe snapshot не подтверждает маршрут DeliveryLeg") from exc
    if (
        snapshot["config_version_id"] != config_id
        or snapshot["recipe_item_id"] != item.item_id
        or item.to_dict() != expected.to_dict()
        or row["item_type"] != item.kind
        or row["item_name"] != item.display_name
        or row["line_number"] != item.position
        or row["quantity"] != item.quantity_per_order
    ):
        raise DeliveryHandlerRouteError("OrderLine не совпадает с Recipe ConfigVersion")
    if isinstance(item.spec, SteamAssetSpec) and item.spec.game_id.casefold() == "cs2":
        try:
            configured_bots = json.loads(row["bot_pool_json"] or "[]")
        except (TypeError, ValueError) as exc:
            raise DeliveryHandlerRouteError("ConfigVersion содержит некорректный bot pool") from exc
        if not isinstance(configured_bots, list):
            raise DeliveryHandlerRouteError("ConfigVersion содержит некорректный bot pool")
        allowed_bots = item.spec.allowed_bot_pool or tuple(
            bot for bot in configured_bots if isinstance(bot, str) and bot.strip()
        )
        if (
            leg_app_id != 730
            or leg_context_id != "2"
            or (allowed_bots and str(row["bot_name"]).casefold()
                not in {bot.casefold() for bot in allowed_bots})
        ):
            raise DeliveryHandlerRouteError("CS2 DeliveryLeg не совпадает с закреплёнными AppID/context/bot pool")
        if allow_legacy_reconcile:
            return {
                "order_id": str(order_id), "leg_id": str(leg_id),
                "item_type": item.kind, "delivery_method": item.executor_key,
                "recipe_item_id": item.item_id, "config_version_id": config_id,
                "app_id": leg_app_id, "context_id": leg_context_id,
                "reconcile_only": True,
            }
        # CS2 remains disabled until its dedicated ASF capability has separate acceptance.
        return {
            "order_id": str(order_id), "leg_id": str(leg_id),
            "item_type": "cs2_item", "delivery_method": item.executor_key,
            "recipe_item_id": item.item_id, "config_version_id": config_id,
            "app_id": leg_app_id, "context_id": leg_context_id,
        }
    return {
        "order_id": str(order_id), "leg_id": str(leg_id),
        "item_type": item.kind, "delivery_method": item.executor_key,
        "recipe_item_id": item.item_id, "config_version_id": config_id,
    }
