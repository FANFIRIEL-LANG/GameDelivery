from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

_SELECTOR_FIELDS = frozenset({
    "market_hash_name", "name", "item_type", "class_id",
    "quality", "exterior", "rarity",
})


def _clean(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} должен быть строкой")
    result = value.strip()
    if not result:
        raise ValueError(f"{field} не должен быть пустым")
    return result


@dataclass(frozen=True)
class StockItemIdentity:
    """Verified item attributes; physical amount and tradability are separate."""

    game_id: str
    app_id: int
    context_id: str
    market_hash_name: str = ""
    name: str = ""
    item_type: str = ""
    class_id: str = ""
    quality: str = ""
    exterior: str = ""
    rarity: str = ""

    def normalized(self) -> "StockItemIdentity":
        game_id = _clean(self.game_id, "game_id")
        context_id = _clean(self.context_id, "context_id")
        if isinstance(self.app_id, bool) or not isinstance(self.app_id, int) or self.app_id < 1:
            raise ValueError("app_id должен быть положительным целым числом")
        values = {}
        for field in _SELECTOR_FIELDS:
            value = getattr(self, field)
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise ValueError(f"{field} должен быть строкой")
            values[field] = value.strip()
        name = values["name"]
        if name.casefold().startswith(("unknown item ", "unknown asset ")):
            values["name"] = ""
        return StockItemIdentity(game_id=game_id, app_id=self.app_id, context_id=context_id, **values)

    def matches_spec(self, spec: object) -> bool:
        """Fail-closed exact match for SteamAssetSpec-like objects."""
        try:
            identity = self.normalized()
            game_id = _clean(getattr(spec, "game_id"), "spec.game_id")
            app_id = getattr(spec, "app_id")
            context_id = _clean(getattr(spec, "context_id"), "spec.context_id")
            selector = getattr(spec, "selector")
            alternatives = getattr(spec, "selector_options", ())
        except (AttributeError, TypeError, ValueError):
            return False
        if (isinstance(app_id, bool) or not isinstance(app_id, int)
            or (identity.game_id, identity.app_id, identity.context_id) != (game_id, app_id, context_id)
            or not isinstance(selector, Mapping) or not selector
            or not isinstance(alternatives, (tuple, list))):
            return False
        for option in (selector, *alternatives):
            if (not isinstance(option, Mapping) or not option
                or any(key not in _SELECTOR_FIELDS for key in option)):
                continue
            matched = True
            for field, expected in option.items():
                if not isinstance(expected, str) or not expected.strip():
                    matched = False
                    break
                actual = getattr(identity, field)
                if not actual or actual != expected.strip():
                    matched = False
                    break
            if matched:
                return True
        return False

    def allows_bot(self, bot_name: str, spec: object) -> bool:
        """Bot-pool policy is deliberately separate from item identity."""
        try:
            raw_pool = getattr(spec, "allowed_bot_pool")
            if isinstance(raw_pool, (str, bytes)):
                return False
            pool = tuple(raw_pool)
        except (AttributeError, TypeError):
            return False
        if not pool:
            return True
        if not isinstance(bot_name, str) or not bot_name.strip():
            return False
        return bot_name.strip().casefold() in {
            value.strip().casefold() for value in pool
            if isinstance(value, str) and value.strip()
        }
