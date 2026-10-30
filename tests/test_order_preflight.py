import tempfile
import unittest
from pathlib import Path

from games.steam_cards.recipient_badge import RecipientBadge
from games.steam_cards.reservations import AssetReservationStore
from games.steam_cards.selection import CardCopy
from games.steam_cards.order_preflight import SteamCardOrderPreflight


class OrderPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.reservations = AssetReservationStore(Path(self.temp.name) / "db.sqlite")
        self.service = SteamCardOrderPreflight(self.reservations)
        self.inventories = {
            "Bot A": [CardCopy("101", "Game", "A"), CardCopy("102", "Game", "B")],
            "Bot B": [CardCopy("201", "Game", "A"), CardCopy("202", "Game", "B")],
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_unverified_badge_blocks_without_reserving(self):
        result = self.service.prepare(
            order_id="o1", game_name="Game", requested_sets=1,
            inventories=self.inventories, recipient_badges=[], badge_data_verified=False,
        )
        self.assertFalse(result.ready_for_manual_review)
        self.assertEqual(result.reserved_assets, ())
        self.assertEqual(self.reservations.list_for_order("o1"), [])

    def test_verified_badge_and_inventory_prepare_manual_review(self):
        result = self.service.prepare(
            order_id="o1", game_name="Game", requested_sets=1,
            inventories=self.inventories,
            recipient_badges=[RecipientBadge("Game", 2)],
            badge_data_verified=True,
        )
        self.assertTrue(result.ready_for_manual_review)
        self.assertEqual(len(result.selected_sets), 1)
        self.assertEqual(len(result.reserved_assets), 2)
        self.assertIn("ручной проверке", result.message)
        self.assertIn("не выполнялась", result.message)

    def test_foil_cards_are_never_reserved_when_normal_copy_exists(self):
        inventories = {
            "Bot A": [
                CardCopy("foil-a", "Game", "A", is_foil=True),
                CardCopy("normal-a", "Game", "A"),
                CardCopy("normal-b", "Game", "B"),
            ],
        }
        result = self.service.prepare(
            order_id="foil-exclusion", game_name="Game", requested_sets=1,
            inventories=inventories,
            recipient_badges=[RecipientBadge("Game", 0)], badge_data_verified=True,
        )
        self.assertTrue(result.ready_for_manual_review)
        self.assertEqual(
            set(result.reserved_assets),
            {("Bot A", "normal-a"), ("Bot A", "normal-b")},
        )
        self.assertNotIn(("Bot A", "foil-a"), result.reserved_assets)
        self.assertNotIn(
            ("Bot A", "foil-a"),
            {(row["bot_name"], row["asset_id"])
             for row in self.reservations.list_for_order("foil-exclusion")},
        )

    def test_foil_card_cannot_replace_missing_normal_card(self):
        inventories = {
            "Bot A": [
                CardCopy("foil-a", "Game", "A", is_foil=True),
                CardCopy("normal-b", "Game", "B"),
            ],
        }
        with self.assertRaisesRegex(ValueError, "полных наборов"):
            self.service.prepare(
                order_id="foil-only", game_name="Game", requested_sets=1,
                inventories=inventories,
                recipient_badges=[RecipientBadge("Game", 0)], badge_data_verified=True,
            )
        self.assertEqual(self.reservations.list_for_order("foil-only"), [])

    def test_badge_capacity_blocks_order(self):
        result = self.service.prepare(
            order_id="o1", game_name="Game", requested_sets=2,
            inventories=self.inventories,
            recipient_badges=[RecipientBadge("Game", 4)],
            badge_data_verified=True,
        )
        self.assertFalse(result.ready_for_manual_review)
        self.assertEqual(self.reservations.list_for_order("o1"), [])

    def test_reservation_conflict_blocks_second_order_atomically(self):
        first = self.service.prepare(
            order_id="o1", game_name="Game", requested_sets=1,
            inventories={"Bot A": self.inventories["Bot A"]},
            recipient_badges=[RecipientBadge("Game", 0)], badge_data_verified=True,
        )
        self.assertTrue(first.ready_for_manual_review)
        second = self.service.prepare(
            order_id="o2", game_name="Game", requested_sets=1,
            inventories={"Bot A": self.inventories["Bot A"]},
            recipient_badges=[RecipientBadge("Game", 0)], badge_data_verified=True,
        )
        self.assertFalse(second.ready_for_manual_review)
        self.assertEqual(self.reservations.list_for_order("o2"), [])

    def test_cancel_review_releases_reservations(self):
        self.service.prepare(
            order_id="o1", game_name="Game", requested_sets=1,
            inventories=self.inventories,
            recipient_badges=[RecipientBadge("Game", 0)], badge_data_verified=True,
        )
        self.assertEqual(self.service.cancel_preflight("o1"), 2)
        self.assertEqual(self.reservations.list_for_order("o1"), [])


if __name__ == "__main__":
    unittest.main()
