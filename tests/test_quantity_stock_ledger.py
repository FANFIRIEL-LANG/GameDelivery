from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

from core.database.db import Database
from core.database.connection import connect_sqlite
from core.delivery.stock_ledger import (
    QuantityStockLedger, StockResource, is_fresh_stock_observation,
)
from core.delivery.stock_identity import StockItemIdentity
from core.delivery.recipes import SteamAssetSpec
from games.inventory import InventoryItem


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
            from core.database.ledger_migrations import QUANTITY_STOCK_IDENTITY_MIGRATION_ID
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM gd_schema_migrations WHERE migration_id=?",
                (QUANTITY_STOCK_IDENTITY_MIGRATION_ID,),
            ).fetchone())
            columns = {row["name"] for row in db.execute("PRAGMA table_info(gd_stock_observations)")}
            self.assertTrue({"identity_json", "is_tradable"}.issubset(columns))


    def test_existing_database_is_backed_up_before_quantity_migration(self):
        path = Path(self.tmp.name) / "legacy-stock.sqlite3"
        with connect_sqlite(path) as db:
            db.execute("CREATE TABLE legacy_marker(value TEXT)")
            db.execute("INSERT INTO legacy_marker(value) VALUES('preserve')")
        database = Database(path)
        database.initialize()
        from core.database.ledger_migrations import QUANTITY_STOCK_BACKUP_SUFFIX
        backup = Path(str(path) + QUANTITY_STOCK_BACKUP_SUFFIX)
        self.assertTrue(backup.exists())
        with connect_sqlite(backup) as db:
            self.assertEqual(db.execute("SELECT value FROM legacy_marker").fetchone()[0], "preserve")

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
            row = db.execute(
                "SELECT units,state FROM gd_stock_allocations WHERE allocation_id='allocation-1'"
            ).fetchone()
            self.assertEqual((row["units"], row["state"]), (6, "active"))

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



    def test_stock_observation_freshness_has_strict_five_minute_window(self):
        now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(is_fresh_stock_observation(
            (now - timedelta(minutes=5)).isoformat(), now=now,
        ))
        self.assertFalse(is_fresh_stock_observation(
            (now - timedelta(minutes=5, seconds=1)).isoformat(), now=now,
        ))
        self.assertFalse(is_fresh_stock_observation(
            (now + timedelta(seconds=1)).isoformat(), now=now,
        ))
        self.assertFalse(is_fresh_stock_observation("not-a-timestamp", now=now))

    def test_direct_allocation_rejects_old_usable_observation(self):
        self.ledger.observe(self.resource, 10)
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_stock_observations SET observed_at='2000-01-01T00:00:00+00:00' "
                "WHERE game_id='cs2' AND bot_name='Bot-A'"
            )
        with self.assertRaisesRegex(ValueError, "устарело"):
            self.ledger.allocate(
                allocation_id="allocation-old-observation",
                resource=self.resource,
                units=1,
                owner_type="listing",
                listing_id="listing-a",
            )
        with self.database.connect() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM gd_stock_allocations WHERE allocation_id='allocation-old-observation'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_old_usable_observation_is_not_reported_as_free_stock(self):
        self.ledger.observe(self.resource, 10)
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_stock_observations SET observed_at='2000-01-01T00:00:00+00:00' "
                "WHERE game_id='cs2' AND bot_name='Bot-A'"
            )
        self.assertEqual(self.ledger.free_units(self.resource), 0)

    def test_eligible_resources_ignores_old_usable_observations(self):
        identity = StockItemIdentity(
            "cs2", 730, "2", market_hash_name="AK-47 | Redline",
            name="AK-47 | Redline", item_type="Rifle", class_id="class-redline",
        )
        self.ledger.observe(
            self.resource, 10, identity=identity, is_tradable=True,
        )
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_stock_observations SET observed_at='2000-01-01T00:00:00+00:00' "
                "WHERE game_id='cs2' AND bot_name='Bot-A'"
            )
        spec = SteamAssetSpec(
            "cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",),
        )
        self.assertEqual(self.ledger.eligible_resources(spec), [])

    def test_stock_observation_identity_is_immutable_and_revision_is_monotonic(self):
        self.ledger.observe(self.resource, 10)
        with self.database.connect() as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "identity is immutable"):
                db.execute(
                    "UPDATE gd_stock_observations SET bot_name='Bot-B' WHERE game_id='cs2'"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "revision must increase"):
                db.execute(
                    "UPDATE gd_stock_observations SET revision=1 WHERE game_id='cs2'"
                )

    def test_stock_observation_rejects_noncanonical_asset_key(self):
        self.ledger.observe(self.resource, 10)
        with self.database.connect() as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "key mismatch"):
                db.execute(
                    "UPDATE gd_stock_observations SET asset_id_key='wrong-key' WHERE game_id='cs2'"
                )

    def test_consumed_units_stop_blocking_after_new_inventory_revision(self):
        self.ledger.observe(self.resource, 10)
        self.ledger.allocate(
            allocation_id="consumed-allocation", resource=self.resource, units=4,
            owner_type="listing", listing_id="listing-a",
        )
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_stock_allocations SET state='consumed',consumed_revision=1 "
                "WHERE allocation_id='consumed-allocation'"
            )
        self.assertEqual(self.ledger.free_units(self.resource), 6)
        observation = self.ledger.observe(self.resource, 6)
        self.assertEqual(observation["revision"], 2)
        self.assertEqual(observation["availability_state"], "usable")
        self.assertEqual(self.ledger.free_units(self.resource), 6)

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


    def test_concurrent_allocations_cannot_overbook_one_stack(self):
        self.ledger.observe(self.resource, 5)
        barrier = Barrier(2)

        def reserve(allocation_id: str) -> str:
            barrier.wait()
            try:
                self.ledger.allocate(
                    allocation_id=allocation_id, resource=self.resource, units=4,
                    owner_type="listing", listing_id="listing-a",
                )
                return "reserved"
            except sqlite3.IntegrityError:
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, ("concurrent-a", "concurrent-b")))
        self.assertCountEqual(outcomes, ["reserved", "blocked"])
        self.assertEqual(self.ledger.free_units(self.resource), 1)

    def test_observation_persists_exact_identity_and_tradability(self):
        identity = __import__(
            "core.delivery.stock_identity", fromlist=["StockItemIdentity"]
        ).StockItemIdentity(
            "cs2", 730, "2", market_hash_name="AK-47 | Redline (Field-Tested)",
            name="AK-47 | Redline", item_type="Classified Rifle", class_id="2001",
            exterior="Field-Tested", rarity="Classified",
        )
        row = self.ledger.observe(
            self.resource, 100, identity=identity, is_tradable=True
        )
        self.assertEqual(row["is_tradable"], 1)
        self.assertIn('"class_id":"2001"', row["identity_json"])
        spec = SteamAssetSpec(
            game_id="cs2", app_id=730, context_id="2",
            selector={"class_id": "2001", "market_hash_name": "AK-47 | Redline (Field-Tested)"},
        )
        self.assertEqual(self.ledger.eligible_resources(spec), [self.resource])

    def test_selector_matcher_rejects_unknown_false_and_missing_tradability(self):
        from core.delivery.stock_identity import StockItemIdentity
        base_identity = StockItemIdentity(
            "cs2", 730, "2", market_hash_name="AK-47 | Redline (Field-Tested)",
            name="AK-47 | Redline", item_type="Classified Rifle", class_id="2001",
            exterior="Field-Tested", rarity="Classified",
        )
        spec = SteamAssetSpec(
            "cs2", 730, "2", {"class_id": "2001"}, allowed_bot_pool=("Bot-A",)
        )
        for asset_id, flag in (("1101", False), ("1102", None)):
            resource = StockResource("cs2", 730, "2", "Bot-A", asset_id)
            self.ledger.observe(resource, 3, identity=base_identity, is_tradable=flag)
        self.assertEqual(self.ledger.eligible_resources(spec), [])

        tradable_resource = StockResource("cs2", 730, "2", "Bot-A", "1103")
        self.ledger.observe(tradable_resource, 3, identity=base_identity, is_tradable=True)
        self.assertEqual(self.ledger.eligible_resources(spec), [tradable_resource])
        blocked_spec = SteamAssetSpec(
            "cs2", 730, "2", {"class_id": "2001"}, allowed_bot_pool=("Bot-B",)
        )
        self.assertEqual(self.ledger.eligible_resources(blocked_spec), [])

    def test_identity_scope_mismatch_is_rejected_on_observe(self):
        from core.delivery.stock_identity import StockItemIdentity
        wrong_scope = StockItemIdentity(
            "dota2", 570, "2", class_id="2001"
        )
        with self.assertRaisesRegex(ValueError, "scope"):
            self.ledger.observe(
                self.resource, 1, identity=wrong_scope, is_tradable=True
            )

    def test_complete_snapshot_invalidates_missing_assets_atomically(self):
        item = InventoryItem(
            game_id="cs2", bot_name="Bot-A", asset_id="123456",
            class_id="2001", instance_id="0", name="AK-47 | Redline",
            market_hash_name="AK-47 | Redline (Field-Tested)",
            item_type="Classified Rifle", is_tradable=True, amount=10,
        )
        result = self.ledger.observe_snapshot("cs2", "Bot-A", [item])
        self.assertEqual(result["observed_count"], 1)
        self.ledger.allocate(
            allocation_id="snapshot-allocation", resource=self.resource, units=4,
            owner_type="listing", listing_id="listing-a",
        )
        empty = self.ledger.observe_snapshot("cs2", "Bot-A", [])
        self.assertEqual(empty["observed_count"], 0)
        self.assertEqual(empty["unavailable_count"], 1)
        with self.database.connect() as db:
            observation = db.execute(
                "SELECT revision,availability_state,is_tradable,identity_json "
                "FROM gd_stock_observations WHERE observation_id=?",
                (self.ledger._observation_id(self.resource),),
            ).fetchone()
            allocation = db.execute(
                "SELECT units,state FROM gd_stock_allocations WHERE allocation_id='snapshot-allocation'"
            ).fetchone()
        self.assertEqual((observation["revision"], observation["availability_state"]), (2, "unavailable"))
        self.assertEqual(observation["is_tradable"], 1)
        self.assertIsNotNone(observation["identity_json"])
        self.assertEqual((allocation["units"], allocation["state"]), (4, "active"))
        self.assertEqual(self.ledger.free_units(self.resource), 0)

    def test_complete_snapshot_rejects_wrong_bot_and_duplicate_assets(self):
        item = InventoryItem(
            "cs2", "Bot-A", "123456", "2001", "0", "Item", amount=1,
            is_tradable=True,
        )
        with self.assertRaisesRegex(ValueError, "другой игры или бота"):
            self.ledger.observe_snapshot("cs2", "Bot-B", [item])
        with self.assertRaisesRegex(ValueError, "повторяющийся"):
            self.ledger.observe_snapshot("cs2", "Bot-A", [item, item])

    def test_resource_rejects_non_string_asset_ids(self):
        for invalid in (123456, None, "", "   "):
            with self.assertRaises(ValueError):
                StockResource("cs2", 730, "2", "Bot-A", invalid).normalized()

    def test_allocation_rows_cannot_be_physically_deleted(self):
        self.ledger.observe(self.resource, 10)
        self.ledger.allocate(
            allocation_id="protected-allocation", resource=self.resource, units=3,
            owner_type="listing", listing_id="listing-a",
        )
        with self.database.connect() as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot be deleted"):
                db.execute(
                    "DELETE FROM gd_stock_allocations WHERE allocation_id='protected-allocation'"
                )
        self.assertEqual(self.ledger.free_units(self.resource), 7)

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

    def test_quantity_stock_migration_rolls_back_partial_ddl_on_trigger_conflict(self):
        from core.database.ledger_migrations import (
            QUANTITY_STOCK_MIGRATION_ID,
            _apply_quantity_stock_migration,
        )
        from games.asset_ids import asset_id_key

        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.create_function("gd_asset_id_key", 1, asset_id_key, deterministic=True)
            connection.execute(
                "CREATE TABLE gd_schema_migrations(migration_id TEXT PRIMARY KEY)"
            )
            connection.execute(
                "CREATE TABLE gd_resource_reservations("
                "state TEXT,game_id TEXT,app_id INTEGER,context_id TEXT,"
                "bot_name TEXT,asset_id_key TEXT)"
            )
            connection.execute(
                "CREATE TABLE steam_asset_reservations(bot_name TEXT,asset_id TEXT)"
            )
            connection.execute("CREATE TABLE migration_trigger_collision(id INTEGER)")
            # SQLite trigger names are database-global. This forces a failure
            # after the quantity tables, indexes and allocation guards were created.
            connection.execute(
                "CREATE TRIGGER gd_stock_observations_identity_immutable "
                "BEFORE UPDATE ON migration_trigger_collision BEGIN SELECT 1; END"
            )

            with self.assertRaisesRegex(sqlite3.OperationalError, "already exists"):
                _apply_quantity_stock_migration(connection)

            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            triggers = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            self.assertNotIn("gd_stock_observations", tables)
            self.assertNotIn("gd_stock_allocations", tables)
            self.assertNotIn(QUANTITY_STOCK_MIGRATION_ID, {
                row[0] for row in connection.execute(
                    "SELECT migration_id FROM gd_schema_migrations"
                )
            })
            self.assertEqual(
                triggers,
                {"gd_stock_observations_identity_immutable"},
            )
        finally:
            connection.close()
