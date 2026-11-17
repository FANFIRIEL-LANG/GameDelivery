from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.database.connection import connect_sqlite
from core.database.ledger_migrations import apply_delivery_ledger_migrations


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
    """ % (", UNIQUE(source_platform,external_deal_id)" if unique_external else ""))


class PlayerokSyncMigrationTests(unittest.TestCase):
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
                self.assertEqual(db.execute("SELECT order_id FROM gd_order_lines").fetchone()[0], "order-1")
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


if __name__ == "__main__":
    unittest.main()
