from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

from games.steam_cards.selection import CardCopy

AssetKey = tuple[str, str]


@dataclass(frozen=True)
class SharedGameSets:
    """Complete normal-card sets aggregated across several ASF bot inventories."""

    game_name: str
    cards_per_set: int
    available_sets: int
    # Each inner tuple is one complete set of exact (bot_name, asset_id) pairs.
    sets: tuple[tuple[AssetKey, ...], ...]


def find_shared_game_sets(
    inventories: Mapping[str, Iterable[CardCopy]],
) -> list[SharedGameSets]:
    """Count complete sets across bots without losing physical asset ownership.

    One set contains one tradable, non-foil copy of every distinct normal card
    name for a game. Copies may belong to different bots. Asset IDs are scoped
    to their bot because the same opaque ID on two accounts is not the same item.
    """
    by_game: dict[str, dict[str, list[AssetKey]]] = defaultdict(lambda: defaultdict(list))
    seen_assets: set[AssetKey] = set()

    for raw_bot, cards in inventories.items():
        bot_name = str(raw_bot).strip()
        if not bot_name:
            continue
        for card in cards:
            asset_id = str(card.asset_id).strip()
            game_name = card.game_name.strip()
            card_name = card.card_name.strip()
            if not asset_id or not game_name or not card_name:
                continue
            asset_key = (bot_name, asset_id)
            if asset_key in seen_assets:
                continue
            seen_assets.add(asset_key)
            if not card.is_trading_card or card.is_foil or card.is_tradable is not True:
                continue
            by_game[game_name][card_name].append(asset_key)

    results: list[SharedGameSets] = []
    for game_name, cards_by_name in sorted(by_game.items(), key=lambda item: item[0].casefold()):
        ordered_names = sorted(cards_by_name, key=str.casefold)
        if not ordered_names:
            continue
        available = min(len(cards_by_name[name]) for name in ordered_names)
        if available < 1:
            continue
        per_card = {
            name: sorted(cards_by_name[name], key=lambda key: (key[0].casefold(), key[1]))
            for name in ordered_names
        }
        sets = tuple(
            tuple(per_card[name][set_index] for name in ordered_names)
            for set_index in range(available)
        )
        results.append(SharedGameSets(game_name, len(ordered_names), available, sets))
    return results


def select_shared_game_sets(
    inventories: Mapping[str, Iterable[CardCopy]],
    game_name: str,
    sets_needed: int,
) -> tuple[tuple[AssetKey, ...], ...]:
    """Return exact assets for N full sets of one game or fail without partial output."""
    if isinstance(sets_needed, bool) or not isinstance(sets_needed, int) or sets_needed < 1:
        raise ValueError("sets_needed должен быть целым числом больше нуля")
    requested_game = game_name.strip()
    if not requested_game:
        raise ValueError("game_name не должен быть пустым")
    available = next(
        (item for item in find_shared_game_sets(inventories)
         if item.game_name.casefold() == requested_game.casefold()),
        None,
    )
    if available is None or available.available_sets < sets_needed:
        count = available.available_sets if available is not None else 0
        raise ValueError(
            f"Недостаточно полных наборов для игры {requested_game}: "
            f"доступно {count}, требуется {sets_needed}"
        )
    return available.sets[:sets_needed]
