from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.reservation_service import DeliveryReservationService, InventorySnapshot
from games.asset_ids import validate_asset_id
from games.inventory import InventoryItem, parse_game_inventory
from games.steam_cards.asf_inventory import parse_bot_inventory
from games.steam_cards.inventory_views import build_inventory_view, select_inventory_instances
from games.steam_cards.owned_selection import select_owned_complete_sets
from games.steam_cards.reservations import AssetReservationStore
from games.steam_cards.selection import CardCopy, find_available_sets
from games.steam_cards.shared_sets import find_shared_game_sets
from games.steam_cards.snapshot import InventorySnapshotStore


INVALID_ASSET_IDS = (None, True, 123, 12.5, "", " \t\n")


class AssetIDValidationTests(unittest.TestCase):
    def test_validator_rejects_non_string_and_empty_values(self):
        for value in INVALID_ASSET_IDS:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_asset_id(value)

    def test_validator_preserves_valid_steam_and_cs2_ids(self):
        self.assertEqual(validate_asset_id("  000123456789  "), "000123456789")
        self.assertEqual(validate_asset_id("  000987654321  "), "000987654321")

    @staticmethod
    def _steam_payload(asset_id):
        return {"Success": True, "Result": {"Bot-A": {
            "Assets": [{"assetid": asset_id, "classid": "class-1"}],
            "Descriptions": [{
                "classid": "class-1", "instanceid": "0",
                "name": "Test item", "type": "Weapon", "tradable": True,
            }],
        }}}

    def test_steam_asf_parser_validates_raw_asset_id_before_string_conversion(self):
        for value in INVALID_ASSET_IDS:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_bot_inventory(self._steam_payload(value), "Bot-A", "Test game")
        parsed = parse_bot_inventory(
            self._steam_payload("  000123456789  "), "Bot-A", "Test game"
        )
        self.assertEqual(parsed[0].asset_id, "000123456789")

    def test_general_game_parser_validates_raw_asset_id(self):
        # Game inventory crosses the ASF JSON boundary, where UInt64 AssetID
        # values may be represented as JSON integers. Other invalid types
        # remain rejected; internal InventoryItem construction stays strict.
        invalid_asf_values = (None, True, 12.5, "", " \\t\\n")
        for value in invalid_asf_values:
            payload = {"Success": True, "Result": {"Bot-A": {
                "Assets": [{"assetid": value, "classid": "class-1"}],
                "Descriptions": [{"classid": "class-1", "instanceid": "0", "name": "Item"}],
            }}}
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_game_inventory(payload, "cs2", "Bot-A")

        payload = {"Success": True, "Result": {"Bot-A": {
            "Assets": [{"assetid": 123, "classid": "class-1"}],
            "Descriptions": [{"classid": "class-1", "instanceid": "0", "name": "Item"}],
        }}}
        parsed = parse_game_inventory(payload, "cs2", "Bot-A")
        self.assertEqual(parsed[0].asset_id, "123")

        payload["Result"]["Bot-A"]["Assets"][0]["assetid"] = "  000987654321  "
        parsed = parse_game_inventory(payload, "cs2", "Bot-A")
        self.assertEqual(parsed[0].asset_id, "000987654321")
        with self.assertRaises(ValueError):
            InventoryItem("cs2", "Bot-A", 123, "class", "0", "Item")

    def test_card_model_and_selectors_reject_invalid_source_ids(self):
        for value in INVALID_ASSET_IDS:
            with self.subTest(value=value), self.assertRaises(ValueError):
                CardCopy(value, "Game", "Card")

        for value in INVALID_ASSET_IDS:
            malformed_card = SimpleNamespace(
                asset_id=value, game_name="Game", card_name="Card", is_trading_card=True,
                is_foil=False, is_tradable=True, market_hash_name="Card",
            )
            for selector in (
                lambda: find_available_sets([malformed_card]),
                lambda: find_shared_game_sets({"Bot-A": [malformed_card]}),
                lambda: select_owned_complete_sets({"Bot-A": [malformed_card]}, "Game", 1),
                lambda: build_inventory_view({"Bot-A": [malformed_card]}, ["Bot-A"], mode="shared"),
                lambda: select_inventory_instances([("Bot-A", value)], 1),
            ):
                with self.subTest(value=value, selector=selector), self.assertRaises(ValueError):
                    selector()

    def test_snapshot_store_rejects_before_persisting_invalid_id(self):
        with TemporaryDirectory() as directory:
            store = InventorySnapshotStore(Path(directory) / "snapshot.sqlite3")
            for value in INVALID_ASSET_IDS:
                malformed = SimpleNamespace(
                    asset_id=value, game_name="Game", card_name="Card",
                    is_trading_card=True, is_foil=False, is_tradable=True,
                    icon_url="", icon_url_large="", market_hash_name="",
                    item_quality="", game_app_id=None,
                )
                with self.subTest(value=value), self.assertRaises(ValueError):
                    store.replace([malformed], "Bot-A")
            self.assertEqual(store.load("Bot-A"), [])
            store.replace([CardCopy("  000123456789  ", "Game", "Card")], "Bot-A")
            self.assertEqual(store.load("Bot-A")[0].asset_id, "000123456789")

    def test_legacy_reservation_store_rejects_invalid_id_before_write(self):
        with TemporaryDirectory() as directory:
            store = AssetReservationStore(Path(directory) / "reservations.sqlite3")
            for value in INVALID_ASSET_IDS:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    store.reserve("order", [("Bot-A", value)])
            self.assertEqual(store.list_reserved_assets(), ())

    def test_cs2_selector_rejects_invalid_raw_id_and_preserves_valid_id(self):
        service = object.__new__(DeliveryReservationService)
        service.max_snapshot_age = timedelta(minutes=5)
        now = datetime.now(timezone.utc)

        def snapshot(asset_id):
            item = SimpleNamespace(
                asset_id=asset_id, bot_name="Bot-A", game_id="cs2", appid=730,
                context_id="2", is_tradable=True, amount=1,
                market_hash_name="Test item", name="Test item",
            )
            return InventorySnapshot("Bot-A", "cs2", 730, "2", now, (item,), True)

        selectors = {"game_id": "cs2", "app_id": 730, "context_id": "2", "market_hash_name": "Test item"}
        for value in INVALID_ASSET_IDS:
            with self.subTest(value=value), self.assertRaises(ValueError):
                service._select_assets(selectors, 1, {"Bot-A": snapshot(value)}, ("Bot-A",), now)
        result = service._select_assets(
            selectors, 1, {"Bot-A": snapshot("000987654321")}, ("Bot-A",), now
        )
        self.assertEqual(result, (("Bot-A", "000987654321", 730, "2"),))

    def test_reservation_boundary_and_direct_ledger_adapter_reject_invalid_ids(self):
        for value in INVALID_ASSET_IDS:
            with self.subTest(value=value), self.assertRaises(ValueError):
                DeliveryReservationService._validated_asset_id(value)

        with TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Database(Path(directory) / "ledger.sqlite3"))
            for value in INVALID_ASSET_IDS:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    ledger.add_delivery_leg(
                        leg_id="leg", order_line_id="line", bot_name="Bot-A",
                        app_id=730, context_id="2", asset_ids=[value],
                    )
            with ledger.database.connect() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM gd_delivery_legs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
