import unittest

from games.steam_cards.inventory_views import build_inventory_view
from games.steam_cards.selection import CardCopy


class InventoryGeneratorTests(unittest.TestCase):
    def test_shared_view_supports_one_shot_generators_and_preserves_owners(self):
        inventories = {
            "Bot A": (card for card in [CardCopy("a1", "Game", "Card"), CardCopy("a2", "Game", "Card")]),
            "Bot B": (card for card in [CardCopy("b1", "Game", "Card")]),
        }
        view = build_inventory_view(inventories, ["Bot A", "Bot B"], mode="shared")
        self.assertEqual(len(view.rows), 1)
        self.assertEqual(view.rows[0].quantity, 3)
        self.assertEqual(dict(view.rows[0].assets_by_bot), {
            "Bot A": ("a1", "a2"),
            "Bot B": ("b1",),
        })


if __name__ == "__main__":
    unittest.main()
