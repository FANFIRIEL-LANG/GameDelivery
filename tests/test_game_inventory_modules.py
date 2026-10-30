import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from games.catalog import GAME_CATALOG, get_game
from games.inventory import (\n    GameInventoryStore, parse_game_inventory, refresh_game_inventory,\n    is_container_item, filter_game_inventory_items,\n)
from integrations.asf.client import ASFError
from app.game_inventory_cards import rarity_color, stattrak_label, GameItemImageLoader


def payload():
    return {
        "Success": True,
        "Result": {
            "Bot A": {
                "Assets": [
                    {"assetid": "1001", "classid": "2001", "instanceid": "0", "amount": "1"}
                ],
                "Descriptions": [
                    {
                        "classid": "2001", "instanceid": "0",
                        "name": "AK-47 | Redline (Field-Tested)",
                        "type": "Classified Rifle",
                        "market_hash_name": "AK-47 | Redline (Field-Tested)",
                        "icon_url": "abc123",
                        "tradable": 1, "marketable": 1,
                        "tags": [
                            {"category": "Exterior", "localized_tag_name": "Field-Tested", "internal_name": "wearcondition_well_worn"},
                            {"category": "Rarity", "localized_tag_name": "Classified", "internal_name": "rarity4"},
                        ],
                    }
                ],
            }
        },
    }


class GameCatalogTests(unittest.TestCase):
    def test_requested_games_have_expected_steam_inventory_routes(self):
        self.assertEqual(
            [(g.game_id, g.app_id, g.context_id) for g in GAME_CATALOG],
            [("cs2", 730, 2), ("dota2", 570, 2), ("steam_cards", 753, 6),
             ("tf2", 440, 2), ("rust", 252490, 2)],
        )
        self.assertEqual(get_game("CS2").game_id, "cs2")
        with self.assertRaises(ValueError):
            get_game("unknown")


class GameInventoryParserTests(unittest.TestCase):
    def test_parses_generic_item_metadata_without_card_assumptions(self):
        items = parse_game_inventory(payload(), "cs2", "Bot A")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item.game_id, item.bot_name, item.asset_id), ("cs2", "Bot A", "1001"))
        self.assertEqual(item.name, "AK-47 | Redline (Field-Tested)")
        self.assertEqual(item.exterior, "Field-Tested")
        self.assertEqual(item.rarity, "Classified")
        self.assertTrue(item.is_tradable)
        self.assertTrue(item.is_marketable)
        self.assertTrue(item.icon_url.startswith("https://"))

    def test_unknown_tradability_is_not_promoted_to_true(self):
        data = payload()
        del data["Result"]["Bot A"]["Descriptions"][0]["tradable"]
        item = parse_game_inventory(data, "dota2", "Bot A")[0]
        self.assertIsNone(item.is_tradable)

    def test_incomplete_response_rejected(self):
        with self.assertRaises(ASFError):
            parse_game_inventory({"Success": True, "Result": {"Bot A": {"Assets": []}}}, "cs2", "Bot A")

    def test_duplicate_asset_id_rejected(self):
        data = payload()
        data["Result"]["Bot A"]["Assets"].append(dict(data["Result"]["Bot A"]["Assets"][0]))
        with self.assertRaises(ASFError):
            parse_game_inventory(data, "cs2", "Bot A")


