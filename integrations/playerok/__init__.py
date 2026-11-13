"""Playerok connector and durable deal synchronization."""
from integrations.playerok.client import PlayerokAdapter
from integrations.playerok.sync import PlayerokDealSyncService

__all__ = ["PlayerokAdapter", "PlayerokDealSyncService"]
