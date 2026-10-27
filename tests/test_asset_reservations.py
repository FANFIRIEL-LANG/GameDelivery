import tempfile
import unittest
from pathlib import Path

from games.steam_cards.reservations import AssetReservationStore


class AssetReservationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = Path(self.temp_dir.name) / "reservations.sqlite3"
        self.store = AssetReservationStore(self.database)

    def test_reserve_stores_exact_assets_for_order(self):
        result = self.store.reserve("GD-1", [("Bot A", "123"), ("Bot A", "456")])

        self.assertTrue(result["reserved"])
        self.assertEqual(result["order_id"], "GD-1")
        self.assertEqual(
            self.store.list_for_order("GD-1"),
            [
                {"bot_name": "Bot A", "asset_id": "123"},
                {"bot_name": "Bot A", "asset_id": "456"},
            ],
        )

    def test_conflict_rejects_entire_batch_without_partial_reservation(self):
        self.store.reserve("GD-1", [("Bot A", "123")])

        result = self.store.reserve("GD-2", [("Bot A", "456"), ("Bot A", "123")])

        self.assertFalse(result["reserved"])
        self.assertEqual(result["conflicts"], [
            {"bot_name": "Bot A", "asset_id": "123", "reserved_by": "GD-1"}
        ])
        self.assertEqual(self.store.list_for_order("GD-2"), [])
        self.assertEqual(
            self.store.list_for_order("GD-1"),
            [{"bot_name": "Bot A", "asset_id": "123"}],
        )

    def test_same_order_can_repeat_reservation_without_duplicate_rows(self):
        assets = [("Bot A", "123"), ("Bot A", "123"), ("Bot B", "123")]

        first = self.store.reserve("GD-1", assets)
        second = self.store.reserve("GD-1", assets)

        self.assertTrue(first["reserved"])
        self.assertTrue(second["reserved"])
        self.assertEqual(len(self.store.list_for_order("GD-1")), 2)

    def test_bot_name_case_variants_do_not_bypass_asset_reservations(self):
        self.store.reserve("GD-1", [("Bot A", "123")])

        conflict = self.store.reserve("GD-2", [("bot a", "123")])
        same_order = self.store.reserve("GD-1", [("BOT A", "00123")])

        self.assertFalse(conflict["reserved"])
        self.assertEqual(conflict["conflicts"][0]["reserved_by"], "GD-1")
        self.assertTrue(same_order["reserved"])
        self.assertEqual(
            self.store.list_for_order("GD-1"),
            [{"bot_name": "BOT A", "asset_id": "123"}],
        )
        self.assertEqual(self.store.list_for_order("GD-2"), [])

    def test_same_asset_id_on_different_bots_is_a_separate_reservation(self):
        first = self.store.reserve("GD-1", [("Bot A", "123")])
        second = self.store.reserve("GD-2", [("Bot B", "123")])

        self.assertTrue(first["reserved"])
        self.assertTrue(second["reserved"])

    def test_release_only_removes_the_requested_orders_reservations(self):
        self.store.reserve("GD-1", [("Bot A", "123"), ("Bot A", "456")])
        self.store.reserve("GD-2", [("Bot A", "789")])

        released = self.store.release("GD-1")

        self.assertEqual(released, 2)
        self.assertEqual(self.store.list_for_order("GD-1"), [])
        self.assertEqual(
            self.store.list_for_order("GD-2"),
            [{"bot_name": "Bot A", "asset_id": "789"}],
        )

    def test_empty_order_or_assets_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "order_id"):
            self.store.reserve(" ", [("Bot A", "123")])
        with self.assertRaisesRegex(ValueError, "непустой список"):
            self.store.reserve("GD-1", [])
        with self.assertRaisesRegex(ValueError, "непустой список"):
            self.store.reserve("GD-1", [("Bot A", "  ")])

    def test_reservations_accept_opaque_non_empty_asset_identifiers(self):
        result = self.store.reserve("GD-1", [("Bot A", "asset-123")])

        self.assertTrue(result["reserved"])
        self.assertEqual(
            self.store.list_for_order("GD-1"),
            [{"bot_name": "Bot A", "asset_id": "asset-123"}],
        )

    def test_bot_name_must_not_be_blank(self):
        with self.assertRaisesRegex(ValueError, "корректных"):
            self.store.reserve("GD-1", [(" ", "123")])


if __name__ == "__main__":
    unittest.main()
