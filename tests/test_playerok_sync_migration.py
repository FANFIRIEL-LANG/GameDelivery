from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.database.connection import connect_sqlite
from core.database.ledger_migrations import apply_delivery_ledger_migrations
from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from games.steam_cards.reservations import AssetReservationStore


def _legacy_schema(db: sqlite3.Connection, *, unique_external: bool = True) -> None:
    db.executescript("""
        CREATE TABLE gd_schema_migrations(migration_id TEXT PRIMARY KEY);
        INSERT INTO gd_schema_migrations VALUES('2026-11-delivery-ledger-v1');
        CREATE TABLE gd_listings(listing_id TEXT PRIMARY KEY);
        CREATE TABLE gd_config_versions(config_version_id TEXT PRIMARY KEY);
        CREATE TABLE gd_deals(
            deal_id TEXT PRIMARY KEY, source_platform TEXT NOT NULL,
            external_deal_id TEXT NOT NULL, chat_id TEXT,
            listing_id TEXT NOT NULL REFERENCES gd_listings(listing_id),
            config_version_id TEXT NOT NULL REFERENCES gd_config_versions(config_version_id),
            platform_status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
            playerok_account_id TEXT, is_historical INTEGER NOT NULL DEFAULT 0
            %s
        );
        CREATE TABLE gd_orders(
            order_id TEXT PRIMARY KEY,
            deal_id TEXT NOT NULL UNIQUE REFERENCES gd_deals(deal_id)
        );
        CREATE TABLE gd_order_lines(
            order_line_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL REFERENCES gd_orders(order_id)
        );
        CREATE TABLE gd_legacy_order_map(
            legacy_table TEXT NOT NULL,
            legacy_order_id TEXT NOT NULL,
            order_id TEXT NOT NULL REFERENCES gd_orders(order_id),
            PRIMARY KEY(legacy_table,legacy_order_id),
            UNIQUE(order_id)
        );
        CREATE TABLE gd_resource_reservations(
            reservation_id TEXT PRIMARY KEY,
            owner_type TEXT NOT NULL CHECK(owner_type IN ('listing','order','legacy')),
            listing_id TEXT,
            config_version_id TEXT,
            order_line_id TEXT,
            legacy_owner_id TEXT,
            game_id TEXT NOT NULL DEFAULT '',
            app_id INTEGER NOT NULL DEFAULT 0,
            context_id TEXT NOT NULL DEFAULT '',
            bot_name TEXT NOT NULL COLLATE NOCASE,
            asset_id TEXT NOT NULL,
            units INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'active',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX idx_gd_resource_active_global
            ON gd_resource_reservations(bot_name COLLATE NOCASE,asset_id) WHERE state='active';
    """ % (", UNIQUE(source_platform,external_deal_id)" if unique_external else ""))


