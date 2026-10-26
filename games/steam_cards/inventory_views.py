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
    quantity: int
    # Exact assets grouped by the bot that owns them.
    assets_by_bot: tuple[tuple[str, tuple[str, ...]], ...]


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
    """Build a shared or per-bot inventory view without losing asset ownership.

    No selected bots produces an empty view. Shared mode aggregates matching
    card identities across selected bots for display only; every asset remains
    assigned to its source bot. Separate mode creates distinct rows per bot.
    """
    if mode not in {"shared", "separate"}:
        raise ValueError("mode должен быть 'shared' или 'separate'")

    known_bots = {name.strip(): name for name in inventories if name.strip()}
    selected: list[str] = []
    seen: set[str] = set()
    for raw_name in selected_bots:
        name = raw_name.strip()
        if name in known_bots and name not in seen:
            selected.append(name)
            seen.add(name)

    assets: dict[tuple[str, str, bool, bool, str | None], list[str]] = defaultdict(list)
    seen_asset_ids: set[tuple[str, str]] = set()
    for bot_name in selected:
        for card in inventories[known_bots[bot_name]]:
            asset_id = str(card.asset_id).strip()
            game_name = card.game_name.strip()
            card_name = card.card_name.strip()
            if not asset_id or not game_name or not card_name:
                continue
            # Asset IDs are only unique within a Steam account, not across bots.
            asset_key = (bot_name, asset_id)
            if asset_key in seen_asset_ids:
                continue
            seen_asset_ids.add(asset_key)
            owner_key = None if mode == "shared" else bot_name
            assets[(game_name, card_name, card.is_trading_card, card.is_foil, owner_key)].append(asset_id)

    rows: list[InventoryRow] = []
    for (game_name, card_name, is_card, is_foil, owner_key), ids in sorted(
        assets.items(),
        key=lambda item: (
            item[0][0].casefold(), item[0][1].casefold(),
            item[0][2], item[0][3], (item[0][4] or "").casefold(),
        ),
    ):
        # Shared rows keep a per-bot allocation map so no item can be mistaken
        # as transferable from another bot.
        if owner_key is None:
            per_bot: dict[str, list[str]] = defaultdict(list)
            for bot_name in selected:
                bot_cards = inventories[known_bots[bot_name]]
                for card in bot_cards:
                    if (
                        card.game_name.strip() == game_name
                        and card.card_name.strip() == card_name
                        and card.is_trading_card == is_card
                        and card.is_foil == is_foil
                        and card.asset_id.strip() in ids
                        and card.asset_id.strip()
                    ):
                        per_bot[bot_name].append(card.asset_id.strip())
            assets_by_bot = tuple(
                (bot, tuple(dict.fromkeys(per_bot[bot])))
                for bot in selected if per_bot.get(bot)
            )
        else:
            assets_by_bot = ((owner_key, tuple(dict.fromkeys(ids))),)
        rows.append(
            InventoryRow(
                game_name=game_name,
                card_name=card_name,
                is_trading_card=is_card,
                is_foil=is_foil,
                quantity=sum(len(asset_ids) for _, asset_ids in assets_by_bot),
                assets_by_bot=assets_by_bot,
            )
        )

    return InventoryView(mode=mode, selected_bots=tuple(selected), rows=tuple(rows))
