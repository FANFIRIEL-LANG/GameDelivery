from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from integrations.playerok.listing_config import PlayerokListingConfigService


class SteamCardListingLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tmp.name) / "steam-card-limit.sqlite3")
        self.database.initialize()
        self.service = PlayerokListingConfigService(self.database)

    def tearDown(self):
        self.tmp.cleanup()

    def test_steam_card_config_saves_listing_total_separately_from_order_quantity(self):
        result = self.service.save(
            external_listing_id="playerok-steam-card-listing",
            title="Steam card sets",
            url="https://playerok.com/item/test",
            mode="steam_cards",
            quantity=2,
            listing_quantity=10,
            bot_pool=["Bot-A"],
        )
        self.assertEqual(result["listing_quantity"], 10)
        with self.database.connect() as db:
            config = db.execute(
                "SELECT delivery_quantity_per_deal FROM gd_config_versions WHERE config_version_id=?",
                (result["config_version_id"],),
            ).fetchone()
            limit = db.execute(
                "SELECT total_sets FROM gd_steam_card_listing_limits WHERE listing_id=?",
                (result["listing_id"],),
            ).fetchone()
        self.assertEqual(int(config["delivery_quantity_per_deal"]), 2)
        self.assertEqual(int(limit["total_sets"]), 10)

    def test_listing_total_cannot_be_reduced_below_already_claimed_sets(self):
        result = self.service.save(
            external_listing_id="playerok-steam-card-listing-2",
            title="Steam card sets",
            url="https://playerok.com/item/test-2",
            mode="steam_cards",
            quantity=1,
            listing_quantity=10,
            bot_pool=["Bot-A"],
        )
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO gd_deals(deal_id,source_platform,external_deal_id,listing_id,"
                "config_version_id,payload_json) VALUES(?,?,?,?,?,'{}')",
                ("deal-1", "playerok", "external-deal-1", result["listing_id"], result["config_version_id"]),
            )
            db.execute(
                "INSERT INTO gd_orders(order_id,deal_id,total_quantity) VALUES('order-1','deal-1',1)"
            )
            db.execute(
                "INSERT INTO gd_steam_card_listing_claims(order_id,listing_id,config_version_id,sets,state) "
                "VALUES('order-1',?,?,4,'reserved')",
                (result["listing_id"], result["config_version_id"]),
            )
        with self.assertRaisesRegex(ValueError, "уже занято 4 наборов"):
            self.service.set_steam_card_listing_limit(listing_id=result["listing_id"], total_sets=3)


if __name__ == "__main__":
    unittest.main()
