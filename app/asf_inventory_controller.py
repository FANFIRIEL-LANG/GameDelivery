from __future__ import annotations

from games.steam_cards.asf_sync import refresh_bot_snapshot
from games.steam_cards.snapshot import InventorySnapshotStore
from integrations.asf.client import ASFClient


class ASFInventoryController:
    """Explicit, on-demand inventory refresh. Never sends trades or edits ASF config."""

    def __init__(self, database_path, *, timeout: float = 30.0, base_url: str = "http://127.0.0.1:1242", allow_alternate_loopback_port: bool = False):
        self.store = InventorySnapshotStore(database_path)
        self.timeout = timeout
        self.base_url = base_url
        self.allow_alternate_loopback_port = allow_alternate_loopback_port

    def refresh(self, bot_name: str, ipc_password: str | None = None):
        if not bot_name.strip():
            raise ValueError("Укажи имя существующего бота ASF")
        client = ASFClient(
            base_url=self.base_url, ipc_password=ipc_password or None,
            timeout=self.timeout,
            allow_alternate_loopback_port=self.allow_alternate_loopback_port,
        )
        return refresh_bot_snapshot(client, self.store, bot_name.strip())
