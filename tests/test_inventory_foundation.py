import sqlite3
import tempfile
import unittest
from pathlib import Path

from games.steam_cards.selection import CardCopy
from games.steam_cards.snapshot import InventorySnapshotStore
from games.steam_cards.owned_selection import select_owned_complete_sets
from games.steam_cards.reservations import AssetReservationStore


class InventorySnapshotMigrationTests(unittest.TestCase):
    def test_per_bot_snapshots_allow_same_asset_id(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InventorySnapshotStore(Path(directory) / "inventory.db")
            store.replace([CardCopy("same", "Game", "A")], "Bot A")
            store.replace([CardCopy("same", "Game", "B")], "Bot B")
            self.assertEqual(store.load("Bot A")[0].card_name, "A")
            self.assertEqual(store.load("Bot B")[0].card_name, "B")
            self.assertEqual(set(store.load_all()), {"Bot A", "Bot B"})

    def test_replace_one_bot_does_not_clear_another(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InventorySnapshotStore(Path(directory) / "inventory.db")
            store.replace([CardCopy("a", "Game", "A")], "A")
            store.replace([CardCopy("b", "Game", "B")], "B")
            store.replace([], "A")
            self.assertEqual(store.load("A"), [])
            self.assertEqual([c.asset_id for c in store.load("B")], ["b"])

    def test_migrates_legacy_snapshot_without_losing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                    CREATE TABLE steam_inventory_snapshot (
                        asset_id TEXT PRIMARY KEY, game_name TEXT NOT NULL,
                        card_name TEXT NOT NULL, is_trading_card INTEGER NOT NULL,
                        is_foil INTEGER NOT NULL
                    );
                    INSERT INTO steam_inventory_snapshot VALUES ('old-id','Old Game','Old Card',1,0);
                    CREATE TABLE steam_inventory_snapshot_meta (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        updated_at TEXT NOT NULL, item_count INTEGER NOT NULL
                    );
                    INSERT INTO steam_inventory_snapshot_meta VALUES (1,'2026-01-01T00:00:00+00:00',1);
                """)
            store = InventorySnapshotStore(path)
            self.assertEqual(store.load("legacy")[0].asset_id, "old-id")
            self.assertEqual(store.status("legacy")["item_count"], 1)
            self.assertEqual(store.status("legacy")["updated_at"], "2026-01-01T00:00:00+00:00")
            # Legacy snapshots have no trustworthy tradability field and stay unverified.
            self.assertIsNone(store.load("legacy")[0].is_tradable)


class OwnedSetSelectionTests(unittest.TestCase):
    def test_shared_selection_keeps_exact_owner(self):
        inventories = {
            "Bot A": [CardCopy("a1", "Game", "A"), CardCopy("a2", "Game", "B")],
            "Bot B": [CardCopy("b1", "Game", "A"), CardCopy("b2", "Game", "B")],
        }
        result = select_owned_complete_sets(inventories, "Game", 2)
        self.assertEqual(len(result), 2)
        self.assertEqual({asset.bot_name for asset in result[0].asset_ids}, {"Bot A"})
        self.assertEqual({asset.asset_id for asset in result[0].asset_ids}, {"a1", "a2"})
        self.assertEqual({asset.bot_name for asset in result[1].asset_ids}, {"Bot B"})
        self.assertEqual({asset.asset_id for asset in result[1].asset_ids}, {"b1", "b2"})

    def test_shared_selection_excludes_assets_not_confirmed_tradable(self):
        inventories = {
            "Bot A": [
                CardCopy("a", "Game", "A", is_tradable=True),
                CardCopy("b", "Game", "B", is_tradable=False),
            ],
        }
        with self.assertRaisesRegex(ValueError, "полных наборов"):
            select_owned_complete_sets(inventories, "Game", 1)

    def test_shared_selection_blocks_insufficient_sets_and_ignores_foil(self):
        inventories = {
            "Bot A": [CardCopy("a", "Game", "A"), CardCopy("foil", "Game", "B", is_foil=True)],
            "Bot B": [CardCopy("b", "Game", "A")],
        }
        with self.assertRaises(ValueError):
            select_owned_complete_sets(inventories, "Game", 1)


class AssetReservationTests(unittest.TestCase):
    def test_reservation_conflict_is_atomic_and_scoped_by_bot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AssetReservationStore(Path(directory) / "reservations.db")
            self.assertTrue(store.reserve("order-1", [("Bot A", "same")])["reserved"])
            conflict = store.reserve("order-2", [("Bot A", "same"), ("Bot B", "new")])
            self.assertFalse(conflict["reserved"])
            self.assertEqual(conflict["conflicts"][0]["reserved_by"], "order-1")
            self.assertEqual(store.list_for_order("order-2"), [])
            self.assertTrue(store.reserve("order-3", [("Bot B", "same")])["reserved"])
            self.assertEqual(store.release("order-1"), 1)


if __name__ == "__main__":
    unittest.main()
