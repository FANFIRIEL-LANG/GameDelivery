import unittest

from games.steam_cards.inventory_views import (
    build_inventory_view, expand_inventory_instances, filter_inventory_rows, select_inventory_instances,
)

from games.steam_cards.selection import CardCopy


def card(asset_id, name="Card A", game="Game", foil=False, tradable=True):
    return CardCopy(asset_id, game, name, is_foil=foil, is_tradable=tradable)


class InventoryViewTests(unittest.TestCase):
    def setUp(self):
        self.inventories = {
            "Bot A": [card("same-id"), card("a2"), card("foil", foil=True)],
            "Bot B": [card("same-id"), card("b2"), card("other", name="Card B")],
            "Bot C": [card("c1", game="Other Game")],
        }

    def test_no_selected_bots_returns_empty_view(self):
        view = build_inventory_view(self.inventories, [], mode="shared")
        self.assertEqual(view.rows, ())
        self.assertEqual(view.selected_bots, ())

    def test_shared_mode_aggregates_quantity_but_preserves_owner(self):
        view = build_inventory_view(self.inventories, ["Bot A", "Bot B"], mode="shared")
        row = next(row for row in view.rows if row.game_name == "Game" and row.card_name == "Card A" and not row.is_foil)
        self.assertEqual(row.quantity, 4)
        self.assertEqual(dict(row.assets_by_bot), {
            "Bot A": ("same-id", "a2"),
            "Bot B": ("same-id", "b2"),
        })

    def test_expanded_instances_keep_exact_bot_ownership_for_quantity_selection(self):
        view = build_inventory_view(self.inventories, ["Bot A", "Bot B"], mode="shared")
        row = next(row for row in view.rows if row.card_name == "Card A" and not row.is_foil)
        self.assertEqual(
            expand_inventory_instances(row),
            (("Bot A", "same-id"), ("Bot A", "a2"), ("Bot B", "same-id"), ("Bot B", "b2")),
        )

    def test_same_asset_id_on_different_bots_is_not_a_duplicate(self):
        view = build_inventory_view(self.inventories, ["Bot A", "Bot B"], mode="shared")
        row = next(row for row in view.rows if row.card_name == "Card A" and not row.is_foil)
        self.assertEqual(row.quantity, 4)
        self.assertEqual(len(dict(row.assets_by_bot)), 2)

    def test_separate_mode_keeps_bot_rows_separate(self):
        view = build_inventory_view(self.inventories, ["Bot A", "Bot B"], mode="separate")
        rows = [row for row in view.rows if row.card_name == "Card A" and not row.is_foil]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].quantity, 2)
        self.assertEqual(rows[1].quantity, 2)
        self.assertNotEqual(rows[0].assets_by_bot, rows[1].assets_by_bot)

    def test_unknown_bot_is_ignored_and_duplicate_selection_removed(self):
        view = build_inventory_view(
            self.inventories, ["Bot A", "Unknown", "Bot A"], mode="separate"
        )
        self.assertEqual(view.selected_bots, ("Bot A",))
        self.assertTrue(all(dict(row.assets_by_bot).keys() == {"Bot A"} for row in view.rows))

    def test_inventory_view_keeps_tradability_distinct(self):
        inventories = {
            "Bot A": [
                card("tradable", tradable=True),
                card("nontradable", tradable=False),
                card("unknown", tradable=None),
            ],
        }
        view = build_inventory_view(inventories, ["Bot A"], mode="separate")
        by_transferability = {row.is_tradable: row for row in view.rows}
        self.assertEqual(by_transferability[True].quantity, 1)
        self.assertEqual(by_transferability[False].quantity, 1)
        self.assertEqual(by_transferability[None].quantity, 1)

    def test_shared_view_preserves_artwork_and_cs_quality_metadata(self):
        inventories = {
            "Bot A": [CardCopy(
                "asset-1", "Counter-Strike 2", "AK-47 | Redline",
                is_trading_card=False,
                icon_url="https://community.cloudflare.steamstatic.com/economy/image/icon",
                market_hash_name="AK-47 | Redline (Field-Tested)",
                item_quality="FT",
            )],
            "Bot B": [CardCopy(
                "asset-2", "Counter-Strike 2", "AK-47 | Redline",
                is_trading_card=False,
                icon_url="https://community.cloudflare.steamstatic.com/economy/image/icon",
                market_hash_name="AK-47 | Redline (Field-Tested)",
                item_quality="FT",
            )],
        }
        view = build_inventory_view(inventories, ["Bot A", "Bot B"], mode="shared")
        self.assertEqual(len(view.rows), 1)
        self.assertEqual(view.rows[0].quantity, 2)
        self.assertEqual(view.rows[0].icon_url, "https://community.cloudflare.steamstatic.com/economy/image/icon")
        self.assertEqual(view.rows[0].market_hash_name, "AK-47 | Redline (Field-Tested)")
        self.assertEqual(view.rows[0].item_quality, "FT")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            build_inventory_view(self.inventories, ["Bot A"], mode="merged")


    def test_inventory_search_matches_game_card_owner_and_asset_id(self):
        view = build_inventory_view(self.inventories, ["Bot A", "Bot B"], mode="shared")
        self.assertEqual(
            [row.card_name for row in filter_inventory_rows(view.rows, "bot b")],
            ["Card A", "Card B"],
        )
        self.assertEqual(
            [row.card_name for row in filter_inventory_rows(view.rows, "same-id")],
            ["Card A"],
        )
        self.assertEqual(
            [row.card_name for row in filter_inventory_rows(view.rows, "Other Game")],
            [],
        )

    def test_inventory_type_and_transfer_filters(self):
        inventories = {
            "Bot A": [
                card("card", "Card", tradable=True),
                CardCopy("item", "Game", "Item", is_trading_card=False, is_tradable=False),
                card("foil", "Foil", foil=True, tradable=True),
                card("unknown", "Unknown", tradable=None),
            ],
        }
        view = build_inventory_view(inventories, ["Bot A"], mode="shared")
        self.assertEqual({row.card_name for row in filter_inventory_rows(view.rows, item_filter="cards")}, {"Card", "Foil", "Unknown"})
        self.assertEqual({row.card_name for row in filter_inventory_rows(view.rows, item_filter="items")}, {"Item"})
        self.assertEqual({row.card_name for row in filter_inventory_rows(view.rows, item_filter="tradable")}, {"Card", "Foil"})
        self.assertEqual({row.card_name for row in filter_inventory_rows(view.rows, item_filter="nontradable")}, {"Item"})
        self.assertEqual({row.card_name for row in filter_inventory_rows(view.rows, item_filter="foil")}, {"Foil"})

    def test_unknown_inventory_filter_is_rejected(self):
        with self.assertRaises(ValueError):
            filter_inventory_rows((), item_filter="unknown")

    def test_quantity_selection_preserves_owner_and_asset_and_deduplicates(self):
        instances = [("Bot B", "2"), ("Bot A", "1"), ("Bot A", "1"), ("Bot B", "3")]
        self.assertEqual(select_inventory_instances(instances, 2), (("Bot B", "2"), ("Bot A", "1")))
        self.assertEqual(select_inventory_instances(instances, 0), ())

    def test_quantity_selection_rejects_invalid_or_unavailable_quantity(self):
        instances = [("Bot A", "1")]
        with self.assertRaises(ValueError):
            select_inventory_instances(instances, -1)
        with self.assertRaises(ValueError):
            select_inventory_instances(instances, 2)


if __name__ == "__main__":
    unittest.main()
