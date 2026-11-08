import tempfile
import unittest
from pathlib import Path

from games.steam_cards.order_preflight import SteamCardOrderPreflight
from games.steam_cards.recipient_badge import RecipientBadge
from games.steam_cards.reservations import AssetReservationStore
from games.steam_cards.selection import CardCopy


class MultiGameOrderPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.reservations = AssetReservationStore(Path(self.temp.name) / "reservations.sqlite")
        self.service = SteamCardOrderPreflight(self.reservations)
        self.inventories = {
            "Bot A": [
                CardCopy("dota-a1", "Dota 2", "Dota Alpha", market_hash_name="Dota Alpha"),
                CardCopy("dota-a2", "Dota 2", "Dota Alpha", market_hash_name="Dota Alpha"),
                CardCopy("rust-a1", "Rust", "Rust Alpha", market_hash_name="Rust Alpha"),
                CardCopy("rust-a2", "Rust", "Rust Alpha", market_hash_name="Rust Alpha"),
            ],
            "Bot B": [
                CardCopy("dota-b1", "Dota 2", "Dota Beta", market_hash_name="Dota Beta"),
                CardCopy("dota-b2", "Dota 2", "Dota Beta", market_hash_name="Dota Beta"),
                CardCopy("rust-b1", "Rust", "Rust Beta", market_hash_name="Rust Beta"),
                CardCopy("rust-b2", "Rust", "Rust Beta", market_hash_name="Rust Beta"),
            ],
        }

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, *, order_id="multi-1", requested_sets=4, **overrides):
        arguments = {
            "order_id": order_id,
            "requested_sets": requested_sets,
            "inventories": self.inventories,
            "recipient_badges": [RecipientBadge("Dota 2", 2), RecipientBadge("Rust", 3)],
            "badge_data_verified": True,
            "buyer_complete_sets_by_app_id": {570: 1, 252490: 0},
            "buyer_inventory_verified": True,
            "seller_inventory_verified": True,
            "card_hashes_by_app_id": {
                570: ("Dota Alpha", "Dota Beta"),
                252490: ("Rust Alpha", "Rust Beta"),
            },
            "card_definitions_verified": True,
        }
        arguments.update(overrides)
        return self.service.prepare_multi_game(**arguments)

    def test_maps_real_inventory_and_badges_to_exact_multi_game_order(self):
        result = self.prepare()

        self.assertTrue(result.ready_for_manual_review)
        self.assertEqual(sum(item.sets for item in result.allocations), 4)
        self.assertEqual({item.app_id: item.sets for item in result.allocations}, {570: 2, 252490: 2})
        self.assertEqual(len(result.selected_sets), 4)
        self.assertEqual(len(result.reserved_assets), 8)
        self.assertTrue(all(len(selected.asset_ids) == 2 for selected in result.selected_sets))
        self.assertTrue(all({asset.bot_name for asset in selected.asset_ids} == {"Bot A", "Bot B"}
                            for selected in result.selected_sets))
        self.assertEqual(
            {(row["bot_name"], row["asset_id"]) for row in self.reservations.list_for_order("multi-1")},
            set(result.reserved_assets),
        )
        self.assertIn("Отправка обмена не выполнялась", result.message)

    def test_infeasible_quantity_reserves_no_partial_assets(self):
        result = self.prepare(order_id="too-many", requested_sets=5)

        self.assertFalse(result.ready_for_manual_review)
        self.assertEqual(result.allocations, ())
        self.assertEqual(result.selected_sets, ())
        self.assertEqual(result.reserved_assets, ())
        self.assertEqual(self.reservations.list_for_order("too-many"), [])

    def test_excludes_other_orders_reservations_before_planning(self):
        self.reservations.reserve("other-order", (
            ("Bot A", "dota-a1"), ("Bot A", "dota-a2"),
            ("Bot B", "dota-b1"), ("Bot B", "dota-b2"),
        ))

        result = self.prepare(order_id="after-reservation", requested_sets=2)

        self.assertTrue(result.ready_for_manual_review)
        self.assertEqual({item.app_id: item.sets for item in result.allocations}, {252490: 2})
        self.assertEqual(len(result.reserved_assets), 4)
        self.assertEqual(len(self.reservations.list_for_order("other-order")), 4)

    def test_missing_or_unverified_buyer_inventory_counts_fail_closed(self):
        missing = self.prepare(
            order_id="missing-buyer-count",
            buyer_complete_sets_by_app_id={570: 1},
        )
        unverified = self.prepare(
            order_id="unverified-buyer-count",
            buyer_inventory_verified=False,
        )
        stale_seller = self.prepare(
            order_id="unverified-seller-snapshot",
            seller_inventory_verified=False,
        )

        for result, order_id in (
            (missing, "missing-buyer-count"),
            (unverified, "unverified-buyer-count"),
            (stale_seller, "unverified-seller-snapshot"),
        ):
            with self.subTest(order_id=order_id):
                self.assertFalse(result.ready_for_manual_review)
                self.assertEqual(result.reserved_assets, ())
                self.assertEqual(self.reservations.list_for_order(order_id), [])

    def test_unverified_card_definitions_fail_closed(self):
        result = self.prepare(
            order_id="unverified-card-definitions",
            card_definitions_verified=False,
        )

        self.assertFalse(result.ready_for_manual_review)
        self.assertEqual(result.reserved_assets, ())
        self.assertEqual(self.reservations.list_for_order("unverified-card-definitions"), [])

    def test_verification_flags_must_be_booleans(self):
        with self.assertRaises(ValueError):
            self.prepare(order_id="string-badge-verified", badge_data_verified="false")

        with self.assertRaises(ValueError):
            self.prepare(order_id="string-verified", buyer_inventory_verified="false")

        with self.assertRaises(ValueError):
            self.prepare(order_id="string-seller-verified", seller_inventory_verified="false")

        with self.assertRaises(ValueError):
            self.prepare(order_id="string-card-verified", card_definitions_verified="false")

    def test_count_and_card_definition_inputs_must_be_mappings(self):
        with self.assertRaises(ValueError):
            self.prepare(order_id="invalid-count-map", buyer_complete_sets_by_app_id=None)

        with self.assertRaises(ValueError):
            self.prepare(order_id="invalid-hash-map", card_hashes_by_app_id=None)

    def test_unknown_app_id_and_unverified_badges_fail_closed(self):
        inventories = {"Bot A": [
            CardCopy("u1", "Unsupported Game", "Alpha"),
            CardCopy("u2", "Unsupported Game", "Beta"),
        ]}
        unknown_game = self.prepare(
            order_id="unknown-game",
            requested_sets=1,
            inventories=inventories,
            recipient_badges=[],
            buyer_complete_sets_by_app_id={},
        )
        unverified_badges = self.prepare(
            order_id="unverified-badges",
            badge_data_verified=False,
        )

        for result, order_id in (
            (unknown_game, "unknown-game"),
            (unverified_badges, "unverified-badges"),
        ):
            with self.subTest(order_id=order_id):
                self.assertFalse(result.ready_for_manual_review)
                self.assertEqual(result.reserved_assets, ())
                self.assertEqual(self.reservations.list_for_order(order_id), [])

    def test_different_existing_order_reservations_are_not_replaced(self):
        existing = self.prepare(order_id="retry-order", requested_sets=1)
        before = self.reservations.list_for_order("retry-order")
        self.assertTrue(existing.ready_for_manual_review)

        result = self.prepare(
            order_id="retry-order",
            requested_sets=2,
            buyer_complete_sets_by_app_id={570: 0, 252490: 2},
        )

        self.assertFalse(result.ready_for_manual_review)
        self.assertEqual(self.reservations.list_for_order("retry-order"), before)


if __name__ == "__main__":
    unittest.main()
