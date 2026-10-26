import unittest

from games.steam_cards.selection import CardCopy, find_available_sets, select_complete_sets


def cards(game, names, copies=1):
    return [
        CardCopy(asset_id=f"{game}-{name}-{copy}", game_name=game, card_name=name)
        for name in names
        for copy in range(copies)
    ]


class SteamCardSelectionTests(unittest.TestCase):
    def test_counts_full_sets_by_least_available_card(self):
        inventory = cards("Game A", ["A", "B", "C"], copies=2)
        inventory += [CardCopy("extra-a", "Game A", "A")]
        result = find_available_sets(inventory)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].available_sets, 2)
        self.assertEqual(result[0].cards_per_set, 3)
        self.assertEqual(len(result[0].asset_ids), 6)

    def test_ignores_foil_and_non_cards(self):
        inventory = cards("Game A", ["A", "B"])
        inventory += [
            CardCopy("foil-c", "Game A", "C", is_foil=True),
            CardCopy("item-d", "Game A", "D", is_trading_card=False),
        ]
        result = find_available_sets(inventory)
        self.assertEqual(result[0].cards_per_set, 2)
        self.assertEqual(result[0].available_sets, 1)

    def test_duplicate_asset_id_counted_once(self):
        inventory = [
            CardCopy("same", "Game A", "A"),
            CardCopy("same", "Game A", "B"),
            CardCopy("b", "Game A", "B"),
        ]
        result = find_available_sets(inventory)
        self.assertEqual(result[0].available_sets, 0 if False else 1)
        self.assertEqual(len(result[0].asset_ids), 2)

    def test_selects_required_sets_and_no_partial_result(self):
        inventory = cards("Small", ["A", "B"], copies=2)
        chosen = select_complete_sets(inventory, 2)
        self.assertEqual(chosen[0].available_sets, 2)
        self.assertEqual(len(chosen[0].asset_ids), 4)
        with self.assertRaises(ValueError):
            select_complete_sets(inventory, 3)

    def test_rejects_invalid_quantity(self):
        with self.assertRaises(ValueError):
            select_complete_sets([], 0)


if __name__ == "__main__":
    unittest.main()
