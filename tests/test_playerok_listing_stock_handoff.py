from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.order_pipeline import DeliveryOrderPipeline
from core.delivery.recipes import RecipeItem, SteamAssetSpec
from core.delivery.stock_identity import StockItemIdentity
from core.delivery.stock_ledger import QuantityStockLedger, StockResource
from integrations.playerok.listing_config import PlayerokListingConfigService
from integrations.playerok.sync import PlayerokDealSyncService
from tests.test_playerok_sync import FakePlayerok, deal


class PlayerokListingStockHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "playerok-listing-stock.sqlite3")
        self.database.initialize()
        self.ledger = DeliveryLedger(self.database)
        self.syncer = PlayerokDealSyncService(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_paid_playerok_recipe_transfers_listing_stock_through_order_preflight_atomically(self):
        recipe_item = RecipeItem(
            "cs2-redline", 1, "steam_asset", 1, 2, True, "shared-asf-stock",
            "steam_trade_offer",
            SteamAssetSpec(
                "cs2", 730, "2",
                {"market_hash_name": "AK-47 | Redline"},
                (),
            ),
        )
        config = self.ledger.create_recipe_config_version(
            listing_id="typed-listing-live",
            source_platform="playerok",
            external_listing_id="listing-live",
            recipe_items=[recipe_item],
            title="Typed CS2 listing",
        )

        stock = QuantityStockLedger(self.database)
        resource = StockResource("cs2", 730, "2", "Bot-A", "stock-asset-1")
        stock.observe(
            resource,
            6,
            identity=StockItemIdentity(
                "cs2", 730, "2",
                market_hash_name="AK-47 | Redline",
                name="AK-47 | Redline",
                item_type="Rifle",
                class_id="redline-class",
            ),
            is_tradable=True,
        )
        allocated = PlayerokListingConfigService(self.database).allocate_listing_stock(
            config_version_id=config["config_version_id"],
            listing_quantity=3,
        )
        self.assertEqual(allocated["reserved_units"], 6)

        adapter = FakePlayerok(
            baseline=[],
            active=[deal("stock-transfer-paid", "listing-live")],
        )
        self.syncer.sync(adapter)  # Complete the historical baseline.
        self.syncer.sync(adapter)  # Persist the first post-baseline PAID deal and order.
        order_id = self.syncer._stable(
            "playerok-order", "seller-account-1\x1fstock-transfer-paid"
        )

        pipeline = DeliveryOrderPipeline(self.database)
        prepared = pipeline.prepare_recipe_order(order_id=order_id, snapshots={})
        self.assertEqual(prepared.status, "reserved")

        with self.database.connect() as db:
            line = db.execute(
                "SELECT order_line_id FROM gd_order_lines WHERE order_id=?",
                (order_id,),
            ).fetchone()
            self.assertIsNotNone(line)
            transfer = db.execute(
                "SELECT units,listing_id,config_version_id,order_line_id "
                "FROM gd_stock_transfers WHERE order_line_id=?",
                (line["order_line_id"],),
            ).fetchone()
            self.assertIsNotNone(transfer)
            self.assertEqual(
                (transfer["units"], transfer["listing_id"], transfer["config_version_id"]),
                (2, config["listing_id"], config["config_version_id"]),
            )
            self.assertEqual(transfer["order_line_id"], line["order_line_id"])

            allocations = db.execute(
                "SELECT owner_type,state,units FROM gd_stock_allocations "
                "WHERE config_version_id=? ORDER BY owner_type",
                (config["config_version_id"],),
            ).fetchall()
            self.assertEqual(
                sorted((row["owner_type"], row["state"], row["units"]) for row in allocations),
                [("listing", "active", 4), ("listing", "released", 6), ("order", "active", 2)],
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM gd_stock_transfers WHERE order_line_id=?",
                    (line["order_line_id"],),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM gd_delivery_legs l "
                    "JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id "
                    "WHERE ol.order_id=?",
                    (order_id,),
                ).fetchone()[0],
                1,
            )

        # Re-running preparation for the already-reserved order must not transfer
        # the same listing units a second time or append another transfer event.
        repeated = pipeline.prepare_recipe_order(order_id=order_id, snapshots={})
        self.assertEqual(repeated.status, "reserved")
        with self.database.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM gd_stock_transfers WHERE order_line_id=("
                    "SELECT order_line_id FROM gd_order_lines WHERE order_id=?)",
                    (order_id,),
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
