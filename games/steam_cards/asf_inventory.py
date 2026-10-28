from __future__ import annotations

from typing import Any

from integrations.asf.client import ASFError
from games.steam_cards.selection import CardCopy


def _key(mapping: dict[str, Any], name: str) -> str | None:
    wanted = name.casefold()
    return next((key for key in mapping if key.casefold() == wanted), None)


def _optional_flag(mapping: dict[str, Any], name: str, asset_label: str) -> bool | None:
    """Read a Steam item flag without treating missing data as permission."""
    key = _key(mapping, name)
    if key is None:
        return None
    value = mapping[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true"}:
            return True
        if normalized in {"0", "false"}:
            return False
    raise ASFError(f"ASF вернул некорректный флаг {name} у предмета {asset_label}")


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

    Reject incomplete asset descriptions and cards without an authoritative game
    tag, rather than silently saving an incomplete or incorrectly grouped snapshot.
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
    if assets_key is None or descriptions_key is None:
        raise ASFError("ASF не подтвердил полный ответ инвентаря; существующий снимок сохранён")
    assets = inventory[assets_key]
    descriptions = inventory[descriptions_key]
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
            raise ASFError("ASF вернул некорректный предмет; существующий снимок сохранён")
        asset_key = _key(asset, "assetid") or _key(asset, "id")
        class_key = _key(asset, "classid")
        instance_key = _key(asset, "instanceid")
        if asset_key is None or class_key is None:
            raise ASFError("ASF вернул предмет без asset ID или class ID; снимок не обновлён")
        asset_id = str(asset[asset_key]).strip()
        if not asset_id:
            raise ASFError("ASF вернул пустой asset ID; снимок не обновлён")
        if asset_id in seen_asset_ids:
            raise ASFError("ASF вернул повторяющийся asset ID; снимок не обновлён")
        seen_asset_ids.add(asset_id)
        identity = (
            str(asset[class_key]),
            str(asset[instance_key]) if instance_key else "0",
        )
        description = description_by_identity.get(identity)
        if description is None:
            raise ASFError(f"Для предмета {asset_id} не найдены данные описания; старый снимок сохранён")

        name_key, type_key = _key(description, "name"), _key(description, "type")
        item_name = str(description.get(name_key, "")).strip() if name_key else ""
        item_type = str(description.get(type_key, "")).strip() if type_key else ""
        normalized_type = item_type.casefold()
        is_card = "trading card" in normalized_type
        is_foil = is_card and "foil" in normalized_type
        actual_game_name = game_name.strip()
        has_game_tag = False
        tags_key = _key(description, "tags")
        tags = description.get(tags_key, []) if tags_key else []
        if isinstance(tags, list):
            for tag in tags:
                if not isinstance(tag, dict):
                    continue
                category_key = _key(tag, "category")
                game_tag_key = _key(tag, "localized_tag_name")
                if category_key and game_tag_key and str(tag[category_key]).casefold() == "game":
                    tagged_game = str(tag[game_tag_key]).strip()
                    if tagged_game:
                        actual_game_name = tagged_game
                        has_game_tag = True
                        break
        if is_card and not has_game_tag:
            raise ASFError(
                f"Для карточки {item_name or asset_id} ASF не вернул тег игры; "
                "старый снимок сохранён"
            )
        is_tradable = _optional_flag(description, "tradable", item_name or asset_id)
        if is_card and is_tradable is None:
            raise ASFError(
                f"Для карточки {item_name or asset_id} ASF не подтвердил передаваемость; "
                "старый снимок сохранён"
            )
        def image_url(field: str) -> str:
            field_key = _key(description, field)
            raw = str(description.get(field_key, "") or "").strip() if field_key else ""
            if not raw:
                return ""
            if raw.startswith("//"):
                return "https:" + raw
            if raw.startswith("https://") or raw.startswith("http://"):
                return raw
            if raw.startswith("/economy/image/"):
                return "https://community.cloudflare.steamstatic.com" + raw
            return "https://community.cloudflare.steamstatic.com/economy/image/" + raw.lstrip("/")

        market_key = _key(description, "market_hash_name")
        market_hash_name = str(description.get(market_key, "") or "").strip() if market_key else ""
        item_quality = ""
        if isinstance(tags, list):
            for tag in tags:
                if not isinstance(tag, dict):
                    continue
                category_key = _key(tag, "category")
                internal_key = _key(tag, "internal_name")
                localized_key = _key(tag, "localized_tag_name")
                category = str(tag.get(category_key, "") if category_key else "").casefold()
                internal = str(tag.get(internal_key, "") if internal_key else "").casefold()
                localized = str(tag.get(localized_key, "") if localized_key else "").strip()
                if category in {"exterior", "quality"}:
                    quality_map = {
                        "factory new": "FN", "minimal wear": "MW",
                        "field-tested": "FT", "well-worn": "WW",
                        "battle-scarred": "BS",
                    }
                    item_quality = quality_map.get(localized.casefold(), quality_map.get(internal, ""))
                    if item_quality:
                        break

        cards.append(CardCopy(
            asset_id=asset_id,
            game_name=actual_game_name,
            card_name=item_name or f"Unknown item {asset_id}",
            is_trading_card=is_card,
            is_foil=is_foil,
            is_tradable=is_tradable,
            icon_url=image_url("icon_url"),
            icon_url_large=image_url("icon_url_large"),
            market_hash_name=market_hash_name,
            item_quality=item_quality,
        ))
    return cards
