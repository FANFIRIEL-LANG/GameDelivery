import tempfile
import unittest
from pathlib import Path

from games.steam_cards.selection import CardCopy
from games.steam_cards.snapshot import InventorySnapshotStore


class InventorySnapshotTests(unittest.TestCase):
    def test_empty_snapshot_is_marked_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InventorySnapshotStore(Path(directory) / "app.db")
            self.assertEqual(store.status(), {"updated_at": None, "item_count": 0, "stale": True})

    def test_replace_persists_snapshot_and_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InventorySnapshotStore(Path(directory) / "app.db")
            status = store.replace([
                CardCopy("asset-1", "Game", "Card A"),
                CardCopy("asset-2", "Game", "Card B", is_foil=True, is_tradable=False),
                CardCopy("asset-1", "Game", "Duplicate asset"),
            ])
            self.assertEqual(status["item_count"], 2)
            self.assertFalse(status["stale"])
            loaded = store.load()
            self.assertEqual([card.asset_id for card in loaded], ["asset-1", "asset-2"])
            self.assertTrue(loaded[0].is_tradable)
            self.assertFalse(loaded[1].is_tradable)
            self.assertEqual(store.status()["updated_at"], status["updated_at"])

    def test_reopen_reads_saved_snapshot_without_fetching(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.db"
            InventorySnapshotStore(path).replace([CardCopy("asset-1", "Game", "Card A")])
            reopened = InventorySnapshotStore(path)
            self.assertEqual(reopened.load()[0].asset_id, "asset-1")


    def test_empty_saved_snapshot_is_stale_even_when_metadata_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InventorySnapshotStore(Path(directory) / "app.db")
            store.replace([])
            status = store.status()
            self.assertEqual(status["item_count"], 0)
            self.assertTrue(status["stale"])

    def test_snapshot_older_than_24_hours_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.db"
            store = InventorySnapshotStore(path)
            store.replace([CardCopy("asset-1", "Game", "Card A")])
            with store._connect() as db:
                db.execute(
                    "UPDATE steam_inventory_snapshot_meta SET updated_at = ? WHERE bot_name = ?",
                    ("2020-01-01T00:00:00+00:00", "default"),
                )
            self.assertTrue(store.status()["stale"])

    def test_snapshot_with_invalid_timestamp_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.db"
            store = InventorySnapshotStore(path)
            store.replace([CardCopy("asset-1", "Game", "Card A")])
            with store._connect() as db:
                db.execute(
                    "UPDATE steam_inventory_snapshot_meta SET updated_at = ? WHERE bot_name = ?",
                    ("not-a-timestamp", "default"),
                )
            self.assertTrue(store.status()["stale"])



if __name__ == "__main__":
    unittest.main()
