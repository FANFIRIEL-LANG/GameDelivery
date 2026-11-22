from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from core.delivery.listing_stock import InsufficientListingStock, ListingStockService
from core.delivery.stock_ledger import QuantityStockLedger, StockResource


class ListingStockServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tmp.name) / "listing-stock.sqlite3")
        self.database.initialize()
        self.ledger = QuantityStockLedger(self.database)
        self.service = ListingStockService(self.database)
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO gd_listings(listing_id,source_platform,title) VALUES('listing-1','manual','Test')"
            )
            db.execute(
                """
                INSERT INTO gd_config_versions
                    (config_version_id,listing_id,version_number,delivery_quantity_per_deal,
                     item_selectors_json,bot_pool_json)
                VALUES('config-1','listing-1',1,6,'{}','[]')
                """
            )
            db.execute(
                """
                INSERT INTO gd_deals
                    (deal_id,source_platform,external_deal_id,listing_id,config_version_id,payload_json)
                VALUES('deal-1','playerok','external-1','listing-1','config-1','{}')
                """
            )
            db.execute(
                "INSERT INTO gd_orders(order_id,deal_id,total_quantity) VALUES('order-1','deal-1',6)"
            )
            db.execute(
                """
                INSERT INTO gd_order_lines
                    (order_line_id,order_id,config_version_id,line_number,item_type,item_name,
                     item_selector_json,quantity)
                VALUES('line-1','order-1','config-1',1,'cs2','Test item','{}',6)
                """
            )
        with self.database.connect() as db:
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gd_stock_transfers'"
            ).fetchone())
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gd_stock_allocation_batches'"
            ).fetchone())
        self.a = StockResource("cs2", 730, "2", "Bot-A", "1001")
        self.b = StockResource("cs2", 730, "2", "Bot-B", "2002")

    def tearDown(self):
        self.tmp.cleanup()

    def test_listing_allocation_is_exact_deterministic_and_transfer_is_idempotent(self):
        self.ledger.observe(self.a, 5)
        self.ledger.observe(self.b, 5)
        rows = self.service.allocate_listing(
            request_id="allocation-request-1", listing_id="listing-1",
            config_version_id="config-1", units=8, resources=[self.b, self.a],
        )
        self.assertEqual(sum(row["units"] for row in rows), 8)
        self.assertEqual([(row["bot_name"], row["units"]) for row in rows],
                         [("Bot-A", 5), ("Bot-B", 3)])
        repeated_allocation = self.service.allocate_listing(
            request_id="allocation-request-1", listing_id="listing-1",
            config_version_id="config-1", units=8, resources=[self.a, self.b],
        )
        self.assertEqual(
            {row["allocation_id"] for row in repeated_allocation},
            {row["allocation_id"] for row in rows},
        )
        with self.assertRaises(ValueError):
            self.service.allocate_listing(
                request_id="allocation-request-1", listing_id="listing-1",
                config_version_id="config-1", units=7, resources=[self.a, self.b],
            )

        transferred = self.service.transfer_to_order(
            transfer_event_id="paid-event-1", listing_id="listing-1",
            config_version_id="config-1", order_line_id="line-1", units=6,
        )
        self.assertEqual(sum(row["units"] for row in transferred), 6)
        self.assertEqual([(row["bot_name"], row["units"]) for row in transferred],
                         [("Bot-A", 5), ("Bot-B", 1)])
        repeated = self.service.transfer_to_order(
            transfer_event_id="paid-event-1", listing_id="listing-1",
            config_version_id="config-1", order_line_id="line-1", units=6,
        )
        self.assertEqual(
            {row["allocation_id"] for row in repeated},
            {row["allocation_id"] for row in transferred},
        )
        with self.database.connect() as db:
            active_listing_units = db.execute(
                """
                SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations
                WHERE owner_type='listing' AND listing_id='listing-1'
                  AND config_version_id='config-1' AND state='active'
                """
            ).fetchone()[0]
            order_units = db.execute(
                """
                SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations
                WHERE owner_type='order' AND order_line_id='line-1' AND state='active'
                """
            ).fetchone()[0]
            transfer_events = db.execute(
                "SELECT COUNT(*) FROM gd_stock_transfers WHERE order_line_id='line-1'"
            ).fetchone()[0]
        self.assertEqual(active_listing_units, 2)
        self.assertEqual(order_units, 6)
        self.assertEqual(transfer_events, 1)
        self.assertEqual(self.ledger.free_units(self.a), 0)
        self.assertEqual(self.ledger.free_units(self.b), 0)

    def test_insufficient_listing_stock_leaves_no_partial_batch_or_allocations(self):
        self.ledger.observe(self.a, 3)
        self.ledger.observe(self.b, 2)
        with self.assertRaises(InsufficientListingStock) as caught:
            self.service.allocate_listing(
                request_id="not-enough", listing_id="listing-1",
                config_version_id="config-1", units=6, resources=[self.a, self.b],
            )
        self.assertEqual((caught.exception.required, caught.exception.available), (6, 5))
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_stock_allocation_batches"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_stock_allocations"
            ).fetchone()[0], 0)

    def test_transfer_cannot_exceed_order_line_quantity_or_reuse_event_id(self):
        self.ledger.observe(self.a, 10)
        self.service.allocate_listing(
            request_id="allocation-2", listing_id="listing-1",
            config_version_id="config-1", units=10, resources=[self.a],
        )
        self.service.transfer_to_order(
            transfer_event_id="paid-event-2", listing_id="listing-1",
            config_version_id="config-1", order_line_id="line-1", units=4,
        )
        with self.assertRaisesRegex(ValueError, "превышает количество"):
            self.service.transfer_to_order(
                transfer_event_id="paid-event-3", listing_id="listing-1",
                config_version_id="config-1", order_line_id="line-1", units=3,
            )
        with self.assertRaisesRegex(ValueError, "другими параметрами"):
            self.service.transfer_to_order(
                transfer_event_id="paid-event-2", listing_id="listing-1",
                config_version_id="config-1", order_line_id="line-1", units=5,
            )
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_stock_transfers WHERE order_line_id='line-1'"
            ).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