class GameInventoryStoreTests(unittest.TestCase):
    def test_refresh_uses_game_route_and_persists_separate_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GameInventoryStore(Path(directory) / "inventory.sqlite3")
            client = Mock()
            client.get_bot_inventory.return_value = payload()
            result = refresh_game_inventory(client, store, "cs2", "Bot A")
            client.get_bot_inventory.assert_called_once_with(
                "Bot A", app_id=730, context_id=2, language="english"
            )
            self.assertEqual(result["item_count"], 1)
            self.assertEqual(store.load("cs2", "Bot A")[0].asset_id, "1001")

    def test_load_all_keeps_each_items_account_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GameInventoryStore(Path(directory) / "inventory.sqlite3")
            store.replace("cs2", "Bot A", parse_game_inventory(payload(), "cs2", "Bot A"))
            data_b = payload()
            data_b["Result"]["Bot B"] = data_b["Result"].pop("Bot A")
            store.replace("cs2", "Bot B", parse_game_inventory(data_b, "cs2", "Bot B"))
            all_items = store.load_all("cs2")
            self.assertEqual({item.bot_name for item in all_items}, {"Bot A", "Bot B"})
            self.assertEqual(len(all_items), 2)

    def test_snapshots_are_isolated_by_game_and_bot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GameInventoryStore(Path(directory) / "inventory.sqlite3")
            item = parse_game_inventory(payload(), "cs2", "Bot A")
            store.replace("cs2", "Bot A", item)
            self.assertEqual(store.load("cs2", "Bot A"), item)
            self.assertEqual(store.load("dota2", "Bot A"), [])
            self.assertEqual(store.load("cs2", "Bot B"), [])


class GameInventoryFilteringTests(unittest.TestCase):
    def test_cs2_container_is_detected_from_item_type(self):
        item = parse_game_inventory(payload(), "cs2", "Bot A")[0]
        from dataclasses import replace
        container = replace(item, item_type="Base Grade Container")
        self.assertTrue(is_container_item(container))
        self.assertEqual(filter_game_inventory_items([item, container], True), [container])

    def test_cs2_container_is_detected_from_type_tag(self):
        item = parse_game_inventory(payload(), "cs2", "Bot A")[0]
        from dataclasses import replace
        container = replace(item, item_type="Base Grade", tags=("Container",))
        self.assertTrue(is_container_item(container))

    def test_non_cs2_items_are_not_classified_as_cs2_containers(self):
        item = parse_game_inventory(payload(), "dota2", "Bot A")[0]
        from dataclasses import replace
        misleading = replace(item, item_type="Container", tags=("Container",))
        self.assertFalse(is_container_item(misleading))
        self.assertEqual(filter_game_inventory_items([misleading], True), [])

    def test_all_items_filter_preserves_inventory(self):
        items = parse_game_inventory(payload(), "cs2", "Bot A")
        self.assertEqual(filter_game_inventory_items(items), items)


class GameInventoryImageFallbackTests(unittest.TestCase):
    def test_image_candidates_match_steam_cards_fallback_hosts(self):
        url = "https://community.cloudflare.steamstatic.com/economy/image/abcdef/96fx96f"
        candidates = GameItemImageLoader.steam_image_candidates(url)
        self.assertEqual(len(candidates), 3)
        self.assertIn("https://community.akamai.steamstatic.com/economy/image/abcdef/96fx96f", candidates)
        self.assertIn("https://steamcommunity-a.akamaihd.net/economy/image/abcdef/96fx96f", candidates)
        self.assertIn("https://community.steamstatic.com/economy/image/abcdef/96fx96f", candidates)
        self.assertNotIn(url, candidates)

    def test_image_candidates_require_a_path(self):
        self.assertEqual(GameItemImageLoader.steam_image_candidates("https://example.com"), [])


class GameInventoryCardMetadataTests(unittest.TestCase):
    def test_stattrak_is_detected_from_item_metadata(self):
        item = parse_game_inventory(payload(), "cs2", "Bot A")[0]
        self.assertEqual(stattrak_label(item), "Нет")
        from dataclasses import replace
        self.assertEqual(stattrak_label(replace(item, tags=item.tags + ("StatTrak™",))), "Да")

    def test_rarity_has_a_distinct_border_color(self):
        self.assertEqual(rarity_color("Covert"), "#eb4b4b")
        self.assertNotEqual(rarity_color("Classified"), rarity_color("Mil-Spec Grade"))


if __name__ == "__main__":
    unittest.main()
