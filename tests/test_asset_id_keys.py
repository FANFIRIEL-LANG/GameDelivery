from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.database.connection import connect_sqlite
from core.database.db import Database
from core.database.ledger_migrations import (
    ASSET_ID_BLOCKING_STATE_MIGRATION_ID,
    ASSET_ID_KEY_MIGRATION_ID,
    apply_delivery_ledger_migrations,
)
from core.delivery.ledger import DeliveryLedger
from games.asset_ids import asset_id_key, asset_id_with_key
from games.steam_cards.reservations import AssetReservationStore


class AssetIDKeyTests(unittest.TestCase):
    def test_helper_preserves_source_and_applies_only_ascii_positive_numeric_alias_rule(self):
        expected = {
            "123": ("123", "123"),
            "00123": ("00123", "123"),
            "0": ("0", "0"),
            "000": ("000", "000"),
            "00123a": ("00123a", "00123a"),
            "１２３": ("１２３", "１２３"),
        }
        for source, pair in expected.items():
            with self.subTest(source=source):
                self.assertEqual(asset_id_with_key(source), pair)
        for invalid in (None, True, 123, 1.5, "", "  "):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                asset_id_with_key(invalid)

    def test_sqlite_function_matches_python_for_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "keys.sqlite3")
            db.initialize()
            with db.connect() as connection:
                for source in ("123", "00123", "0", "000", "00123a", "１２３"):
                    self.assertEqual(connection.execute("SELECT gd_asset_id_key(?)", (source,)).fetchone()[0], asset_id_key(source))

    def test_store_preserves_source_and_conflicts_aliases_only_per_bot(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AssetReservationStore(Path(temp) / "reservations.sqlite3")
            self.assertTrue(store.reserve("owner-a", [("Bot-A", "00123")])["reserved"])
            self.assertTrue(store.reserve("owner-a", [("Bot-A", "00123")])["reserved"])
            self.assertTrue(store.reserve("owner-a", [("Bot-A", "123")])["reserved"])
            with store._connect() as db:
                self.assertEqual(db.execute(
                    "SELECT asset_id FROM steam_asset_reservations WHERE bot_name='Bot-A'"
                ).fetchone()[0], "00123")
            self.assertFalse(store.reserve("owner-b", [("Bot-A", "123")])["reserved"])
            self.assertTrue(store.reserve("owner-b", [("Bot-B", "123")])["reserved"])

    def test_alias_retry_mismatching_saved_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.sqlite3"
            store = AssetReservationStore(path)
            self.assertTrue(store.reserve("owner-a", [("Bot-A", "00123")])["reserved"])
            with store._connect() as connection:
                connection.execute(
                    "CREATE TABLE delivery_order_assets(order_id TEXT,bot_name TEXT,asset_id TEXT)"
                )
                connection.execute(
                    "INSERT INTO delivery_order_assets VALUES('owner-a','Bot-A','00123')"
                )
            with self.assertRaisesRegex(ValueError, "сохранённым manifest"):
                store.reserve("owner-a", [("Bot-A", "123")])
            self.assertEqual(store.list_for_order("owner-a")[0]["asset_id"], "00123")

    def test_zero_only_ids_remain_distinct_and_release_allows_reuse(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AssetReservationStore(Path(temp) / "reservations.sqlite3")
            self.assertTrue(store.reserve("owner-a", [("Bot-A", "0")])["reserved"])
            self.assertTrue(store.reserve("owner-b", [("Bot-A", "000")])["reserved"])
            self.assertEqual(store.release("owner-a"), 1)
            self.assertTrue(store.reserve("owner-c", [("Bot-A", "0")])["reserved"])

    def test_legacy_conflict_blocks_reservation_and_sql_insert_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "reservations.sqlite3")
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('unknown-owner','legacy','manual:unknown','cs2',730,'2','Bot-A','00123',1,'legacy_conflict')"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) VALUES('Bot-A','123','other')"
                    )
            store = AssetReservationStore(database.path)
            result = store.reserve("other", [("Bot-A", "123")])
            self.assertFalse(result["reserved"])

    def test_direct_sql_insert_and_update_cannot_bypass_unique_alias_key(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "direct.sqlite3")
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) VALUES('Bot-A','00123','one')"
                )
                self.assertEqual(connection.execute(
                    "SELECT asset_id,asset_id_key FROM steam_asset_reservations"
                ).fetchone()[:], ("00123", "123"))
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE steam_asset_reservations SET asset_id='123' WHERE order_id='one'")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE steam_asset_reservations SET asset_id='123' WHERE order_id='one'")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE gd_resource_reservations SET asset_id='123' "
                        "WHERE legacy_owner_id='steam_asset_reservations:one' AND state='active'"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) VALUES('Bot-A','123','two')"
                    )
                connection.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('r1','legacy','manual:r1','cs2',730,'2','Bot-B','asset-x',1,'active')"
                )
                connection.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('r2','legacy','manual:r2','cs2',730,'2','Bot-B','asset-y',1,'active')"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE gd_resource_reservations SET asset_id='asset-y' WHERE reservation_id='r1'")
                # A key spoof is recomputed from source ID by the update trigger.
                connection.execute("UPDATE gd_resource_reservations SET asset_id_key='asset-z' WHERE reservation_id='r1'")
                self.assertEqual(connection.execute(
                    "SELECT asset_id_key FROM gd_resource_reservations WHERE reservation_id='r1'"
                ).fetchone()[0], "asset-x")

    def test_legacy_conflict_cannot_duplicate_blocking_key_and_state_transitions_remain_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "blocking-states.sqlite3")
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('active-a','legacy','manual:a','cs2',730,'2','Bot-A','00123',1,'active')"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO gd_resource_reservations "
                        "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                        "VALUES('conflict-b','legacy','manual:b','cs2',730,'2','Bot-A','123',1,'legacy_conflict')"
                    )
                connection.execute(
                    "UPDATE gd_resource_reservations SET state='legacy_conflict' WHERE reservation_id='active-a'"
                )
                connection.execute(
                    "UPDATE gd_resource_reservations SET state='active' WHERE reservation_id='active-a'"
                )
                connection.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('released-b','legacy','manual:b','cs2',730,'2','Bot-A','123',1,'released')"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE gd_resource_reservations SET state='legacy_conflict' WHERE reservation_id='released-b'"
                    )
                self.assertEqual([tuple(row) for row in connection.execute(
                    "SELECT reservation_id,state FROM gd_resource_reservations WHERE bot_name='Bot-A' ORDER BY reservation_id"
                ).fetchall()], [("active-a", "active"), ("released-b", "released")])

    def test_blocking_state_guard_migration_upgrades_existing_asset_key_database(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "guard-upgrade.sqlite3")
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "DELETE FROM gd_schema_migrations WHERE migration_id=?",
                    (ASSET_ID_BLOCKING_STATE_MIGRATION_ID,),
                )
                connection.execute("DROP TRIGGER gd_asset_reservations_guard_insert")
                connection.execute("DROP TRIGGER gd_asset_reservations_guard_update")
                connection.commit()
                self.assertTrue(apply_delivery_ledger_migrations(connection))
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM gd_schema_migrations WHERE migration_id=?",
                    (ASSET_ID_BLOCKING_STATE_MIGRATION_ID,),
                ).fetchone())
                connection.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('active-a','legacy','manual:a','cs2',730,'2','Bot-A','00123',1,'active')"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO gd_resource_reservations "
                        "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                        "VALUES('conflict-b','legacy','manual:b','cs2',730,'2','Bot-A','123',1,'legacy_conflict')"
                    )

    def test_projection_failure_rolls_back_canonical_insert_and_release_paths_are_symmetric(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "projection.sqlite3")
            database.initialize()
            with database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('canonical-tx','legacy','manual:tx','steam_cards',753,'6','Bot-A','00123',1,'active')"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) VALUES('Bot-A','123','other-owner')"
                    )
                connection.rollback()
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM gd_resource_reservations WHERE reservation_id='canonical-tx'"
                ).fetchone())

                connection.execute(
                    "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) VALUES('Bot-A','00123','legacy-a')"
                )
                canonical_id = connection.execute(
                    "SELECT reservation_id FROM gd_resource_reservations WHERE state='active' AND bot_name='Bot-A'"
                ).fetchone()[0]
                connection.execute(
                    "UPDATE gd_resource_reservations SET state='released' WHERE reservation_id=?", (canonical_id,)
                )
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM steam_asset_reservations WHERE bot_name='Bot-A'"
                ).fetchone()[0], 0)
                connection.execute(
                    "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) VALUES('Bot-A','123','legacy-b')"
                )
                self.assertEqual(connection.execute(
                    "SELECT asset_id FROM steam_asset_reservations WHERE bot_name='Bot-A'"
                ).fetchone()[0], "123")

    def test_deleting_order_projection_releases_matching_canonical_owner_by_key(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "order-projection.sqlite3")
            ledger = DeliveryLedger(database)
            config = ledger.create_listing_config_version(
                listing_id="projection-listing", source_platform="manual", external_listing_id="projection-listing-ext",
                delivery_quantity_per_deal=1, item_selectors={"app_id": 753, "context_id": "6"}, bot_pool=["Bot-A"],
            )
            ledger.create_deal(
                deal_id="projection-deal", source_platform="manual", external_deal_id="projection-deal-ext",
                chat_id=None, listing_id=config["listing_id"], config_version_id=config["config_version_id"],
                platform_status="PAID",
            )
            ledger.create_order(order_id="projection-order", deal_id="projection-deal", total_quantity=1)
            line = ledger.add_order_line(
                order_line_id="projection-line", order_id="projection-order", line_number=1,
                item_type="steam_card", item_name="card", item_selector={}, quantity=1,
            )
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,order_line_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('projection-res','order',?,'steam_cards',753,'6','Bot-A','00123',1,'active')",
                    (line["order_line_id"],),
                )
                connection.execute(
                    "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) "
                    "VALUES('Bot-A','00123','projection-order')"
                )
                connection.execute(
                    "DELETE FROM steam_asset_reservations WHERE order_id='projection-order'"
                )
                self.assertEqual(connection.execute(
                    "SELECT state FROM gd_resource_reservations WHERE reservation_id='projection-res'"
                ).fetchone()[0], "released")

    def test_legacy_alias_conflict_migration_rolls_back_projection_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "legacy-conflict.sqlite3")
            database.initialize()
            with database.connect() as connection:
                connection.execute("DROP INDEX idx_gd_resource_active_asset_key")
                connection.execute("DROP INDEX idx_steam_reservation_asset_key")
                connection.execute("DROP TRIGGER steam_asset_reservations_set_key_insert")
                connection.execute("DROP TRIGGER steam_asset_reservations_set_key_update")
                connection.execute("DROP TRIGGER steam_asset_reservations_guard_insert")
                connection.execute("DROP TRIGGER steam_asset_reservations_to_ledger_insert")
                connection.execute("DELETE FROM gd_schema_migrations WHERE migration_id=?", (ASSET_ID_KEY_MIGRATION_ID,))
                connection.execute(
                    "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) VALUES('Bot-A','123','legacy-a')"
                )
                connection.execute(
                    "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) VALUES('Bot-A','00123','legacy-b')"
                )
                connection.commit()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "alias conflicts"):
                    apply_delivery_ledger_migrations(connection)
                self.assertEqual([tuple(row) for row in connection.execute(
                    "SELECT asset_id,order_id,asset_id_key FROM steam_asset_reservations ORDER BY asset_id"
                ).fetchall()], [("00123", "legacy-b", None), ("123", "legacy-a", None)])
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM gd_schema_migrations WHERE migration_id=?", (ASSET_ID_KEY_MIGRATION_ID,)
                ).fetchone())

    def test_key_migration_is_idempotent_and_blocks_canonical_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "idempotent.sqlite3")
            database.initialize()
            with database.connect() as connection:
                self.assertFalse(apply_delivery_ledger_migrations(connection))
                connection.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('first','legacy','manual:first','cs2',730,'2','Bot-A','000123',1,'active')"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO gd_resource_reservations "
                        "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                        "VALUES('second','legacy','manual:second','cs2',730,'2','Bot-A','123',1,'active')"
                    )

    def test_sqlite_writer_without_registered_key_function_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "no-function.sqlite3"
            Database(path).initialize()
            raw = sqlite3.connect(path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    raw.execute(
                        "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) VALUES('Bot-A','123','owner')"
                    )
                raw.rollback()
            finally:
                raw.close()

    def test_alias_conflict_migration_rolls_back_and_leaves_marker_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "migration-conflict.sqlite3"
            with connect_sqlite(path) as connection:
                with (
                    patch("core.database.ledger_migrations._apply_asset_id_key_migration", return_value=False),
                    patch("core.database.ledger_migrations._apply_asset_id_blocking_state_migration", return_value=False),
                ):
                    apply_delivery_ledger_migrations(connection)
                for rid, asset, owner in (("old-1", "123", "legacy:a"), ("old-2", "00123", "legacy:b")):
                    connection.execute(
                        "INSERT INTO gd_resource_reservations "
                        "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                        "VALUES(?,'legacy',?,'cs2',730,'2','Bot-A',?,1,'active')",
                        (rid, owner, asset),
                    )
                connection.commit()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "alias conflicts"):
                    apply_delivery_ledger_migrations(connection)
                self.assertEqual([tuple(row) for row in connection.execute(
                    "SELECT asset_id FROM gd_resource_reservations WHERE reservation_id IN ('old-1','old-2') ORDER BY reservation_id"
                ).fetchall()], [("123",), ("00123",)])
                self.assertNotIn("asset_id_key", {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(gd_resource_reservations)")
                })
                self.assertNotIn("asset_id_key", {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(steam_asset_reservations)")
                })
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM gd_schema_migrations WHERE migration_id=?", (ASSET_ID_KEY_MIGRATION_ID,)
                ).fetchone())

    def test_two_connections_racing_for_aliases_reserve_only_one_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "race.sqlite3"
            store_a, store_b = AssetReservationStore(path), AssetReservationStore(path)
            barrier = threading.Barrier(2)
            results = []

            def reserve(store, owner, asset):
                barrier.wait()
                results.append(store.reserve(owner, [("Bot-A", asset)]) ["reserved"])

            threads = [
                threading.Thread(target=reserve, args=(store_a, "owner-a", "00123")),
                threading.Thread(target=reserve, args=(store_b, "owner-b", "123")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(sorted(results), [False, True])


if __name__ == "__main__":
    unittest.main()
