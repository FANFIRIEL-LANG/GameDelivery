from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.database.connection import connect_sqlite
from core.database.db import Database
from core.database.ledger_migrations import (
    BACKUP_SUFFIX,
    migrate_delivery_ledger,
)
from core.delivery.ledger import DeliveryLedger
from games.steam_cards.reservations import AssetReservationStore
from games.steam_cards.snapshot import InventorySnapshotStore


class DeliveryLedgerMigrationTests(unittest.TestCase):
    def test_legacy_orders_delivery_orders_and_reservations_are_preserved_and_imported(self):
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "legacy.sqlite3"
            with sqlite3.connect(database_path) as legacy:
                legacy.executescript(
                    """
                    CREATE TABLE orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source TEXT NOT NULL,
                        external_id TEXT NOT NULL,
                        item_type TEXT NOT NULL,
                        item_name TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        error_message TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(source, external_id)
                    );
                    CREATE TABLE delivery_orders (
                        order_id TEXT PRIMARY KEY,
                        recipient_trade_url TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        asset_count INTEGER NOT NULL,
                        created_at TEXT NOT NULL DEFAULT '',
                        source_platform TEXT NOT NULL DEFAULT 'manual',
                        source_order_id TEXT,
                        source_title TEXT NOT NULL DEFAULT '',
                        source_buyer TEXT NOT NULL DEFAULT '',
                        source_status TEXT NOT NULL DEFAULT '',
                        source_amount TEXT NOT NULL DEFAULT '',
                        source_url TEXT NOT NULL DEFAULT '',
                        trade_offer_id TEXT NOT NULL DEFAULT '',
                        trade_confirmation_status TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE delivery_order_assets (
                        order_id TEXT NOT NULL,
                        bot_name TEXT NOT NULL,
                        asset_id TEXT NOT NULL,
                        PRIMARY KEY(order_id, bot_name, asset_id),
                        FOREIGN KEY(order_id) REFERENCES delivery_orders(order_id)
                    );
                    CREATE TABLE steam_asset_reservations (
                        bot_name TEXT NOT NULL,
                        asset_id TEXT NOT NULL,
                        order_id TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY(bot_name, asset_id)
                    );
                    """
                )
                legacy.execute(
                    """
                    INSERT INTO orders
                        (source, external_id, item_type, item_name, quantity, status,
                         payload_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "Playerok", "legacy-core-deal", "steam_card_set", "Legacy set", 2, "queued",
                        json.dumps({"chat_id": "legacy-chat-1"}),
                        "2026-10-01T10:00:00Z", "2026-10-01T10:00:00Z",
                    ),
                )
                legacy.execute(
                    """
                    INSERT INTO delivery_orders
                        (order_id, recipient_trade_url, status, asset_count, created_at,
                         source_platform, source_order_id, source_title, source_buyer,
                         source_status, trade_offer_id, trade_confirmation_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "GD-LEGACY-1",
                        "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=do-not-copy-this-token",
                        "sent — ожидание покупателя", 2, "2026-10-02T11:00:00Z",
                        "playerok", "deal-legacy-1", "Legacy cards", "Buyer", "PAID",
                        "9876543210", "pending",
                    ),
                )
                legacy.executemany(
                    "INSERT INTO delivery_order_assets (order_id, bot_name, asset_id) VALUES (?, ?, ?)",
                    [
                        ("GD-LEGACY-1", "Bot-A", "10001"),
                        ("GD-LEGACY-1", "Bot-A", "10002"),
                    ],
                )
                legacy.execute(
                    "INSERT INTO steam_asset_reservations (bot_name, asset_id, order_id) VALUES (?, ?, ?)",
                    ("Bot-A", "10001", "GD-LEGACY-1"),
                )

            original_rows = database_path.stat().st_size
            self.assertTrue(migrate_delivery_ledger(database_path))
            self.assertFalse(migrate_delivery_ledger(database_path))

            backup_path = Path(str(database_path) + BACKUP_SUFFIX)
            self.assertTrue(backup_path.exists())
            self.assertNotIn(b"do-not-copy-this-token", backup_path.read_bytes())
            with connect_sqlite(backup_path) as backup:
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(
                    backup.execute("SELECT COUNT(*) FROM delivery_orders").fetchone()[0], 1
                )
                self.assertEqual(
                    backup.execute("SELECT recipient_trade_url FROM delivery_orders").fetchone()[0],
                    "",
                )

            with connect_sqlite(database_path) as migrated:
                self.assertEqual(migrated.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM delivery_orders").fetchone()[0], 1)
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM delivery_order_assets").fetchone()[0], 2)
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM steam_asset_reservations").fetchone()[0], 1)

                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 2)
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 2)
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM gd_order_lines").fetchone()[0], 2)
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM gd_delivery_legs").fetchone()[0], 1)
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM gd_delivery_attempts").fetchone()[0], 1)
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active'").fetchone()[0], 1)

                core_deal = migrated.execute(
                    "SELECT chat_id FROM gd_deals WHERE deal_id='legacy-core-deal:1'"
                ).fetchone()
                self.assertEqual(core_deal["chat_id"], "legacy-chat-1")

                leg = migrated.execute(
                    "SELECT bot_name, app_id, context_id, asset_ids_json, trade_offer_id "
                    "FROM gd_delivery_legs"
                ).fetchone()
                self.assertEqual(leg["bot_name"], "Bot-A")
                self.assertEqual(leg["app_id"], 753)
                self.assertEqual(leg["context_id"], "6")
                self.assertEqual(json.loads(leg["asset_ids_json"]), ["10001", "10002"])
                self.assertEqual(leg["trade_offer_id"], "9876543210")

                reservation = migrated.execute(
                    "SELECT owner_type, order_line_id, bot_name, asset_id, state "
                    "FROM gd_resource_reservations WHERE state='active'"
                ).fetchone()
                self.assertEqual(reservation["owner_type"], "order")
                self.assertTrue(reservation["order_line_id"])
                self.assertEqual((reservation["bot_name"], reservation["asset_id"]), ("Bot-A", "10001"))

                violations = []
                for table in (
                    "gd_config_versions", "gd_deals", "gd_orders", "gd_order_lines",
                    "gd_delivery_legs", "gd_delivery_attempts", "gd_resource_reservations",
                ):
                    violations.extend(migrated.execute(f"PRAGMA foreign_key_check({table})").fetchall())
                self.assertEqual(violations, [])

                self.assertIsNotNone(migrated.execute(
                    "SELECT 1 FROM gd_schema_migrations WHERE migration_id='2026-11-delivery-ledger-v1'"
                ).fetchone())

            self.assertGreaterEqual(database_path.stat().st_size, original_rows)

    def test_foreign_keys_are_enabled_for_database_and_runtime_store_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fk.sqlite3"
            database = Database(path)
            database.initialize()

            with database.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

            with connect_sqlite(path) as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

            reservation_store = AssetReservationStore(path)
            with reservation_store._connect() as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

            snapshot_store = InventorySnapshotStore(path)
            with snapshot_store._connect() as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_config_version_is_an_immutable_snapshot_and_new_changes_create_new_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "ledger.sqlite3")
            ledger = DeliveryLedger(database)
            version_one = ledger.create_listing_config_version(
                listing_id="listing-1",
                source_platform="playerok",
                external_listing_id="listing-external-1",
                delivery_quantity_per_deal=3,
                item_selectors={"game_id": "cs2", "market_hash_name": "AK-47 | Redline"},
                bot_pool=["Bot-A", "Bot-B"],
                title="CS2 bundle",
            )
            version_two = ledger.create_listing_config_version(
                listing_id="listing-1",
                source_platform="playerok",
                external_listing_id="listing-external-1",
                delivery_quantity_per_deal=5,
                item_selectors={"game_id": "cs2", "market_hash_name": "AWP | Asiimov"},
                bot_pool=["Bot-C"],
                title="CS2 bundle v2",
            )
            self.assertEqual(version_one["version_number"], 1)
            self.assertEqual(version_two["version_number"], 2)

            with database.connect() as connection:
                stored_one = connection.execute(
                    "SELECT delivery_quantity_per_deal, item_selectors_json, bot_pool_json "
                    "FROM gd_config_versions WHERE config_version_id=?",
                    (version_one["config_version_id"],),
                ).fetchone()
                self.assertEqual(stored_one["delivery_quantity_per_deal"], 3)
                self.assertEqual(json.loads(stored_one["item_selectors_json"]), version_one["item_selectors"])
                self.assertEqual(json.loads(stored_one["bot_pool_json"]), ["Bot-A", "Bot-B"])
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE gd_config_versions SET delivery_quantity_per_deal=99 WHERE config_version_id=?",
                        (version_one["config_version_id"],),
                    )

    def test_delivery_attempt_idempotency_key_is_composite_and_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "ledger.sqlite3")
            ledger = DeliveryLedger(database)
            config = ledger.create_listing_config_version(
                listing_id="listing-2",
                source_platform="playerok",
                external_listing_id="listing-external-2",
                delivery_quantity_per_deal=1,
                item_selectors={"app_id": 753, "context_id": 6, "item_type": "TradingCard"},
                bot_pool=["Cards-Bot"],
            )
            ledger.create_deal(
                deal_id="deal-2",
                source_platform="playerok",
                external_deal_id="deal-external-2",
                chat_id="chat-2",
                listing_id="listing-2",
                config_version_id=config["config_version_id"],
                platform_status="PAID",
            )
            ledger.create_order(order_id="order-2", deal_id="deal-2", total_quantity=1)
            ledger.add_order_line(
                order_line_id="line-2", order_id="order-2", line_number=1,
                item_type="steam_card", item_name="Trading Card",
                item_selector={"market_hash_name": "Card A"}, quantity=1,
            )
            ledger.add_delivery_leg(
                leg_id="leg-2", order_line_id="line-2", bot_name="Cards-Bot",
                app_id=753, context_id=6, asset_ids=["123456"],
            )

            first, created = ledger.create_delivery_attempt(
                order_id="order-2", leg_id="leg-2", retry_generation=0
            )
            repeated, created_again = ledger.create_delivery_attempt(
                order_id="order-2", leg_id="leg-2", retry_generation=0,
                status="must-not-overwrite",
            )
            next_generation, next_created = ledger.create_delivery_attempt(
                order_id="order-2", leg_id="leg-2", retry_generation=1
            )

            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(first["attempt_id"], repeated["attempt_id"])
            self.assertEqual(first["idempotency_key"], "order-2:leg-2:0")
            self.assertEqual(repeated["status"], "prepared")
            self.assertTrue(next_created)
            self.assertEqual(next_generation["idempotency_key"], "order-2:leg-2:1")

            with database.connect() as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO gd_delivery_attempts
                            (attempt_id, order_id, leg_id, retry_generation, idempotency_key)
                        VALUES ('duplicate-attempt', 'order-2', 'leg-2', 0, 'order-2:leg-2:0')
                        """,
                    )

    def test_active_resource_reservation_is_globally_unique_per_bot_and_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "ledger.sqlite3")
            ledger = DeliveryLedger(database)
            config = ledger.create_listing_config_version(
                listing_id="listing-3",
                source_platform="playerok",
                external_listing_id="listing-external-3",
                delivery_quantity_per_deal=1,
                item_selectors={"app_id": 730, "context_id": 2, "name": "CS2 item"},
                bot_pool=["Bot-A"],
            )
            ledger.create_deal(
                deal_id="deal-3", source_platform="playerok", external_deal_id="deal-external-3",
                chat_id=None, listing_id="listing-3",
                config_version_id=config["config_version_id"], platform_status="PAID",
            )
            ledger.create_order(order_id="order-3", deal_id="deal-3", total_quantity=1)
            ledger.add_order_line(
                order_line_id="line-3", order_id="order-3", line_number=1,
                item_type="cs2_item", item_name="CS2 item", item_selector={"name": "CS2 item"},
                quantity=1,
            )
            with database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO gd_resource_reservations
                        (reservation_id, owner_type, order_line_id, game_id,
                         app_id, context_id, bot_name, asset_id, units, state)
                    VALUES ('reserve-1', 'order', 'line-3', 'cs2', 730, '2', 'Bot-A', '999', 1, 'active')
                    """
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO gd_resource_reservations
                            (reservation_id, owner_type, order_line_id, game_id,
                             app_id, context_id, bot_name, asset_id, units, state)
                        VALUES ('reserve-2', 'order', 'line-3', 'cs2', 730, '2', 'bot-a', '999', 1, 'active')
                        """
                    )


if __name__ == "__main__":
    unittest.main()
