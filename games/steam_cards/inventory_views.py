from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Literal, Mapping

from games.steam_cards.selection import CardCopy


InventoryMode = Literal["shared", "separate"]


@dataclass(frozen=True)
class OwnedCard:
    """A card with explicit ownership; ownership is never discarded in shared view."""

    bot_name: str
    card: CardCopy


@dataclass(frozen=True)
class InventoryRow:
    """One display row for a game/card identity in an inventory view."""

    game_name: str
    card_name: str
    is_trading_card: bool
    is_foil: bool
    is_tradable: bool | None
    quantity: int
    assets_by_bot: tuple[tuple[str, tuple[str, ...]], ...]
    icon_url: str = ""
    icon_url_large: str = ""
    market_hash_name: str = ""
    item_quality: str = ""


@dataclass(frozen=True)
class InventoryView:
    mode: InventoryMode
    selected_bots: tuple[str, ...]
    rows: tuple[InventoryRow, ...]


def build_inventory_view(
    inventories: Mapping[str, Iterable[CardCopy]],
    selected_bots: Iterable[str],
    *,
    mode: InventoryMode,
) -> InventoryView:
    """Build a shared or per-bot view while preserving exact asset ownership."""
    if mode not in {"shared", "separate"}:
        raise ValueError("mode должен быть 'shared' или 'separate'")

    # Materialize once because an inventory may be a one-shot generator.
    normalized = {name: list(cards) for name, cards in inventories.items()}
    known_bots = {name.strip(): name for name in normalized if name.strip()}
    selected: list[str] = []
    seen: set[str] = set()
    for raw_name in selected_bots:
        name = raw_name.strip()
        if name in known_bots and name not in seen:
            selected.append(name)
            seen.add(name)

    grouped: dict[tuple[str, str, bool, bool, bool | None, str | None, str, str, str, str], dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    seen_assets: set[tuple[str, str]] = set()
    for bot in selected:
        for card in normalized[known_bots[bot]]:
            asset_id = str(card.asset_id).strip()
            game_name = card.game_name.strip()
            card_name = card.card_name.strip()
            if not asset_id or not game_name or not card_name:
                continue
            asset_key = (bot, asset_id)
            if asset_key in seen_assets:
                continue
            seen_assets.add(asset_key)
            owner_key = None if mode == "shared" else bot
            grouped[(
                game_name, card_name, card.is_trading_card, card.is_foil,
                card.is_tradable, owner_key, card.icon_url, card.icon_url_large,
                card.market_hash_name, card.item_quality,
            )][bot].append(asset_id)

    rows: list[InventoryRow] = []
    for (game_name, card_name, is_card, is_foil, is_tradable, owner_key,
         icon_url, icon_url_large, market_hash_name, item_quality), per_bot in sorted(
        grouped.items(),
        key=lambda item: (
            item[0][0].casefold(), item[0][1].casefold(),
            item[0][2], item[0][3], item[0][4] is not True,
            (item[0][5] or "").casefold(), item[0][6], item[0][7],
            item[0][8].casefold(), item[0][9].casefold(),
        ),
    ):
        owners = [(bot, tuple(ids)) for bot, ids in per_bot.items() if ids]
        if owner_key is not None:
            owners = [(owner_key, tuple(per_bot.get(owner_key, [])))]
        rows.append(InventoryRow(
            game_name=game_name,
            card_name=card_name,
            is_trading_card=is_card,
            is_foil=is_foil,
            is_tradable=is_tradable,
            quantity=sum(len(ids) for _, ids in owners),
            assets_by_bot=tuple(owners),
            icon_url=icon_url,
            icon_url_large=icon_url_large,
            market_hash_name=market_hash_name,
            item_quality=item_quality,
        ))
    return InventoryView(mode=mode, selected_bots=tuple(selected), rows=tuple(rows))
