import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from games.steam_cards.asf_sync import refresh_bot_snapshot
from games.steam_cards.selection import CardCopy
from games.steam_cards.snapshot import InventorySnapshotStore
from integrations.asf.client import ASFError


class ASFSnapshotSyncTests(unittest.TestCase):
    def test_failed_fetch_does_not_replace_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            store = InventorySnapshotStore(Path(temp) / "inventory.sqlite3")
            store.replace([CardCopy("old", "Game", "Old Card")], bot_name="Bot A")
            client = Mock()
            client.get_bot_inventory.side_effect = ASFError("offline")
            with self.assertRaises(ASFError):
                refresh_bot_snapshot(client, store, "Bot A")
            self.assertEqual([card.asset_id for card in store.load("Bot A")], ["old"])

    def test_successful_fetch_replaces_only_target_bot_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            store = InventorySnapshotStore(Path(temp) / "inventory.sqlite3")
            store.replace([CardCopy("keep", "Other Game", "Other Card")], bot_name="Bot B")
            client = Mock()
            client.get_bot_inventory.return_value = {
                "Success": True,
                "Result": {
                    "Bot A": {
                        "Assets": [{"assetid": "new", "classid": "1", "instanceid": "0"}],
                        "Descriptions": [{"classid": "1", "instanceid": "0", "name": "New Card",
                                          "type": "Trading Card", "tags": [{"category": "Game",
                                          "localized_tag_name": "Example Game"}]}],
                    }
                },
            }
            result = refresh_bot_snapshot(client, store, "Bot A")
            self.assertEqual(result["item_count"], 1)
            self.assertEqual([card.asset_id for card in store.load("Bot A")], ["new"])
            self.assertEqual([card.asset_id for card in store.load("Bot B")], ["keep"])


if __name__ == "__main__":
    unittest.main()
