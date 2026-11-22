from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.recovery import DeliveryRecoveryBlocked, DeliveryRecoveryService
from core.delivery.reservation_service import InventorySnapshot


@dataclass(frozen=True)
class Item:
    asset_id: str
    is_tradable: bool
    bot_name: str = "Bot-A"


class DeliveryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "delivery.sqlite3")
        self.ledger = DeliveryLedger(self.database)
        config = self.ledger.create_listing_config_version(
            listing_id="listing-recovery",
            source_platform="local",
            external_listing_id="external-listing-recovery",
            delivery_quantity_per_deal=1,
            item_selectors={"game_id": "cs2", "app_id": 730, "context_id": 2},
            bot_pool=["Bot-A"],
        )
        self.ledger.create_deal(
            deal_id="deal-recovery",
            source_platform="local",
            external_deal_id="external-deal-recovery",
            chat_id=None,
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            platform_status="PAID",
        )
        self.ledger.create_order(
            order_id="order-recovery", deal_id="deal-recovery", total_quantity=1
        )
        self.ledger.add_order_line(
            order_line_id="line-recovery", order_id="order-recovery",
            line_number=1, item_type="cs2_item", item_name="AK-47",
            item_selector={"game_id": "cs2", "app_id": 730, "context_id": 2},
            quantity=1,
        )
        self.ledger.add_delivery_leg(
            leg_id="leg-recovery", order_line_id="line-recovery",
            bot_name="Bot-A", app_id=730, context_id=2,
            asset_ids=["asset-1"], status="FAILED",
        )
        self.ledger.bind_order_recipient("order-recovery", "76561197960265851")
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO gd_resource_reservations "
                "(reservation_id,owner_type,order_line_id,game_id,app_id,context_id,"
                "bot_name,asset_id,units,state) "
                "VALUES('reservation-recovery','order','line-recovery','cs2',730,'2',"
                "'Bot-A','asset-1',1,'active')"
            )
            self.ledger.create_delivery_attempt(
                order_id="order-recovery", leg_id="leg-recovery",
                retry_generation=0, status="failed",
                result={"error": "ASF rejected offer"}, connection=db,
            )
            db.execute("UPDATE gd_orders SET status='ACTION_REQUIRED' WHERE order_id='order-recovery'")
        self.snapshot = InventorySnapshot(
            bot_name="Bot-A", game_id="cs2", app_id=730, context_id="2",
            refreshed_at=datetime.now(timezone.utc),
            items=(Item("asset-1", True),), asf_verified=True,
        )
        self.recipient = "76561197960265851"
        self.no_offer = True
        self.service = self.make_service()

    def tearDown(self):
        self.temp.cleanup()

    def make_service(self, *, inventory=None, recipient=None, no_offer=None):
        return DeliveryRecoveryService(
            self.database,
            inventory_provider=lambda bot, app, context: self.snapshot if inventory is None else inventory,
            recipient_verifier=lambda order, leg: self.recipient if recipient is None else recipient,
            offer_absence_verifier=lambda order, leg, bot: self.no_offer if no_offer is None else no_offer,
        )

    def test_retry_creates_new_generation_and_preserves_old_attempt_and_reservation(self):
        result = self.service.retry(
            order_id="order-recovery", leg_id="leg-recovery",
            actor="operator-1", reason="Fresh ASF inventory verified",
        )
        self.assertEqual(result["retry_generation"], 1)
        with self.database.connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM gd_delivery_attempts WHERE retry_generation=0").fetchone()[0],
                "failed",
            )
            self.assertEqual(
                db.execute("SELECT status FROM gd_delivery_legs WHERE leg_id='leg-recovery'").fetchone()[0],
                "reserved",
            )
            self.assertEqual(
                db.execute("SELECT status FROM gd_delivery_attempts WHERE retry_generation=1").fetchone()[0],
                "prepared",
            )
            self.assertEqual(
                db.execute("SELECT state FROM gd_resource_reservations WHERE reservation_id='reservation-recovery'").fetchone()[0],
                "active",
            )
            action = db.execute(
                "SELECT action_type,retry_generation,actor,reason FROM gd_delivery_operator_actions"
            ).fetchone()
        self.assertEqual(tuple(action), ("retry", 1, "operator-1", "Fresh ASF inventory verified"))

    def test_retry_fails_closed_without_fresh_tradable_inventory(self):
        stale = InventorySnapshot(
            bot_name="Bot-A", game_id="cs2", app_id=730, context_id="2",
            refreshed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            items=(Item("asset-1", True),), asf_verified=True,
        )
        service = self.make_service(inventory=stale)
        with self.assertRaises(DeliveryRecoveryBlocked):
            service.retry(
                order_id="order-recovery", leg_id="leg-recovery",
                actor="operator-1", reason="retry",
            )
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM gd_delivery_legs").fetchone()[0], "FAILED")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_delivery_operator_actions").fetchone()[0], 0)

    def test_retry_fails_closed_when_recipient_or_offer_absence_is_unverified(self):
        for service in (
            self.make_service(recipient="76561197960265852"),
            self.make_service(no_offer=False),
        ):
            with self.assertRaises(DeliveryRecoveryBlocked):
                service.retry(
                    order_id="order-recovery", leg_id="leg-recovery",
                    actor="operator-1", reason="retry",
                )
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_delivery_operator_actions").fetchone()[0], 0)

    def test_unknown_attempt_cannot_be_retried(self):
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_delivery_attempts SET status='unknown' WHERE retry_generation=0"
            )
        with self.assertRaises(DeliveryRecoveryBlocked):
            self.service.retry(
                order_id="order-recovery", leg_id="leg-recovery",
                actor="operator-1", reason="retry",
            )

    def test_release_is_explicit_audited_and_releases_only_the_leg_reservation(self):
        result = self.service.release(
            order_id="order-recovery", leg_id="leg-recovery",
            actor="operator-2", reason="Order canceled after operator review",
        )
        self.assertEqual(result["status"], "released")
        with self.database.connect() as db:
            self.assertEqual(
                db.execute("SELECT state FROM gd_resource_reservations WHERE reservation_id='reservation-recovery'").fetchone()[0],
                "released",
            )
            action = db.execute(
                "SELECT action_type,actor,reason FROM gd_delivery_operator_actions"
            ).fetchone()
        self.assertEqual(tuple(action), ("release", "operator-2", "Order canceled after operator review"))


    def test_retry_rejects_inventory_snapshot_for_another_app_context(self):
        wrong_context = InventorySnapshot(
            bot_name="Bot-A", game_id="cs2", app_id=753, context_id="6",
            refreshed_at=datetime.now(timezone.utc),
            items=(Item("asset-1", True),), asf_verified=True,
        )
        service = self.make_service(inventory=wrong_context)
        with self.assertRaises(DeliveryRecoveryBlocked):
            service.retry(
                order_id="order-recovery", leg_id="leg-recovery",
                actor="operator-1", reason="retry",
            )


if __name__ == "__main__":
    unittest.main()
