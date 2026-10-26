import unittest
from unittest.mock import patch

from games.steam_cards.asf_inventory import parse_bot_inventory
from integrations.asf.client import ASFClient, ASFError


class ASFInventoryTests(unittest.TestCase):
    def test_inventory_route_uses_official_app_context_endpoint(self):
        with patch.object(ASFClient, "request", return_value={"Success": True, "Result": {}}) as request:
            ASFClient().get_bot_inventory("My Bot", app_id=753, context_id=6)
        request.assert_called_once_with("/Api/Bot/My%20Bot/Inventory/753/6?language=english")

    def test_parser_matches_descriptions_and_identifies_normal_and_foil_cards(self):
        payload = {
            "Success": True,
            "Result": {
                "Bot A": {
                    "Assets": [
                        {"assetid": "100", "classid": "10", "instanceid": "0"},
                        {"assetid": "101", "classid": "11", "instanceid": "0"},
                        {"assetid": "102", "classid": "12", "instanceid": "0"},
                    ],
                    "Descriptions": [
                        {"classid": "10", "instanceid": "0", "name": "Card A", "type": "Steam Trading Card"},
                        {"classid": "11", "instanceid": "0", "name": "Card B", "type": "Foil Trading Card"},
                        {"classid": "12", "instanceid": "0", "name": "Background", "type": "Profile Background"},
                    ],
                }
            },
        }
        cards = parse_bot_inventory(payload, "Bot A", "Example Game")
        self.assertEqual(len(cards), 3)
        self.assertEqual((cards[0].asset_id, cards[0].card_name, cards[0].is_trading_card, cards[0].is_foil),
                         ("100", "Card A", True, False))
        self.assertEqual((cards[1].asset_id, cards[1].card_name, cards[1].is_trading_card, cards[1].is_foil),
                         ("101", "Card B", True, True))
        self.assertFalse(cards[2].is_trading_card)

    def test_parser_does_not_treat_failed_response_as_empty_inventory(self):
        with self.assertRaises(ASFError):
            parse_bot_inventory({"Success": False}, "Bot A", "Game")

    def test_parser_requires_requested_bot_to_be_present(self):
        with self.assertRaises(ASFError):
            parse_bot_inventory({"Success": True, "Result": {}}, "Bot A", "Game")


if __name__ == "__main__":
    unittest.main()
