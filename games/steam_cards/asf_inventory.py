from __future__ import annotations

from typing import Any

from integrations.asf.client import ASFError
from games.steam_cards.selection import CardCopy


def _key(mapping: dict[str, Any], name: str) -> str | None:
    wanted = name.casefold()
    return next((key for key in mapping if key.casefold() == wanted), None)


def _items(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ASFError("ASF вернул ответ инвентаря неожиданного формата")
    success_key = _key(payload, "Success")
    if success_key is not None and payload[success_key] is False:
        raise ASFError("ASF не смог получить инвентарь")
    result_key = _key(payload, "Result")
    result = payload[result_key] if result_key else payload
    if not isinstance(result, dict):
        raise ASFError("ASF не вернул список инвентаря по ботам")
    return result


def parse_bot_inventory(payload: Any, bot_name: str, game_name: str) -> list[CardCopy]:
    """Normalize ASF BotInventoryResponse into local card records.

    This endpoint returns the whole inventory for the requested app/context.
    ASF response descriptions are matched by classid + instanceid. Only items
    whose description type explicitly identifies a trading card are classified
    as cards; unknown or unrelated items are retained as non-card inventory rows.
    """
    if not bot_name.strip() or not game_name.strip():
        raise ValueError("Имя бота и игры не должны быть пустыми")
    result = _items(payload)
    bot_key = _key(result, bot_name)
    if bot_key is None:
        raise ASFError("ASF не вернул инвентарь запрошенного бота")
    inventory = result[bot_key]
    if not isinstance(inventory, dict):
        raise ASFError("ASF вернул некорректный объект инвентаря")

    assets_key = _key(inventory, "Assets")
    descriptions_key = _key(inventory, "Descriptions")
    assets = inventory.get(assets_key, []) if assets_key else []
    descriptions = inventory.get(descriptions_key, []) if descriptions_key else []
    if assets is None or descriptions is None or not isinstance(assets, list) or not isinstance(descriptions, list):
        raise ASFError("ASF вернул некорректные Assets/Descriptions")

    description_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for description in descriptions:
        if not isinstance(description, dict):
            continue
        class_key, instance_key = _key(description, "classid"), _key(description, "instanceid")
        if class_key is None:
            continue
        class_id = str(description[class_key])
        instance_id = str(description[instance_key]) if instance_key else "0"
        description_by_identity[(class_id, instance_id)] = description

    cards: list[CardCopy] = []
    seen_asset_ids: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_key = _key(asset, "assetid") or _key(asset, "id")
        class_key = _key(asset, "classid")
        instance_key = _key(asset, "instanceid")
        if asset_key is None or class_key is None:
            continue
        asset_id = str(asset[asset_key]).strip()
        if not asset_id or asset_id in seen_asset_ids:
            continue
        seen_asset_ids.add(asset_id)
        identity = (
            str(asset[class_key]),
            str(asset[instance_key]) if instance_key else "0",
        )
        description = description_by_identity.get(identity, {})
        name_key, type_key = _key(description, "name"), _key(description, "type")
        item_name = str(description.get(name_key, "")).strip() if name_key else ""
        item_type = str(description.get(type_key, "")).strip() if type_key else ""
        normalized_type = item_type.casefold()
        is_card = "trading card" in normalized_type
        is_foil = is_card and "foil" in normalized_type
        cards.append(CardCopy(
            asset_id=asset_id,
            game_name=game_name.strip(),
            card_name=item_name or (f"Unknown item {asset_id}"),
            is_trading_card=is_card,
            is_foil=is_foil,
        ))
    return cards
