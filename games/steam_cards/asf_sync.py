from __future__ import annotations

from games.steam_cards.asf_inventory import parse_bot_inventory
from games.steam_cards.snapshot import InventorySnapshotStore
from integrations.asf.client import ASFClient


def refresh_bot_snapshot(
    client: ASFClient,
    store: InventorySnapshotStore,
    bot_name: str,
) -> dict[str, object]:
    """Fetch ASF's full Steam Community inventory and persist it only on success.

    Steam trading cards live in app 753 / context 6. Game names are read from
    ASF inventory description tags when present. No trade or account setting is changed.
    """
    payload = client.get_bot_inventory(bot_name, app_id=753, context_id=6, language="english")
    cards = parse_bot_inventory(payload, bot_name, "Unknown game")
    return store.replace(cards, bot_name=bot_name)
