"""Read-only, in-memory projection of one ASF CS2 inventory response."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Any

from integrations.asf.client import ASFError


class RestrictionType(StrEnum):
    NONE = "none"
    MARKET_PURCHASE_COOLDOWN = "market_purchase_cooldown"
    TRADE_PROTECTION = "trade_protection"
    PERMANENTLY_NON_TRADABLE = "permanently_non_tradable"
    UNKNOWN = "unknown"


class RestrictionStatus(StrEnum):
    AVAILABLE = "available"
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CS2InventoryInstance:
    game_id: str
    appid: int | None
    context_id: int | None
    bot_name: str
    asset_id: str
    class_id: str
    instance_id: str
    amount: int | None
    name: str
    item_type: str
    market_hash_name: str
    is_tradable: bool | None
    is_marketable: bool | None
    icon_url: str = ""
    icon_url_large: str = ""
    tags: tuple[str, ...] = ()
    market_tradable_restriction: int | None = None
    market_marketable_restriction: int | None = None
    restriction_type: RestrictionType = RestrictionType.UNKNOWN
    restriction_source: str | None = None
    restriction_status: RestrictionStatus = RestrictionStatus.UNKNOWN
    tradable_after: datetime | None = None
    last_inventory_refresh: datetime | None = None
    quality: str = ""
    exterior: str = ""
    rarity: str = ""

    def __post_init__(self):
        for field in ("game_id", "bot_name", "asset_id", "class_id", "instance_id",
                      "name", "item_type", "market_hash_name", "quality", "exterior", "rarity"):
            if not isinstance(getattr(self, field), str):
                raise TypeError(f"{field} должен быть строкой")
        if not isinstance(self.icon_url, str) or not isinstance(self.icon_url_large, str):
            raise TypeError("URL изображений должны быть строками")
        for field in ("appid", "context_id", "amount", "market_tradable_restriction",
                      "market_marketable_restriction"):
            value = getattr(self, field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise TypeError(f"{field} должен быть целым числом или None")
        if self.amount is not None and self.amount < 0:
            raise ValueError("amount не может быть отрицательным")
        for field in ("is_tradable", "is_marketable"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{field} должен быть bool или None")
        object.__setattr__(self, "tags", tuple(self.tags))
        if any(not isinstance(tag, str) for tag in self.tags):
            raise TypeError("tags должен содержать только строки")
        if not isinstance(self.restriction_type, RestrictionType):
            raise TypeError("restriction_type должен быть RestrictionType")
        if not isinstance(self.restriction_status, RestrictionStatus):
            raise TypeError("restriction_status должен быть RestrictionStatus")
        if self.restriction_source is not None and not isinstance(self.restriction_source, str):
            raise TypeError("restriction_source должен быть строкой или None")
        for field in ("tradable_after", "last_inventory_refresh"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, datetime):
                raise TypeError(f"{field} должен быть datetime или None")
        if self.tradable_after is not None and (
                self.tradable_after.tzinfo is None or self.tradable_after.utcoffset() is None):
            object.__setattr__(self, "tradable_after", None)

    @property
    def physical_key(self) -> tuple[str, str, str]:
        return self.game_id, self.bot_name, self.asset_id

    @property
    def is_reservable(self) -> bool:
        return (
            self.is_tradable is True
            and self.restriction_type is RestrictionType.NONE
            and self.restriction_status is RestrictionStatus.AVAILABLE
            and self.amount is not None
            and self.amount > 0
        )


@dataclass(frozen=True)
class CS2InventoryGroup:
    identity: tuple[Any, ...]
    instances: tuple[CS2InventoryInstance, ...]
    instance_count: int
    known_amount_sum: int
    unknown_amount_instances: int
    total_amount: int | None
    unique_bot_count: int

    def __post_init__(self):
        object.__setattr__(self, "identity", _freeze(self.identity))
        object.__setattr__(self, "instances", tuple(self.instances))
        counters = (self.instance_count, self.known_amount_sum,
                    self.unknown_amount_instances, self.unique_bot_count)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in counters):
            raise TypeError("Счётчики группы должны быть целыми числами")
        if any(value < 0 for value in counters):
            raise ValueError("Счётчики группы не могут быть отрицательными")
        if self.total_amount is not None and (isinstance(self.total_amount, bool)
                                              or not isinstance(self.total_amount, int)):
            raise TypeError("total_amount должен быть int или None")


@dataclass(frozen=True)
class CS2InventoryProjection:
    instances: tuple[CS2InventoryInstance, ...]
    groups: tuple[CS2InventoryGroup, ...]
    known_amount_sum: int
    unknown_amount_instances: int
    total_amount: int | None

    def __post_init__(self):
        object.__setattr__(self, "instances", tuple(self.instances))
        object.__setattr__(self, "groups", tuple(self.groups))
        if any(isinstance(value, bool) or not isinstance(value, int)
               for value in (self.known_amount_sum, self.unknown_amount_instances)):
            raise TypeError("Счётчики проекции должны быть целыми числами")
        if self.total_amount is not None and (isinstance(self.total_amount, bool)
                                              or not isinstance(self.total_amount, int)):
            raise TypeError("total_amount должен быть int или None")


def _key(data: dict[str, Any], name: str) -> str | None:
    return next((k for k in data if str(k).casefold() == name.casefold()), None)


def _freeze(value):
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted(((_freeze(k), _freeze(v)) for k, v in value.items()), key=repr))
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    hash(value)
    return value


def _value(data: dict[str, Any], name: str, default=None):
    k = _key(data, name)
    return data[k] if k is not None else default


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    return None


def _amount_or_none(value: Any) -> int | None:
    amount = _int_or_none(value)
    return amount if amount is not None and amount >= 0 else None


def _identifier(data: dict[str, Any], name: str, default: str = "") -> str:
    key = _key(data, name)
    value = data[key] if key is not None else None
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ASFError(f"ASF вернул некорректный {name}")
    return str(value).strip()


def _description_text(data: dict[str, Any], name: str) -> str:
    value = _value(data, name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ASFError(f"ASF вернул некорректное текстовое поле {name}")
    return value.strip()


def _tag_metadata(description: dict[str, Any]) -> tuple[tuple[str, ...], str, str, str]:
    tags = _value(description, "tags", [])
    if tags is None:
        return (), "", "", ""
    if not isinstance(tags, list):
        raise ASFError("ASF вернул некорректный список tags")
    names = []
    by_category = {}
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        value = _value(tag, "localized_tag_name")
        if value is not None:
            if not isinstance(value, str):
                raise ASFError("ASF вернул некорректное имя tag")
            if value.strip():
                names.append(value.strip())
        internal = _value(tag, "internal_name")
        display = value.strip() if isinstance(value, str) and value.strip() else (
            internal.strip() if isinstance(internal, str) else ""
        )
        category = _value(tag, "category")
        if (display and isinstance(category, str)
                and category.casefold() in {"quality", "exterior", "rarity"}):
            by_category.setdefault(category.casefold(), display)
    return (
        tuple(names), by_category.get("quality", ""),
        by_category.get("exterior", ""), by_category.get("rarity", ""),
    )


def _flag(data: dict[str, Any], name: str) -> bool | None:
    value = _value(data, name)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().casefold() in {"true", "false", "1", "0"}:
        return value.strip().casefold() in {"true", "1"}
    return None


def adapt_cs2_inventory_response(
    payload: Any, bot_name: str, *, appid: int = 730, context_id: int = 2,
    last_inventory_refresh: datetime | None = None,
) -> tuple[CS2InventoryInstance, ...]:
    """Adapt an already-fetched ASF response; never performs IPC or persistence."""
    if (type(appid) is not int or appid != 730
            or type(context_id) is not int or context_id != 2):
        raise ValueError("Адаптер принимает только инвентарь CS2 730/2")
    bot_name = str(bot_name).strip()
    if not bot_name:
        raise ValueError("Имя бота не должно быть пустым")
    if not isinstance(payload, dict):
        raise ASFError("ASF вернул ответ инвентаря неожиданного формата")
    success = _value(payload, "Success")
    if success is False:
        raise ASFError("ASF не смог получить инвентарь CS2")
    if success is not None and not isinstance(success, bool):
        raise ASFError("ASF вернул некорректный Success")
    result = _value(payload, "Result", payload)
    if not isinstance(result, dict):
        raise ASFError("ASF вернул некорректный Result")
    bot_key = _key(result, bot_name)
    if bot_key is None or not isinstance(result[bot_key], dict):
        raise ASFError("ASF не вернул инвентарь запрошенного бота")
    inventory = result[bot_key]
    assets, descriptions = _value(inventory, "Assets"), _value(inventory, "Descriptions")
    if not isinstance(assets, list) or not isinstance(descriptions, list):
        raise ASFError("ASF не подтвердил полный ответ Assets/Descriptions")
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for desc in descriptions:
        if not isinstance(desc, dict):
            continue
        cls = _identifier(desc, "classid")
        if not cls:
            continue
        identity = cls, _identifier(desc, "instanceid", "0") or "0"
        previous = by_identity.get(identity)
        if previous is not None and previous != desc:
            raise ASFError(f"Конфликтующие ASF descriptions: {identity}")
        by_identity[identity] = desc
    output = []
    seen: set[tuple[str, str, str]] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ASFError("ASF вернул некорректный asset")
        aid, cls = _identifier(asset, "assetid"), _identifier(asset, "classid")
        inst = _identifier(asset, "instanceid", "0") or "0"
        if not aid or not cls:
            raise ASFError("ASF asset не содержит assetid/classid")
        physical = ("cs2", bot_name, aid)
        if physical in seen:
            raise ASFError(f"Повторный физический ASF asset: {physical}")
        seen.add(physical)
        # appid/context_id are request provenance when omitted from each asset.
        source_app = _int_or_none(_value(asset, "appid", appid))
        source_context = _int_or_none(_value(asset, "contextid", context_id))
        if source_app is None or source_context is None:
            raise ASFError(f"ASF вернул некорректные appid/contextid у asset {aid}")
        if source_app != 730 or source_context != 2:
            continue
        desc = by_identity.get((cls, inst))
        if desc is None:
            raise ASFError(f"Для asset {aid} не найдено ASF description")
        tags, quality, exterior, rarity = _tag_metadata(desc)
        output.append(CS2InventoryInstance(
            "cs2", source_app, source_context, bot_name, aid, cls, inst,
            _amount_or_none(_value(asset, "amount")), _description_text(desc, "name"),
            _description_text(desc, "type"), _description_text(desc, "market_hash_name"),
            _flag(desc, "tradable"), _flag(desc, "marketable"),
            _description_text(desc, "icon_url"), _description_text(desc, "icon_url_large"),
            tags,
            _int_or_none(_value(desc, "market_tradable_restriction")),
            _int_or_none(_value(desc, "market_marketable_restriction")),
            last_inventory_refresh=last_inventory_refresh,
            quality=quality, exterior=exterior, rarity=rarity,
        ))
    return tuple(output)


def build_cs2_inventory_projection(
    instances: tuple[CS2InventoryInstance, ...] | list[CS2InventoryInstance],
    selected_bots: tuple[str, ...] | list[str] | None = None,
) -> CS2InventoryProjection:
    selected = None if selected_bots is None else {str(b).strip().casefold() for b in selected_bots}
    chosen = [i for i in instances if selected is None or i.bot_name.casefold() in selected]
    unique: dict[tuple[str, str, str], CS2InventoryInstance] = {}
    for item in chosen:
        old = unique.get(item.physical_key)
        if old is not None:
            if old != item:
                raise ValueError(f"Конфликт физического ключа {item.physical_key}")
            raise ValueError(f"Повтор физического ключа {item.physical_key}")
        unique[item.physical_key] = item
    normalized = tuple(sorted(unique.values(), key=lambda i: (
        i.appid if i.appid is not None else -1, i.class_id, i.instance_id,
        i.market_hash_name.casefold(), i.name.casefold(), i.bot_name.casefold(), i.asset_id,
    )))
    grouped: dict[tuple[Any, ...], list[CS2InventoryInstance]] = defaultdict(list)
    for i in normalized:
        key = (
            i.game_id, i.appid, i.context_id, i.class_id, i.instance_id,
            i.name, i.item_type, i.market_hash_name, i.quality, i.exterior,
            i.rarity, tuple(sorted(i.tags, key=str.casefold)),
        )
        grouped[key].append(i)
    groups = []
    for key, members in sorted(grouped.items(), key=lambda pair: str(pair[0]).casefold()):
        known = sum(i.amount for i in members if i.amount is not None and i.amount >= 0)
        unknown = sum(i.amount is None for i in members)
        total = None if unknown else known
        groups.append(CS2InventoryGroup(key, tuple(members), len(members), known, unknown, total, len({i.bot_name for i in members})))
    known = sum(g.known_amount_sum for g in groups)
    unknown = sum(g.unknown_amount_instances for g in groups)
    total = None if unknown or not normalized else known
    return CS2InventoryProjection(normalized, tuple(groups), known, unknown, total)
