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
    """Select normal sets while retaining exact account ownership.

    Foil cards do not count toward sets, but their card names help establish
    the game's card-type list when no normal copy of that type is available.
    """
    if isinstance(sets_needed, bool) or not isinstance(sets_needed, int) or sets_needed < 1:
        raise ValueError("sets_needed должен быть целым числом больше нуля")
    target = game_name.strip()
    if not target:
        raise ValueError("Название игры не должно быть пустым")

    by_name: dict[str, list[OwnedAsset]] = {}
    all_card_names: set[str] = set()
    actual_game = target
    seen: set[tuple[str, str]] = set()
    for bot, cards in inventories.items():
        owner = bot.strip()
        if not owner:
            continue
        for card in cards:
            asset = str(card.asset_id).strip()
            name = card.card_name.strip()
            if not asset or not name or card.game_name.strip().casefold() != target.casefold():
                continue
            if not card.is_trading_card or card.is_tradable is not True:
                continue
            actual_game = card.game_name.strip()
            all_card_names.add(name)
            if card.is_foil or (owner, asset) in seen:
                continue
            seen.add((owner, asset))
            by_name.setdefault(name, []).append(OwnedAsset(owner, asset))

    if not all_card_names:
        raise ValueError("В выбранных инвентарях нет карточек этой игры")
    if any(not by_name.get(name) for name in all_card_names):
        raise ValueError("Недостаточно полных наборов карточек для заказа")

    count = min(len(by_name[name]) for name in all_card_names)
    if count < sets_needed:
        raise ValueError("Недостаточно полных наборов карточек для заказа")

    names = sorted(all_card_names, key=str.casefold)
    result = []
    for index in range(sets_needed):
        assets = tuple(by_name[name][index] for name in names)
        result.append(OwnedSet(actual_game, len(names), assets))
    return result
