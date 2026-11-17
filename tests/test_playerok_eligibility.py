from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.database.connection import connect_sqlite
from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.playerok_eligibility import (
    PlayerokDeliveryBlocked,
    require_canonical_playerok_delivery_eligible,
    require_legacy_playerok_delivery_eligible,
)
from integrations.playerok.sync import PlayerokDealSyncService


class PlayerokDeliveryEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "orders.sqlite3"
        self.database = Database(self.path)
        self.ledger = DeliveryLedger(self.database)
        # The application creates this additive baseline column at startup.
        self.sync = PlayerokDealSyncService(self.database)
        config = self.ledger.create_listing_config_version(
            listing_id="listing-1", source_platform="playerok",
            external_listing_id="external-listing-1", delivery_quantity_per_deal=1,
            item_selectors={"game_id": "cs2", "app_id": 730, "context_id": 2},
            bot_pool=["Bot-A"],
        )
        self.ledger.create_deal(
            deal_id="deal-1", source_platform="playerok", external_deal_id="external-deal-1",
            chat_id="chat-1", listing_id="listing-1",
            config_version_id=config["config_version_id"], platform_status="PAID",
        )
        self.ledger.create_order(order_id="order-1", deal_id="deal-1", total_quantity=1)
        with connect_sqlite(self.path) as db:
            db.execute(
                "UPDATE gd_deals SET playerok_account_id='seller-1',is_historical=0 WHERE deal_id='deal-1'"
            )
            db.execute(
                "INSERT INTO playerok_sync_state(account_user_id,baseline_completed) VALUES('seller-1',1)"
            )
            db.execute("""
                CREATE TABLE delivery_orders (
                    order_id TEXT PRIMARY KEY,
                    source_platform TEXT NOT NULL,
                    source_order_id TEXT,
                    source_deal_id TEXT,
                    source_status
                )
            """)
            db.execute(
                "INSERT INTO delivery_orders VALUES('order-1','playerok','external-deal-1',"
                "'external-deal-1','PAID')"
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_paid_non_historical_canonical_deal_is_eligible_and_legacy_path_is_blocked(self):
        with self.database.connect() as db:
            require_canonical_playerok_delivery_eligible(db, "order-1")
            with self.assertRaises(PlayerokDeliveryBlocked):
                require_legacy_playerok_delivery_eligible(db, "order-1")

    def test_canonical_non_paid_statuses_fail_closed(self):
        for status in ("SENT", "CONFIRMED", "CANCELLED", "unknown", "", "paid", " PAID "):
            with self.subTest(status=status), self.database.connect() as db:
                db.execute("UPDATE gd_deals SET platform_status=? WHERE deal_id='deal-1'", (status,))
                with self.assertRaises(PlayerokDeliveryBlocked):
                    require_canonical_playerok_delivery_eligible(db, "order-1")

    def test_legacy_playerok_order_is_blocked_regardless_of_cached_status(self):
        for status in ("SENT", "CONFIRMED", "CANCELLED", "unknown", "", "paid", " PAID ", None, 7):
            with self.subTest(status=status), self.database.connect() as db:
                db.execute("UPDATE delivery_orders SET source_status=? WHERE order_id='order-1'", (status,))
                with self.assertRaises(PlayerokDeliveryBlocked):
                    require_legacy_playerok_delivery_eligible(db, "order-1")

    def test_historical_baseline_cannot_be_unblocked_by_reimporting_paid_status(self):
        with self.database.connect() as db:
            db.execute("UPDATE gd_deals SET is_historical=1 WHERE deal_id='deal-1'")
            # A later UI import may update this legacy snapshot, but history is sticky
            # in gd_deals and remains the authoritative blocker.
            db.execute("UPDATE delivery_orders SET source_status='PAID' WHERE order_id='order-1'")
            with self.assertRaises(PlayerokDeliveryBlocked):
                require_legacy_playerok_delivery_eligible(db, "order-1")
            with self.assertRaises(PlayerokDeliveryBlocked):
                require_canonical_playerok_delivery_eligible(db, "order-1")

    def test_paid_legacy_order_without_authoritative_deal_record_is_blocked(self):
        with self.database.connect() as db:
            db.execute(
                "UPDATE delivery_orders SET source_deal_id='missing-deal' WHERE order_id='order-1'"
            )
            with self.assertRaises(PlayerokDeliveryBlocked):
                require_legacy_playerok_delivery_eligible(db, "order-1")

    def test_non_playerok_manual_order_keeps_existing_behavior(self):
        with connect_sqlite(self.path) as db:
            db.execute(
                "INSERT INTO delivery_orders(order_id,source_platform,source_status) "
                "VALUES('manual-1','manual','')"
            )
            require_legacy_playerok_delivery_eligible(db, "manual-1")


if __name__ == "__main__":
    unittest.main()
