from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from core.delivery.stock_ledger import QuantityStockLedger, StockResource


class QuantityStockLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "stock.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()
        self.ledger = QuantityStockLedger(self.database)
        self.resource = StockResource("cs2", 730, "2", "Bot-A", "123456")
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO gd_listings(listing_id,source_platform,title) VALUES('listing-a','manual','A')"
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_creates_quantity_tables_and_pre_migration_backup(self):
        from core.database.ledger_migrations import (
            QUANTITY_STOCK_BACKUP_SUFFIX, QUANTITY_STOCK_MIGRATION_ID,
        )
        self.assertTrue(Path(str(self.path) + QUANTITY_STOCK_BACKUP_SUFFIX).exists())
        with self.database.connect() as db:
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM gd_schema_migrations WHERE migration_id=?",
                (QUANTITY_STOCK_MIGRATION_ID,),
            ).fetchone())
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gd_stock_observations'"
            ).fetchone())
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gd_stock_allocations'"
            ).fetchone())

    def test_multiple_allocations_share_one_stack_without_exceeding_amount(self):
        self.ledger.observe(self.resource, 10)
        self.ledger.allocate(
            allocation_id="allocation-1", resource=self.resource, units=4,
            owner_type="listing", listing_id="listing-a",
        )
        self.ledger.allocate(
            allocation_id="allocation-2", resource=self.resource, units=3,
            owner_type="listing", listing_id="listing-a",
        )
        self.assertEqual(self.ledger.free_units(self.resource), 3)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "exceeds observed amount"):
            self.ledger.allocate(
                allocation_id="allocation-3", resource=self.resource, units=4,
                owner_type="listing", listing_id="listing-a",
            )
        self.assertEqual(self.ledger.free_units(self.resource), 3)

    def test_update_cannot_increase_aggregate_allocations_over_observed_amount(self):
        self.ledger.observe(self.resource, 10)
        self.ledger.allocate(
            allocation_id="allocation-1", resource=self.resource, units=4,
            owner_type="listing", listing_id="listing-a",
        )
        self.ledger.allocate(
            allocation_id="allocation-2", resource=self.resource, units=3,
            owner_type="listing", listing_id="listing-a",
        )
        with self.database.connect() as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "exceeds observed amount"):
                db.execute(
                    "UPDATE gd_stock_allocations SET units=7 WHERE allocation_id='allocation-2'"
                )
        self.assertEqual(self.ledger.free_units(self.resource), 3)

    def test_lower_inventory_observation_marks_conflict_without_dropping_allocations(self):
        self.ledger.observe(self.resource, 10)
        self.ledger.allocate(
            allocation_id="allocation-1", resource=self.resource, units=6,
            owner_type="listing", listing_id="listing-a",
        )
        observation = self.ledger.observe(self.resource, 5)
        self.assertEqual(observation["availability_state"], "conflict")
        self.assertEqual(self.ledger.free_units(self.resource), 0)
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT units,state FROM gd_stock_allocations WHERE allocation_id='allocation-1'"
            ).fetchone(), (6, "active"))

    def test_stale_observation_fails_closed(self):
        self.ledger.observe(self.resource, 10)
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_stock_observations SET availability_state='stale' WHERE game_id='cs2'"
            )
        with self.assertRaisesRegex(ValueError, "недоступно"):
            self.ledger.allocate(
                allocation_id="allocation-stale", resource=self.resource, units=1,
                owner_type="listing", listing_id="listing-a",
            )
        self.assertEqual(self.ledger.free_units(self.resource), 0)

    def test_legacy_reservation_blocks_new_quantity_stock(self):
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO gd_resource_reservations
                    (reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,
                     bot_name,asset_id,units,state)
                VALUES('old-reservation','legacy','legacy:test','cs2',730,'2',
                       'Bot-A','123456',1,'active')
                """
            )
        observation = self.ledger.observe(self.resource, 10)
        self.assertEqual(observation["availability_state"], "conflict")
        with self.assertRaisesRegex(ValueError, "недоступно"):
            self.ledger.allocate(
                allocation_id="blocked-allocation", resource=self.resource, units=1,
                owner_type="listing", listing_id="listing-a",
            )

    def test_invalid_amount_and_units_are_rejected(self):
        for amount in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                self.ledger.observe(self.resource, amount)
        self.ledger.observe(self.resource, 10)
        for units in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                self.ledger.allocate(
                    allocation_id=f"bad-{units}", resource=self.resource, units=units,
                    owner_type="listing", listing_id="listing-a",
                )


if __name__ == "__main__":
    unittest.main()
