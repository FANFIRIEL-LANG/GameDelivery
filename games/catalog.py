from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GameDefinition:
    game_id: str
    name: str
    app_id: int
    context_id: int
    module_id: str
    item_domain: str


# Steam trading cards are a separate Steam Community inventory (753/6).
# Game inventories use their own Steam app IDs and context 2.
GAME_CATALOG: tuple[GameDefinition, ...] = (
    GameDefinition("cs2", "Counter-Strike 2", 730, 2, "cs2", "weapon_skins"),
    GameDefinition("dota2", "Dota 2", 570, 2, "dota2", "game_items"),
    GameDefinition("steam_cards", "Steam", 753, 6, "steam_cards", "trading_cards"),
    GameDefinition("tf2", "Team Fortress 2", 440, 2, "tf2", "game_items"),
    GameDefinition("rust", "Rust", 252490, 2, "rust", "game_items"),
)

_GAME_BY_ID = {game.game_id: game for game in GAME_CATALOG}


def get_game(game_id: str) -> GameDefinition:
    key = str(game_id).strip().casefold()
    try:
        return _GAME_BY_ID[key]
    except KeyError:
        raise ValueError(f"Неизвестный игровой модуль: {game_id}") from None
