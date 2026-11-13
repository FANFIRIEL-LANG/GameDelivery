from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from games.steam_cards.recipient_badge import RecipientBadge


@dataclass(frozen=True)
class RecipientCardInputs:
    recipient_steam_id64: int
    recipient_badges: tuple[RecipientBadge, ...]
    buyer_complete_sets_by_app_id: dict[int, int]
    card_hashes_by_app_id: dict[int, tuple[str, ...]]


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"ASF recipient diagnostic: некорректное поле {name}")
    return value


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"ASF recipient diagnostic: некорректный список {name}")
    normalized = tuple(item.strip() for item in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"ASF recipient diagnostic: список {name} содержит повтор")
    return normalized


def normalize_recipient_card_diagnostics(
    payload: Mapping[str, Any],
    *,
    recipient_steam_id64: int,
    game_names_by_app_id: Mapping[int, str],
) -> RecipientCardInputs:
    """Validate ASF read-only recipient diagnostic and derive complete ordinary sets."""
    if not isinstance(payload, Mapping):
        raise ValueError("ASF не вернул объект read-only диагностики получателя")
    if payload.get("mode") != "diagnostic-read-only":
        raise ValueError("ASF вернул неподдерживаемый режим диагностики")
    if payload.get("sendEnabled") is not False or payload.get("tradeOfferCreated") is not False:
        raise ValueError("ASF не подтвердил read-only режим без отправки предложений")
    identity = payload.get("recipientIdentity")
    if (
        not isinstance(identity, Mapping)
        or identity.get("verified") is not True
        or payload.get("recipientSteamId64") != recipient_steam_id64
    ):
        raise ValueError("SteamID64 получателя не подтверждён ASF")

    app_ids = sorted(int(app_id) for app_id in game_names_by_app_id)
    if not app_ids or len(set(app_ids)) != len(app_ids) or any(app_id <= 0 for app_id in app_ids):
        raise ValueError("Список AppID для проверки пуст или содержит некорректные значения")
    badge_raw = payload.get("badgeChecks")
    comparisons_raw = payload.get("comparisons")
    if not isinstance(badge_raw, list) or not isinstance(comparisons_raw, list):
        raise ValueError("ASF не вернул полные проверки значков и сравнений инвентаря")

    badges: dict[int, Mapping[str, Any]] = {}
    for check in badge_raw:
        if not isinstance(check, Mapping) or check.get("subject") != "recipient":
            continue
        app_id = _integer(check.get("appId"), "badgeChecks.appId", minimum=1)
        if app_id in badges:
            raise ValueError(f"ASF повторил badge result для AppID {app_id}")
        badges[app_id] = check

    comparisons: dict[int, Mapping[str, Any]] = {}
    for comparison in comparisons_raw:
        if not isinstance(comparison, Mapping):
            raise ValueError("ASF вернул некорректную запись сравнения инвентаря")
        app_id = _integer(comparison.get("appId"), "comparisons.appId", minimum=1)
        if app_id in comparisons:
            raise ValueError(f"ASF повторил сравнение для AppID {app_id}")
        comparisons[app_id] = comparison

    inventory = payload.get("inventory")
    if not isinstance(inventory, Mapping):
        raise ValueError("ASF не вернул агрегированный инвентарь получателя")
    for field in ("missingHashAmount", "unknownAppAmount", "invalidAmountCount", "invalidEntryCount"):
        if _integer(inventory.get(field), f"inventory.{field}") != 0:
            raise ValueError(f"Инвентарь получателя неполон: ASF сообщил {field}={inventory.get(field)}")

    raw_cards = inventory.get("cards")
    if not isinstance(raw_cards, list):
        raise ValueError("ASF не вернул список обычных карточек получателя")
    counts_by_app: dict[int, dict[str, int]] = {}
    for card in raw_cards:
        if not isinstance(card, Mapping):
            raise ValueError("ASF вернул некорректную карточку в инвентаре получателя")
        app_id = _integer(card.get("realAppID"), "inventory.cards.realAppID", minimum=1)
        market_hash = card.get("marketHashName")
        if not isinstance(market_hash, str) or not market_hash.strip():
            raise ValueError("ASF вернул карточку без MarketHashName")
        amount = _integer(card.get("amount"), "inventory.cards.amount")
        non_tradable = _integer(card.get("nonTradableAmount"), "inventory.cards.nonTradableAmount")
        if non_tradable > amount:
            raise ValueError("ASF вернул nonTradableAmount больше общего количества")
        group = counts_by_app.setdefault(app_id, {})
        if market_hash in group:
            raise ValueError("ASF вернул повторяющуюся группу карточки в инвентаре")
        group[market_hash] = amount

    if set(badges) != set(app_ids) or set(comparisons) != set(app_ids):
        raise ValueError("ASF не вернул результаты для каждого AppID из инвентаря продавца")

    recipient_badges: list[RecipientBadge] = []
    complete_sets: dict[int, int] = {}
    hashes_by_app: dict[int, tuple[str, ...]] = {}
    for app_id in app_ids:
        game_name = str(game_names_by_app_id[app_id]).strip()
        if not game_name:
            raise ValueError(f"Не определено название игры для AppID {app_id}")
        badge = badges[app_id]
        if (
            badge.get("success") is not True
            or badge.get("httpStatusCode") != 200
            or badge.get("responseIdentityVerified") is not True
            or badge.get("appId") != app_id
        ):
            raise ValueError(f"Значок получателя для AppID {app_id} не подтверждён ASF")
        level = _integer(badge.get("level"), "badgeChecks.level")
        max_level = _integer(badge.get("maxLevel"), "badgeChecks.maxLevel", minimum=1)
        border = _integer(badge.get("border"), "badgeChecks.border")
        card_count = _integer(badge.get("cardCount"), "badgeChecks.cardCount", minimum=1)
        unique_count = _integer(badge.get("uniqueHashCount"), "badgeChecks.uniqueHashCount", minimum=1)
        if level > 5 or max_level != 5 or border != 0:
            raise ValueError(f"Для AppID {app_id} нужен подтверждённый обычный значок уровня 0–5")
        if card_count != unique_count or badge.get("duplicateHashes") != []:
            raise ValueError(f"Состав обычного набора для AppID {app_id} содержит повторы")
        comparison = comparisons[app_id]
        if comparison.get("success") is not True or comparison.get("duplicateBadgeHashes") != []:
            raise ValueError(f"Сравнение значка и инвентаря для AppID {app_id} не прошло проверку")
        matched = _string_list(comparison.get("matchedHashes"), "matchedHashes")
        missing = _string_list(comparison.get("missingHashes"), "missingHashes")
        if set(matched).intersection(missing):
            raise ValueError(f"ASF вернул пересекающиеся списки карточек для AppID {app_id}")
        hashes = tuple(sorted(set(matched).union(missing)))
        if len(hashes) != card_count:
            raise ValueError(f"ASF вернул неполный состав набора для AppID {app_id}")
        recipient_badges.append(RecipientBadge(game_name, level, is_foil=False))
        hashes_by_app[app_id] = hashes
        inventory_counts = counts_by_app.get(app_id, {})
        complete_sets[app_id] = min(
            (inventory_counts.get(card_hash, 0) for card_hash in hashes),
            default=0,
        )

    return RecipientCardInputs(
        recipient_steam_id64=recipient_steam_id64,
        recipient_badges=tuple(recipient_badges),
        buyer_complete_sets_by_app_id=complete_sets,
        card_hashes_by_app_id=hashes_by_app,
    )
