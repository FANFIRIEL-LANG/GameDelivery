import unittest

from games.steam_cards.selection import CardCopy
from games.steam_cards.shared_sets import find_shared_game_sets, select_shared_game_sets


class SharedSteamCardSetTests(unittest.TestCase):
    def test_combines_cards_from_different_bots_into_one_complete_set(self):
        inventories = {
            "Bot A": [CardCopy("a1", "Game", "Card A")],
            "Bot B": [CardCopy("b1", "Game", "Card B")],
            "Bot C": [CardCopy("c1", "Game", "Card C")],
        }
        result = find_shared_game_sets(inventories)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].available_sets, 1)
        self.assertEqual(result[0].cards_per_set, 3)
        self.assertEqual(set(result[0].sets[0]), {
            ("Bot A", "a1"), ("Bot B", "b1"), ("Bot C", "c1")
        })

    def test_counts_multiple_sets_spread_across_bots(self):
        inventories = {
            "Bot A": [
                CardCopy("a1", "Game", "Card A"),
                CardCopy("a2", "Game", "Card A"),
                CardCopy("b1", "Game", "Card B"),
            ],
            "Bot B": [
                CardCopy("b2", "Game", "Card B"),
                CardCopy("c1", "Game", "Card C"),
            ],
            "Bot C": [CardCopy("c2", "Game", "Card C")],
        }
        result = find_shared_game_sets(inventories)[0]
        self.assertEqual(result.available_sets, 2)
        self.assertEqual(len(result.sets), 2)
        self.assertTrue(all(len(one_set) == 3 for one_set in result.sets))
        assets = [asset for one_set in result.sets for asset in one_set]
        self.assertEqual(len(assets), len(set(assets)))

    def test_same_asset_id_on_different_bots_is_two_distinct_assets(self):
        inventories = {
            "Bot A": [CardCopy("same", "Game", "Card A")],
            "Bot B": [CardCopy("same", "Game", "Card B")],
        }
        selected = select_shared_game_sets(inventories, "Game", 1)
        self.assertEqual(set(selected[0]), {("Bot A", "same"), ("Bot B", "same")})

    def test_duplicate_asset_within_one_bot_is_counted_once(self):
        inventories = {
            "Bot A": [
                CardCopy("a1", "Game", "Card A"),
                CardCopy("a1", "Game", "Card B"),
            ],
            "Bot B": [CardCopy("b1", "Game", "Card B")],
        }
        result = find_shared_game_sets(inventories)
        self.assertEqual(result[0].available_sets, 1)
        self.assertEqual(set(result[0].sets[0]), {("Bot A", "a1"), ("Bot B", "b1")})

    def test_foil_non_card_and_nontradable_items_are_excluded(self):
        inventories = {"Bot A": [
            CardCopy("a1", "Game", "Card A"),
            CardCopy("foil", "Game", "Card B", is_foil=True),
            CardCopy("background", "Game", "Card B", is_trading_card=False),
            CardCopy("blocked", "Game", "Card B", is_tradable=False),
        ]}
        self.assertEqual(find_shared_game_sets(inventories), [])

    def test_games_are_counted_separately(self):
        inventories = {
            "Bot A": [
                CardCopy("a1", "Game A", "Card A"),
                CardCopy("b1", "Game B", "Card B"),
            ],
            "Bot B": [
                CardCopy("a2", "Game A", "Card B"),
                CardCopy("b2", "Game B", "Card A"),
            ],
        }
        result = find_shared_game_sets(inventories)
        self.assertEqual(
            [(item.game_name, item.available_sets) for item in result],
            [("Game A", 1), ("Game B", 1)],
        )

    def test_selection_returns_exact_number_and_preserves_owners(self):
        inventories = {
            "Bot A": [
                CardCopy("a1", "Game", "Card A"),
                CardCopy("a2", "Game", "Card A"),
                CardCopy("b1", "Game", "Card B"),
            ],
            "Bot B": [CardCopy("b2", "Game", "Card B")],
        }
        selected = select_shared_game_sets(inventories, "game", 2)
        self.assertEqual(len(selected), 2)
        self.assertEqual(sum(len(one_set) for one_set in selected), 4)
        self.assertEqual(
            {bot for one_set in selected for bot, _ in one_set},
            {"Bot A", "Bot B"},
        )

    def test_insufficient_stock_does_not_return_partial_sets(self):
        inventories = {
            "Bot A": [CardCopy("a1", "Game", "Card A")],
            "Bot B": [CardCopy("b1", "Game", "Card B")],
        }
        with self.assertRaisesRegex(ValueError, "доступно 1, требуется 2"):
            select_shared_game_sets(inventories, "Game", 2)

    def test_rejects_invalid_game_or_quantity(self):
        with self.assertRaises(ValueError):
            select_shared_game_sets({}, "Game", 0)
        with self.assertRaises(ValueError):
            select_shared_game_sets({}, " ", 1)


if __name__ == "__main__":
    unittest.main()