class PlayerokSyncMigrationTests(unittest.TestCase):
    def test_partial_legacy_listing_status_guard_is_schema_aware_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy-listing-status.sqlite3"
            with connect_sqlite(path) as db:
                _legacy_schema(db)
                apply_delivery_ledger_migrations(db)
                insert_trigger = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='gd_stock_allocations_listing_enabled_insert'"
                ).fetchone()
                update_trigger = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='gd_stock_allocations_listing_enabled_update'"
                ).fetchone()
                self.assertIsNotNone(insert_trigger)
                self.assertIsNotNone(update_trigger)
                self.assertIn("status column unavailable", insert_trigger[0])
                self.assertIn("status column unavailable", update_trigger[0])
                self.assertNotIn("l.status", insert_trigger[0])
                self.assertNotIn("l.status", update_trigger[0])


    def test_playerok_sync_migration_preserves_trigger_referencing_deals(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trigger-migration.sqlite3"
            with connect_sqlite(path) as db:
                _legacy_schema(db)
                db.execute("INSERT INTO gd_listings VALUES('listing-1')")
                db.execute("INSERT INTO gd_config_versions VALUES('config-1')")
                db.execute("""
                    INSERT INTO gd_deals
                    (deal_id,source_platform,external_deal_id,listing_id,config_version_id,
                     platform_status,payload_json,created_at,playerok_account_id,is_historical)
                    VALUES('deal-1','playerok','external-1','listing-1','config-1',
                           'PAID','{}','now','seller-1',0)
                """)
                db.execute("INSERT INTO gd_orders VALUES('order-1','deal-1')")
                db.execute("""
                    CREATE TRIGGER gd_orders_playerok_delivered
                    AFTER UPDATE OF deal_id ON gd_orders
                    BEGIN
                        SELECT deal_id FROM gd_deals WHERE deal_id=NEW.deal_id;
                    END
                """)
                db.commit()

                self.assertTrue(apply_delivery_ledger_migrations(db))
                trigger = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='gd_orders_playerok_delivered'"
                ).fetchone()
                self.assertIsNotNone(trigger)
                self.assertIn("FROM gd_deals", trigger[0])
                db.execute("UPDATE gd_orders SET deal_id='deal-1' WHERE order_id='order-1'")
                self.assertEqual(
                    db.execute("SELECT deal_id FROM gd_deals").fetchone()[0], "deal-1"
                )

    def test_v1_upgrade_preserves_rows_and_enables_account_scoped_unique_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "migration.sqlite3"
            with connect_sqlite(path) as db:
                _legacy_schema(db)
                db.execute("INSERT INTO gd_listings VALUES('listing-1')")
                db.execute("INSERT INTO gd_config_versions VALUES('config-1')")
                db.execute("""
                    CREATE TABLE playerok_sync_state(
                        account_user_id TEXT PRIMARY KEY,
                        baseline_completed INTEGER NOT NULL DEFAULT 0
                    )
                """)
                db.execute("INSERT INTO playerok_sync_state VALUES('seller-1',1)")
                db.execute("""
                    INSERT INTO gd_deals VALUES
                    ('deal-internal-1','playerok','same-external',NULL,'listing-1','config-1','PAID','{}','now','seller-1',0),
                    ('deal-internal-2','playerok','orphan',NULL,'listing-1','config-1','PAID','{}','now',NULL,0)
                """)
                db.execute("INSERT INTO gd_orders VALUES('order-1','deal-internal-1')")
                db.execute("INSERT INTO gd_order_lines VALUES('line-1','order-1')")
                db.commit()
                self.assertTrue(apply_delivery_ledger_migrations(db))
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                rows = db.execute(
                    "SELECT external_deal_id,playerok_account_id,is_historical,listing_resolution_status "
                    "FROM gd_deals ORDER BY external_deal_id"
                ).fetchall()
                self.assertEqual([tuple(row) for row in rows], [
                    ("orphan", None, 1, "resolved"),
                    ("same-external", "seller-1", 0, "resolved"),
                ])
                self.assertEqual(db.execute("SELECT deal_id FROM gd_orders").fetchone()[0], "deal-internal-1")
                line = db.execute(
                    "SELECT order_id,config_version_id FROM gd_order_lines"
                ).fetchone()
                self.assertEqual(tuple(line), ("order-1", "config-1"))
                self.assertFalse(apply_delivery_ledger_migrations(db))
                db.execute("INSERT INTO gd_listings(listing_id) VALUES('listing-2')")
                db.execute("INSERT INTO gd_config_versions(config_version_id) VALUES('config-2')")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        "INSERT INTO gd_order_lines(order_line_id,order_id,config_version_id) "
                        "VALUES('wrong-version-line','order-1','config-2')"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        "UPDATE gd_order_lines SET config_version_id='config-2' WHERE order_line_id='line-1'"
                    )
                foreign_keys = db.execute("PRAGMA foreign_key_list(gd_order_lines)").fetchall()
                self.assertTrue(any(
                    row[2] == "gd_config_versions" and row[3] == "config_version_id"
                    for row in foreign_keys
                ))
                db.execute("""
                    INSERT INTO gd_deals
                    (deal_id,source_platform,external_deal_id,listing_id,config_version_id,
                     platform_status,payload_json,created_at,playerok_account_id,is_historical,
                     source_listing_ref,listing_resolution_status)
                    VALUES('deal-internal-3','playerok','same-external','listing-1','config-1',
                           'PAID','{}','now','seller-2',0,'','resolved')
                """)
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("""
                        INSERT INTO gd_deals
                        (deal_id,source_platform,external_deal_id,listing_id,config_version_id,
                         platform_status,payload_json,created_at,playerok_account_id,is_historical,
                         source_listing_ref,listing_resolution_status)
                        VALUES('deal-internal-4','playerok','same-external','listing-1','config-1',
                               'PAID','{}','now','seller-1',0,'','resolved')
                    """)

    def test_account_identity_conflict_aborts_migration_without_deleting_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "conflict.sqlite3"
            with connect_sqlite(path) as db:
                _legacy_schema(db, unique_external=False)
                db.execute("INSERT INTO gd_listings VALUES('listing-1')")
                db.execute("INSERT INTO gd_config_versions VALUES('config-1')")
                db.execute("""
                    INSERT INTO gd_deals
                    (deal_id,source_platform,external_deal_id,listing_id,config_version_id,
                     platform_status,payload_json,created_at,playerok_account_id,is_historical)
                    VALUES
                    ('deal-1','playerok','duplicate','listing-1','config-1','PAID','{}','now','seller-1',0),
                    ('deal-2','playerok','duplicate','listing-1','config-1','PAID','{}','now','seller-1',0)
                """)
                db.commit()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "identity conflict"):
                    apply_delivery_ledger_migrations(db)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 2)
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM gd_schema_migrations WHERE migration_id='2026-11-playerok-sync-v2'"
                ).fetchone())

    def test_legacy_reservation_table_is_audited_compatibility_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reservation-bridge.sqlite3"
            with connect_sqlite(path) as db:
                _legacy_schema(db)
                self.assertTrue(apply_delivery_ledger_migrations(db))
                db.execute(
                    "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) VALUES('Bot-A','asset-1','legacy-a')"
                )
                row = db.execute(
                    "SELECT owner_type,legacy_owner_id,state FROM gd_resource_reservations WHERE bot_name='Bot-A' AND asset_id='asset-1'"
                ).fetchone()
                self.assertEqual(tuple(row), (
                    "legacy", "steam_asset_reservations:legacy-a", "active",
                ))
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM gd_reservation_events WHERE new_state='active'"
                ).fetchone()[0], 1)
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) VALUES('bot-a','asset-1','legacy-b')"
                    )
                db.execute("DELETE FROM steam_asset_reservations WHERE order_id='legacy-a'")
                self.assertEqual(db.execute(
                    "SELECT state FROM gd_resource_reservations WHERE bot_name='Bot-A' AND asset_id='asset-1'"
                ).fetchone()[0], "released")
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM gd_reservation_events WHERE previous_state='active' AND new_state='released'"
                ).fetchone()[0], 1)
                db.execute(
                    "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) VALUES('Bot-B','asset-2','legacy-c')"
                )
                db.execute(
                    "UPDATE gd_resource_reservations SET state='consumed' "
                    "WHERE bot_name='Bot-B' AND asset_id='asset-2' AND state='active'"
                )
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM steam_asset_reservations WHERE order_id='legacy-c'"
                ).fetchone()[0], 0)
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM gd_reservation_events WHERE previous_state='active' AND new_state='consumed'"
                ).fetchone()[0], 1)
                self.assertFalse(apply_delivery_ledger_migrations(db))

    def test_preexisting_legacy_reservation_is_backfilled_before_projection_triggers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "populated-legacy-reservation.sqlite3"
            with connect_sqlite(path) as db:
                _legacy_schema(db)
                db.execute("""
                    CREATE TABLE steam_asset_reservations(
                        bot_name TEXT NOT NULL, asset_id TEXT NOT NULL, order_id TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT 'legacy-time', PRIMARY KEY(bot_name,asset_id)
                    )
                """)
                db.execute(
                    "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) "
                    "VALUES('Bot-Legacy','asset-legacy','legacy-order')"
                )
                db.commit()
                self.assertTrue(apply_delivery_ledger_migrations(db))
                row = db.execute(
                    "SELECT owner_type,legacy_owner_id,game_id,app_id,context_id,state "
                    "FROM gd_resource_reservations WHERE bot_name='Bot-Legacy' AND asset_id='asset-legacy'"
                ).fetchone()
                self.assertEqual(tuple(row), (
                    "legacy", "steam_asset_reservations:legacy-order", "steam_cards", 753, "6", "active",
                ))
                # Re-running remains a no-op and retains both the projection and
                # canonical reservation without duplicating the audit event.
                self.assertFalse(apply_delivery_ledger_migrations(db))
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM gd_reservation_events WHERE new_state='active'"
                ).fetchone()[0], 1)

    def test_mapped_legacy_delivery_owner_is_not_marked_conflicting_and_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mapped-legacy-owner.sqlite3"
            database = Database(path)
            ledger = DeliveryLedger(database)
            config = ledger.create_listing_config_version(
                listing_id="mapped-listing", source_platform="manual",
                external_listing_id=None, delivery_quantity_per_deal=1,
                item_selectors={"game_id": "cs2", "app_id": 730, "context_id": 2},
                bot_pool=["Bot-A"],
            )
            ledger.create_deal(
                deal_id="mapped-deal", source_platform="manual", external_deal_id="mapped-external",
                chat_id=None, listing_id=config["listing_id"],
                config_version_id=config["config_version_id"], platform_status="PAID",
            )
            canonical_order = "legacy-delivery-order:123"
            ledger.create_order(order_id=canonical_order, deal_id="mapped-deal", total_quantity=1)
            line = ledger.add_order_line(
                order_line_id="mapped-line", order_id=canonical_order, line_number=1,
                item_type="legacy", item_name="legacy", item_selector={}, quantity=1,
            )
            with database.connect() as db:
                db.execute(
                    "INSERT INTO gd_legacy_order_map(legacy_table,legacy_order_id,order_id) "
                    "VALUES('delivery_orders','123',?)", (canonical_order,),
                )
                db.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,order_line_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('mapped-reservation','order',?,'steam_cards',753,'6','Bot-A','mapped-card',1,'active')",
                    (line["order_line_id"],),
                )
                db.execute(
                    "INSERT INTO steam_asset_reservations(bot_name,asset_id,order_id) "
                    "VALUES('Bot-A','mapped-card','123')"
                )
                db.execute(
                    "DELETE FROM gd_schema_migrations WHERE migration_id='2026-11-unified-resource-reservations-v1'"
                )
                db.commit()
                self.assertTrue(apply_delivery_ledger_migrations(db))
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM gd_resource_reservations WHERE bot_name='Bot-A' "
                    "AND asset_id='mapped-card' AND state='legacy_conflict'"
                ).fetchone()[0], 0)
                before = db.execute(
                    "SELECT COUNT(*) FROM gd_resource_reservations WHERE bot_name='Bot-A' AND asset_id='mapped-card'"
                ).fetchone()[0]
                self.assertEqual(before, 1)
                self.assertFalse(apply_delivery_ledger_migrations(db))
                after = db.execute(
                    "SELECT COUNT(*) FROM gd_resource_reservations WHERE bot_name='Bot-A' AND asset_id='mapped-card'"
                ).fetchone()[0]
                self.assertEqual(after, before)

            reservations = AssetReservationStore(path)
            self.assertEqual(reservations.list_reserved_assets(excluding_order_id=canonical_order), ())
            self.assertTrue(reservations.reserve(
                canonical_order, [("Bot-A", "mapped-card")]
            )["reserved"])


if __name__ == "__main__":
    unittest.main()
