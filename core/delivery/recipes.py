"""Strict, versioned Playerok delivery recipe contracts.

These models describe order obligations. They deliberately do not reserve stock
or execute delivery; those remain responsibilities of the canonical ledger and
delivery pipeline.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias


class RecipeValidationError(ValueError):
    """A recipe is malformed or uses a contract this version does not know."""


class UnsupportedLegacyRecipeError(RecipeValidationError):
    """An old selector is ambiguous and cannot safely become a RecipeItem."""


_ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_STEAM_SELECTOR_FIELDS = {
    "market_hash_name", "name", "item_type", "class_id", "quality", "exterior", "rarity"
}


def _strict_keys(value: Mapping[str, Any], allowed: set[str], required: set[str], label: str) -> None:
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise RecipeValidationError(f"{label}: неизвестные поля: {', '.join(sorted(map(str, unknown)))}")
    if missing:
        raise RecipeValidationError(f"{label}: отсутствуют обязательные поля: {', '.join(sorted(missing))}")


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RecipeValidationError(f"{label} должен быть положительным целым числом")
    return value


def _text(value: Any, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RecipeValidationError(f"{label} должен быть непустой строкой")
    return value.strip()


def _normalize_bot_pool(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise RecipeValidationError("allowed_bot_pool должен быть списком имён ботов")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_bot in value:
        if not isinstance(raw_bot, str) or not raw_bot.strip():
            raise RecipeValidationError("allowed_bot_pool содержит пустое или некорректное имя")
        bot = raw_bot.strip()
        key = bot.casefold()
        if key in seen:
            raise RecipeValidationError("allowed_bot_pool содержит дубликаты")
        seen.add(key)
        normalized.append(bot)
    return tuple(normalized)


@dataclass(frozen=True)
class SteamCardSetSpec:
    app_id: int = 753
    context_id: str = "6"
    selection: str = "buyer_badges_and_complete_sets"
    game_name: str | None = None
    allowed_bot_pool: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.app_id) is not int or self.app_id != 753:
            raise RecipeValidationError("Steam Card Set требует app_id=753")
        if str(self.context_id) != "6":
            raise RecipeValidationError("Steam Card Set требует context_id=6")
        object.__setattr__(self, "context_id", "6")
        if self.selection != "buyer_badges_and_complete_sets":
            raise RecipeValidationError("Неизвестная стратегия выбора Steam Cards")
        if self.game_name is not None and (not isinstance(self.game_name, str) or not self.game_name.strip()):
            raise RecipeValidationError("game_name должен быть непустой строкой или null")
        object.__setattr__(self, "allowed_bot_pool", _normalize_bot_pool(self.allowed_bot_pool))

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id, "context_id": str(self.context_id),
            "selection": self.selection, "game_name": self.game_name,
            "allowed_bot_pool": list(self.allowed_bot_pool),
        }


@dataclass(frozen=True)
class SteamAssetSpec:
    game_id: str
    app_id: int
    context_id: str
    selector: Mapping[str, str]
    allowed_bot_pool: tuple[str, ...] = ()
    selector_options: tuple[Mapping[str, str], ...] = ()

    def __post_init__(self) -> None:
        _text(self.game_id, "game_id")
        _positive_int(self.app_id, "app_id")
        if not isinstance(self.context_id, str):
            raise RecipeValidationError("context_id должен быть строкой")
        _text(self.context_id, "context_id")
        if not isinstance(self.selector, Mapping) or not self.selector:
            raise RecipeValidationError("selector должен быть непустым объектом")
        _strict_keys(self.selector, _STEAM_SELECTOR_FIELDS, set(), "selector")
        for key, value in self.selector.items():
            _text(value, f"selector.{key}")
        if not isinstance(self.selector_options, (tuple, list)):
            raise RecipeValidationError("selector_options должен быть списком альтернатив")
        normalized_options = []
        for index, option in enumerate(self.selector_options):
            if not isinstance(option, Mapping) or not option:
                raise RecipeValidationError("Каждый selector option должен быть непустым объектом")
            _strict_keys(option, _STEAM_SELECTOR_FIELDS, set(), f"selector_options[{index}]")
            for key, value in option.items():
                _text(value, f"selector_options[{index}].{key}")
            normalized_options.append(MappingProxyType(dict(option)))
        object.__setattr__(self, "allowed_bot_pool", _normalize_bot_pool(self.allowed_bot_pool))
        object.__setattr__(self, "selector", MappingProxyType(dict(self.selector)))
        object.__setattr__(self, "selector_options", tuple(normalized_options))

    @property
    def selectors(self) -> tuple[Mapping[str, str], ...]:
        return (self.selector, *self.selector_options)

    def to_dict(self) -> dict[str, Any]:
        value = {
            "game_id": self.game_id.strip(), "app_id": self.app_id,
            "context_id": str(self.context_id), "selector": dict(self.selector),
            "allowed_bot_pool": list(self.allowed_bot_pool),
        }
        if self.selector_options:
            value["selector_options"] = [dict(option) for option in self.selector_options]
        return value


@dataclass(frozen=True)
class DigitalTextUnitSpec:
    allocation: str = "one_unique_unit"

    def __post_init__(self) -> None:
        if self.allocation != "one_unique_unit":
            raise RecipeValidationError("Неизвестная стратегия выделения текстовой единицы")

    def to_dict(self) -> dict[str, Any]:
        return {"allocation": self.allocation}


RecipeSpec: TypeAlias = SteamCardSetSpec | SteamAssetSpec | DigitalTextUnitSpec


@dataclass(frozen=True)
class RecipeItem:
    item_id: str
    position: int
    kind: str
    spec_version: int
    quantity_per_order: int
    required: bool
    stock_source_id: str | None
    executor_key: str
    spec: RecipeSpec

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not _ITEM_ID.fullmatch(self.item_id):
            raise RecipeValidationError("item_id имеет недопустимый формат")
        _positive_int(self.position, "position")
        _positive_int(self.spec_version, "spec_version")
        if self.spec_version != 1:
            raise RecipeValidationError("Неизвестная версия RecipeItem spec")
        _positive_int(self.quantity_per_order, "quantity_per_order")
        if type(self.required) is not bool:
            raise RecipeValidationError("required должен быть bool")
        if not isinstance(self.kind, str):
            raise RecipeValidationError("kind должен быть строкой")
        if not isinstance(self.executor_key, str):
            raise RecipeValidationError("executor_key должен быть строкой")
        if self.stock_source_id is not None:
            _text(self.stock_source_id, "stock_source_id")
        expected: tuple[str, type, str] | None = {
            "steam_card_set": ("steam_trade_offer", SteamCardSetSpec, "Steam Card Set"),
            "steam_asset": ("steam_trade_offer", SteamAssetSpec, "Steam asset"),
            "digital_text_unit": ("text_delivery", DigitalTextUnitSpec, "Digital text unit"),
        }.get(self.kind)
        if expected is None:
            raise RecipeValidationError(f"Неизвестный тип позиции: {self.kind!r}")
        executor, spec_type, label = expected
        if not isinstance(self.spec, spec_type):
            raise RecipeValidationError(f"{self.kind} требует спецификацию {spec_type.__name__}")
        if self.executor_key != executor:
            raise RecipeValidationError(f"{self.kind} требует executor_key={executor!r}")
        if self.kind == "digital_text_unit" and self.stock_source_id is None:
            raise RecipeValidationError("digital_text_unit требует stock_source_id")
        if self.kind in {"steam_card_set", "steam_asset"} \
                and self.stock_source_id is None and not self.spec.allowed_bot_pool:
            raise RecipeValidationError("Steam позиция требует stock_source_id или allowed_bot_pool")

    @property
    def display_name(self) -> str:
        if isinstance(self.spec, SteamAssetSpec):
            return str(self.spec.selector.get("market_hash_name") or self.spec.selector.get("name") or "Steam asset")
        if isinstance(self.spec, SteamCardSetSpec):
            return self.spec.game_name or "Steam Card set"
        return "Digital text unit"

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id, "position": self.position, "kind": self.kind,
            "spec_version": self.spec_version, "quantity_per_order": self.quantity_per_order,
            "required": self.required, "stock_source_id": self.stock_source_id,
            "executor_key": self.executor_key, "spec": self.spec.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "RecipeItem":
        if not isinstance(value, Mapping):
            raise RecipeValidationError("RecipeItem должен быть объектом")
        _strict_keys(
            value,
            {"item_id", "position", "kind", "spec_version", "quantity_per_order", "required",
             "stock_source_id", "executor_key", "spec"},
            {"item_id", "position", "kind", "spec_version", "quantity_per_order", "required",
             "stock_source_id", "executor_key", "spec"},
            "RecipeItem",
        )
        kind = value["kind"]
        raw_spec = value["spec"]
        if not isinstance(raw_spec, Mapping):
            raise RecipeValidationError("spec должен быть объектом")
        if kind == "steam_card_set":
            allowed = {"app_id", "context_id", "selection", "game_name", "allowed_bot_pool"}
            _strict_keys(raw_spec, allowed, {"app_id", "context_id", "selection"}, "steam_card_set.spec")
            spec = SteamCardSetSpec(
                app_id=raw_spec.get("app_id", 753), context_id=str(raw_spec.get("context_id", "6")),
                selection=raw_spec.get("selection", "buyer_badges_and_complete_sets"),
                game_name=raw_spec.get("game_name"),
                allowed_bot_pool=_parse_bot_pool(raw_spec.get("allowed_bot_pool", [])),
            )
        elif kind == "steam_asset":
            allowed = {"game_id", "app_id", "context_id", "selector", "allowed_bot_pool", "selector_options"}
            _strict_keys(raw_spec, allowed, {"game_id", "app_id", "context_id", "selector"}, "steam_asset.spec")
            selector = raw_spec["selector"]
            options = raw_spec.get("selector_options", [])
            if not isinstance(selector, Mapping) or not isinstance(options, list):
                raise RecipeValidationError("steam_asset.spec selector/selector_options имеют неверный формат")
            spec = SteamAssetSpec(
                game_id=raw_spec["game_id"], app_id=raw_spec["app_id"],
                context_id=raw_spec["context_id"], selector=selector,
                allowed_bot_pool=_parse_bot_pool(raw_spec.get("allowed_bot_pool", [])),
                selector_options=tuple(options),
            )
        elif kind == "digital_text_unit":
            _strict_keys(raw_spec, {"allocation"}, {"allocation"}, "digital_text_unit.spec")
            spec = DigitalTextUnitSpec(raw_spec["allocation"])
        else:
            raise RecipeValidationError(f"Неизвестный тип позиции: {kind!r}")
        return cls(
            item_id=value["item_id"], position=value["position"], kind=kind,
            spec_version=value["spec_version"], quantity_per_order=value["quantity_per_order"],
            required=value["required"], stock_source_id=value["stock_source_id"],
            executor_key=value["executor_key"], spec=spec,
        )


@dataclass(frozen=True)
class Recipe:
    items: tuple[RecipeItem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or not self.items:
            raise RecipeValidationError("Recipe должен содержать хотя бы одну позицию")
        if any(not isinstance(item, RecipeItem) for item in self.items):
            raise RecipeValidationError("Recipe.items должен содержать только RecipeItem")
        validated_items = tuple(RecipeItem.from_dict(item.to_dict()) for item in self.items)
        ids = [item.item_id for item in validated_items]
        positions = [item.position for item in validated_items]
        if len(set(ids)) != len(ids):
            raise RecipeValidationError("item_id позиции повторяется")
        if len(set(positions)) != len(positions):
            raise RecipeValidationError("position позиции повторяется")
        object.__setattr__(self, "items", tuple(sorted(validated_items, key=lambda item: item.position)))

    @classmethod
    def from_values(cls, values: Any) -> "Recipe":
        if isinstance(values, Recipe):
            # Re-parse the JSON-shaped representation. This validates objects
            # even when they were altered with object.__setattr__ or a nested
            # mutable value escaped a caller's frozen dataclass.
            values = values.to_list()
        if not isinstance(values, (list, tuple)):
            raise RecipeValidationError("Recipe должен быть списком позиций")
        return cls(tuple(item if isinstance(item, RecipeItem) else RecipeItem.from_dict(item) for item in values))

    def to_list(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.items]


def _parse_bot_pool(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RecipeValidationError("allowed_bot_pool должен быть списком непустых строк")
    return _normalize_bot_pool(value)


def normalize_legacy_cs2_selector_options(value: Any) -> list[dict[str, Any]] | None:
    """Expand the historical CS2 ``items`` wrapper for the legacy executor.

    New configurations store a list of complete selector options. The old
    wrapper remains readable, with its entries interpreted as alternatives.
    """
    if not isinstance(value, Mapping) or value.get("delivery_mode") != "cs2_items":
        return None
    _strict_keys(
        value, {"delivery_mode", "app_id", "context_id", "items"},
        {"delivery_mode", "app_id", "context_id", "items"}, "legacy cs2_items selector",
    )
    if type(value["app_id"]) is not int or value["app_id"] != 730:
        raise RecipeValidationError("CS2 selector требует app_id=730")
    if isinstance(value["context_id"], bool) or str(value["context_id"]) != "2":
        raise RecipeValidationError("CS2 selector требует context_id=2")
    raw_options = value["items"]
    if isinstance(raw_options, Mapping):
        raw_options = [raw_options]
    if not isinstance(raw_options, list) or not raw_options:
        raise RecipeValidationError("CS2 items должен содержать хотя бы один selector")
    options: list[dict[str, Any]] = []
    allowed = _STEAM_SELECTOR_FIELDS | {"game_id", "app_id", "context_id"}
    for raw_option in raw_options:
        if not isinstance(raw_option, Mapping):
            raise RecipeValidationError("CS2 selector option должен быть объектом")
        _strict_keys(raw_option, allowed, set(), "legacy CS2 item selector")
        if "game_id" in raw_option and raw_option["game_id"] != "cs2":
            raise RecipeValidationError("CS2 selector требует game_id='cs2'")
        if "app_id" in raw_option and raw_option["app_id"] != 730:
            raise RecipeValidationError("CS2 selector требует app_id=730")
        if "context_id" in raw_option and (
            isinstance(raw_option["context_id"], bool)
            or str(raw_option["context_id"]) != "2"
        ):
            raise RecipeValidationError("CS2 selector требует context_id=2")
        selector = {key: item for key, item in raw_option.items()
                    if key in _STEAM_SELECTOR_FIELDS}
        if not selector:
            raise RecipeValidationError("CS2 selector option пуст")
        for key, item in selector.items():
            _text(item, f"selector.{key}")
        options.append({"game_id": "cs2", "app_id": 730, "context_id": "2", **selector})
    return options


def legacy_config_to_recipe(config: Mapping[str, Any]) -> Recipe:
    """Convert a supported legacy selector to one typed RecipeItem.

    Multiple CS2 selectors are ordered alternatives of one obligation, never
    independent required RecipeItems.
    """
    if not isinstance(config, Mapping):
        raise UnsupportedLegacyRecipeError("Legacy ConfigVersion должен быть объектом")
    raw = config.get("item_selectors", config.get("item_selectors_json"))
    try:
        selectors = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        raise UnsupportedLegacyRecipeError("Legacy selector содержит некорректный JSON") from exc
    if not isinstance(selectors, Mapping):
        raise UnsupportedLegacyRecipeError("Legacy selector не является одной однозначной спецификацией")
    quantity = config.get("delivery_quantity_per_deal")
    _positive_int(quantity, "delivery_quantity_per_deal")
    raw_pool = config.get("bot_pool", config.get("bot_pool_json", []))
    try:
        bots = json.loads(raw_pool) if isinstance(raw_pool, str) else raw_pool
    except (TypeError, ValueError) as exc:
        raise UnsupportedLegacyRecipeError("Legacy bot pool содержит некорректный JSON") from exc
    if not isinstance(bots, list) or any(not isinstance(bot, str) or not bot.strip() for bot in bots):
        raise UnsupportedLegacyRecipeError("Legacy bot pool имеет неизвестный формат")
    allowed_bots_list: list[str] = []
    seen_bots: set[str] = set()
    for bot in bots:
        normalized = bot.strip().casefold()
        if normalized not in seen_bots:
            seen_bots.add(normalized)
            allowed_bots_list.append(bot.strip())
    allowed_bots = tuple(allowed_bots_list)
    mode = selectors.get("delivery_mode")
    item: RecipeItem
    if mode == "steam_cards" and type(selectors.get("app_id")) is int \
            and selectors.get("app_id") == 753 \
            and not isinstance(selectors.get("context_id"), bool) \
            and str(selectors.get("context_id")) == "6":
        _strict_keys(
            selectors, {"delivery_mode", "app_id", "context_id", "selection", "game_name"},
            {"delivery_mode", "app_id", "context_id", "selection"}, "legacy steam_cards selector",
        )
        if not allowed_bots:
            raise UnsupportedLegacyRecipeError("Legacy Steam Cards config не содержит разрешённого bot pool")
        spec = SteamCardSetSpec(
            selection=selectors["selection"],
            game_name=selectors.get("game_name"), allowed_bot_pool=allowed_bots,
        )
        item = RecipeItem("legacy-position-1", 1, "steam_card_set", 1, quantity, True,
                          None, "steam_trade_offer", spec)
    elif mode == "cs2_items" and type(selectors.get("app_id")) is int \
            and selectors.get("app_id") == 730 \
            and not isinstance(selectors.get("context_id"), bool) \
            and str(selectors.get("context_id")) == "2":
        _strict_keys(selectors, {"delivery_mode", "app_id", "context_id", "items"},
                     {"delivery_mode", "app_id", "context_id", "items"}, "legacy cs2_items selector")
        normalized_options = normalize_legacy_cs2_selector_options(selectors)
        if not normalized_options:
            raise UnsupportedLegacyRecipeError("CS2 selector options отсутствуют или некорректны")
        if not allowed_bots:
            raise UnsupportedLegacyRecipeError("Legacy CS2 config не содержит разрешённого bot pool")
        selector_options = [
            {key: value for key, value in option.items() if key in _STEAM_SELECTOR_FIELDS}
            for option in normalized_options
        ]
        spec = SteamAssetSpec(
            "cs2", 730, "2", selector_options[0], allowed_bots,
            selector_options=tuple(selector_options[1:]),
        )
        item = RecipeItem("legacy-position-1", 1, "steam_asset", 1, quantity, True,
                          None, "steam_trade_offer", spec)
    elif _is_legacy_card_selector(selectors):
        _strict_keys(
            selectors,
            {"item_type", "delivery_type", "app_id", "context_id", "game_name", "name"},
            set(), "legacy card selector",
        )
        game_name = selectors.get("game_name") or selectors.get("name")
        if not allowed_bots:
            raise UnsupportedLegacyRecipeError("Legacy Steam Cards config не содержит разрешённого bot pool")
        spec = SteamCardSetSpec(game_name=game_name, allowed_bot_pool=allowed_bots)
        item = RecipeItem("legacy-position-1", 1, "steam_card_set", 1, quantity, True,
                          None, "steam_trade_offer", spec)
    elif _is_legacy_steam_asset_selector(selectors):
        _strict_keys(
            selectors,
            _STEAM_SELECTOR_FIELDS | {"game_id", "app_id", "context_id"},
            {"game_id", "app_id", "context_id"}, "legacy steam asset selector",
        )
        spec_selector = {key: selectors[key] for key in _STEAM_SELECTOR_FIELDS if key in selectors}
        if not allowed_bots:
            raise UnsupportedLegacyRecipeError("Legacy Steam asset config не содержит разрешённого bot pool")
        if not spec_selector:
            raise UnsupportedLegacyRecipeError("Legacy Steam asset selector пуст")
        spec = SteamAssetSpec(
            selectors["game_id"], selectors["app_id"], str(selectors["context_id"]),
            spec_selector, allowed_bots,
        )
        item = RecipeItem("legacy-position-1", 1, "steam_asset", 1, quantity, True,
                          None, "steam_trade_offer", spec)
    else:
        raise UnsupportedLegacyRecipeError("Legacy ConfigVersion не распознана как однозначный recipe item")
    return Recipe((item,))


def legacy_materialized_order_line(connection, order_id: str, config_version_id: str) -> dict[str, Any] | None:
    """Return the single immutable legacy-compatibility line, if one exists.

    This lets the existing single-item reservation paths reuse the line created
    with a newly synced order. Typed multi-item recipes remain outside those
    legacy executors.
    """
    version = connection.execute(
        "SELECT recipe_schema_version,recipe_items_json,delivery_quantity_per_deal,"
        "item_selectors_json,bot_pool_json FROM gd_config_versions WHERE config_version_id=?",
        (config_version_id,),
    ).fetchone()
    if version is None:
        raise KeyError(f"ConfigVersion {config_version_id} не найден")
    rows = connection.execute(
        "SELECT * FROM gd_order_lines WHERE order_id=? ORDER BY line_number", (order_id,)
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("Legacy executor поддерживает только одну заранее созданную OrderLine")
    try:
        if version["recipe_schema_version"] is None:
            expected_recipe = legacy_config_to_recipe(dict(version))
        else:
            if version["recipe_schema_version"] != 1:
                return None
            typed_items = json.loads(version["recipe_items_json"])
            if not isinstance(typed_items, list) or len(typed_items) != 1:
                return None
            expected_recipe = Recipe.from_values(typed_items)
            legacy_recipe = legacy_config_to_recipe(dict(version))
            typed_item = expected_recipe.items[0].to_dict()
            legacy_item = legacy_recipe.items[0].to_dict()
            typed_item.pop("item_id")
            legacy_item.pop("item_id")
            if typed_item != legacy_item:
                return None
    except (KeyError, TypeError, ValueError):
        return None
    line = dict(rows[0])
    try:
        snapshot = json.loads(line["item_selector_json"])
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "config_version_id", "recipe_item_id", "recipe_item"
        }:
            return None
        item = RecipeItem.from_dict(snapshot["recipe_item"])
    except (KeyError, TypeError, ValueError):
        return None
    expected_item = expected_recipe.items[0]
    if (
        snapshot["config_version_id"] != config_version_id
        or snapshot["recipe_item_id"] != item.item_id
        or item.to_dict() != expected_item.to_dict()
        or line["line_number"] != item.position
        or line["item_type"] != item.kind
        or line["item_name"] != item.display_name
        or line["quantity"] != item.quantity_per_order
    ):
        return None
    return line


def _is_legacy_card_selector(selectors: Mapping[str, Any]) -> bool:
    card_kinds = {"steam_card_set", "trading_card_set", "card_set"}
    kinds = [str(selectors[key]).casefold() for key in ("item_type", "delivery_type")
             if selectors.get(key) is not None]
    return bool(kinds) and all(kind in card_kinds for kind in kinds) \
        and type(selectors.get("app_id", 753)) is int \
        and selectors.get("app_id", 753) == 753 \
        and not isinstance(selectors.get("context_id", "6"), bool) \
        and str(selectors.get("context_id", "6")) == "6"


def _is_legacy_steam_asset_selector(selectors: Mapping[str, Any]) -> bool:
    return (
        type(selectors.get("app_id")) is int and selectors.get("app_id") == 730
        and not isinstance(selectors.get("context_id"), bool)
        and str(selectors.get("context_id")) == "2"
        and str(selectors.get("game_id") or "").casefold() == "cs2"
    )
