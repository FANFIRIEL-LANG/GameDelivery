from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from games.steam_cards.selection import CardCopy


@dataclass(frozen=True)
class OwnedAsset:
    bot_name: str
    asset_id: str


@dataclass(frozen=True)
class OwnedSet:
    game_name: str
    cards_per_set: int
    asset_ids: tuple[OwnedAsset, ...]


def select_owned_complete_sets(
    inventories: Mapping[str, Iterable[CardCopy]],
    game_name: str,
    sets_needed: int,
) -> list[OwnedSet]:
    """Select exact normal card sets without ever losing the source bot.

    Each set contains one copy of every distinct normal trading-card name.
    Cards from different bots may contribute to a shared order, but ownership
    remains explicit so downstream code can create separate per-bot offers.
    """
    if isinstance(sets_needed, bool) or not isinstance(sets_needed, int) or sets_needed < 1:
        raise ValueError("sets_needed должен быть целым числом больше нуля")
    target = game_name.strip()
    if not target:
        raise ValueError("Название игры не должно быть пустым")

    by_name: dict[str, list[OwnedAsset]] = {}
    seen: set[tuple[str, str]] = set()
    actual_game = target
    for bot, cards in inventories.items():
        owner = bot.strip()
        if not owner:
            continue
        for card in cards:
            asset = str(card.asset_id).strip()
            name = card.card_name.strip()
            if not asset or not name or card.game_name.strip().casefold() != target.casefold():
                continue
            if not card.is_trading_card or card.is_foil or (owner, asset) in seen:
                continue
            seen.add((owner, asset))
            actual_game = card.game_name.strip()
            by_name.setdefault(name, []).append(OwnedAsset(owner, asset))

    if not by_name:
        raise ValueError("В выбранных инвентарях нет обычных карточек этой игры")
    count = min(len(items) for items in by_name.values())
    if count < sets_needed:
        raise ValueError("Недостаточно полных наборов карточек для заказа")

    sets: list[OwnedSet] = []
    names = sorted(by_name, key=str.casefold)
    for index in range(sets_needed):
        assets = tuple(by_name[name][index] for name in names)
        sets.append(OwnedSet(actual_game, len(names), assets))
    return sets
