"""Playerok marketplace connector.

The connector uses the isolated adapter in client.py and stores its session
through the OS keyring rather than in the application database.
"""
from integrations.playerok.client import PlayerokAdapter

__all__ = ["PlayerokAdapter"]
