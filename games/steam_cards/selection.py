from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CardCopy:
    """One physical Steam trading-card asset normalized from an inventory source."""

    asset_id: str
    game_name: str
    card_name: str
    is_trading_card: bool = True
    is_foil: bool = False


@dataclass(frozen=True)
class AvailableSet:
    """A complete set available from one game, with the exact asset IDs reserved."""

    game_name: str
    cards_per_set: int
    available_sets: int
    asset_ids: tuple[str, ...]


def find_available_sets(cards: Iterable[CardCopy]) -> list[AvailableSet]:
    """Calculate complete normal (non-foil) sets from a snapshot of inventory.

    A set means one copy of each distinct normal trading-card name for a game.
    This is a pure calculation: it does not reserve items, contact Steam, or send trades.
    """
    by_game: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    seen_asset_ids: set[str] = set()

    for card in cards:
        asset_id = str(card.asset_id).strip()
        game_name = card.game_name.strip()
        card_name = card.card_name.strip()
        if not asset_id or not game_name or not card_name:
            continue
        if asset_id in seen_asset_ids:
            continue
        seen_asset_ids.add(asset_id)
        if not card.is_trading_card or card.is_foil:
            continue
        by_game[game_name][card_name].append(asset_id)

    results: list[AvailableSet] = []
    for game_name, cards_by_name in sorted(by_game.items(), key=lambda item: item[0].casefold()):
        if not cards_by_name:
            continue
        count = min(len(asset_ids) for asset_ids in cards_by_name.values())
        if count < 1:
            continue
        # Deterministic selection: take the same number of copies for every card type.
        selected: list[str] = []
        for card_name in sorted(cards_by_name, key=str.casefold):
            selected.extend(cards_by_name[card_name][:count])
        results.append(
            AvailableSet(
                game_name=game_name,
                cards_per_set=len(cards_by_name),
                available_sets=count,
                asset_ids=tuple(selected),
            )
        )
    return results


def select_complete_sets(cards: Iterable[CardCopy], sets_needed: int) -> list[AvailableSet]:
    """Select enough full sets from a snapshot, or raise ValueError without partial output."""
    if isinstance(sets_needed, bool) or not isinstance(sets_needed, int) or sets_needed < 1:
        raise ValueError("sets_needed должен быть целым числом больше нуля")

    available = find_available_sets(cards)
    selected: list[AvailableSet] = []
    remaining = sets_needed
    # Match the donor prototype's preference for games with fewer distinct card types.
    for game in sorted(available, key=lambda item: (item.cards_per_set, item.game_name.casefold())):
        take = min(game.available_sets, remaining)
        if take <= 0:
            continue
        ids_per_card = game.cards_per_set
        selected.append(
            AvailableSet(
                game_name=game.game_name,
                cards_per_set=ids_per_card,
                available_sets=take,
                asset_ids=game.asset_ids[:take * ids_per_card],
            )
        )
        remaining -= take
        if remaining == 0:
            return selected

    raise ValueError("Недостаточно полных наборов карточек для заказа")
