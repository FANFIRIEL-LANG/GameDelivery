from __future__ import annotations

from games.steam_cards.asf_sync import refresh_bot_snapshot
from games.steam_cards.snapshot import InventorySnapshotStore
from integrations.asf.client import ASFClient


class ASFInventoryController:
    """Explicit, on-demand inventory refresh. Never sends trades or edits ASF config."""

    def __init__(self, database_path, *, timeout: float = 30.0):
        self.store = InventorySnapshotStore(database_path)
        self.timeout = timeout

    def refresh(self, bot_name: str, ipc_password: str | None = None):
        if not bot_name.strip():
            raise ValueError("Укажи имя существующего бота ASF")
        client = ASFClient(ipc_password=ipc_password or None, timeout=self.timeout)
        return refresh_bot_snapshot(client, self.store, bot_name.strip())
