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
from core.delivery.stock_ledger import QuantityStockLedger, StockResource
from core.delivery.stock_identity import StockItemIdentity


@dataclass(frozen=True)
class Item:
    asset_id: str
    is_tradable: bool
    bot_name: str = "Bot-A"
    amount: int = 1


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
        self.config_id = config["config_version_id"]
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
            asset_ids=["asset-1"], status="reserved",
        )
        with self.database.connect() as db:
            db.execute("UPDATE gd_orders SET status='reserved' WHERE order_id='order-recovery'")
            db.execute(
                "INSERT INTO gd_resource_reservations "
                "(reservation_id,owner_type,order_line_id,game_id,app_id,context_id,"
                "bot_name,asset_id,units,state) "
                "VALUES('reservation-recovery','order','line-recovery','cs2',730,'2',"
                "'Bot-A','asset-1',1,'active')"
            )
        self.ledger.bind_order_recipient("order-recovery", "76561197960265851")
        with self.database.connect() as db:
            db.execute("UPDATE gd_delivery_legs SET status='FAILED' WHERE leg_id='leg-recovery'")
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


    def attach_quantity_stock(self):
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_resource_reservations SET state='released' "
                "WHERE order_line_id='line-recovery' AND asset_id='asset-1'"
            )
            db.execute(
                "UPDATE gd_delivery_legs SET asset_amounts_json=? WHERE leg_id='leg-recovery'",
                ('{"asset-1":3}',),
            )
            db.execute(
                "UPDATE gd_listings SET status='disabled' WHERE listing_id='listing-recovery'"
            )
        observation = QuantityStockLedger(self.database).observe(
            StockResource("cs2", 730, "2", "Bot-A", "asset-1"),
            10,
            identity=StockItemIdentity(
                "cs2", 730, "2",
                market_hash_name="AK-47",
                name="AK-47",
                item_type="Rifle",
                class_id="class-1",
            ),
            is_tradable=True,
        )
        with self.database.connect() as db:
            db.execute(
                """
                INSERT INTO gd_stock_allocations
                    (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                     asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                     order_line_id,legacy_owner_id,units,state,recipe_item_id)
                VALUES(?,?,?,?,?,?,?,?,'order',NULL,NULL,?,NULL,3,'active',NULL)
                """,
                (
                    "stock-recovery", observation["observation_id"], "cs2", 730,
                    "2", "Bot-A", "asset-1", observation["asset_id_key"], "line-recovery",
                ),
            )
            db.execute(
                """
                INSERT INTO gd_stock_transfers
                    (transfer_event_id,listing_id,config_version_id,order_line_id,
                     units,allocation_ids_json,recipe_item_id)
                VALUES('transfer-recovery','listing-recovery',?,'line-recovery',3,?,NULL)
                """,
                (self.config_id, '["stock-recovery"]'),
            )
        self.snapshot = InventorySnapshot(
            bot_name="Bot-A", game_id="cs2", app_id=730, context_id="2",
            refreshed_at=datetime.now(timezone.utc),
            items=(Item("asset-1", True, amount=3),), asf_verified=True,
        )

    def test_operator_release_handles_quantity_stock_and_exact_live_amount(self):
        self.attach_quantity_stock()
        result = self.service.release(
            order_id="order-recovery", leg_id="leg-recovery",
            actor="operator", reason="No Steam offer exists",
        )
        self.assertEqual(result["status"], "released")
        self.assertEqual(result["released_allocations"], 1)
        self.assertEqual(result["restored_listing_units"], 0)
        with self.database.connect() as db:
            state = db.execute(
                "SELECT state FROM gd_stock_allocations WHERE allocation_id='stock-recovery'"
            ).fetchone()[0]
        self.assertEqual(state, "released")

    def test_operator_release_rejects_live_stack_amount_below_delivery_amount(self):
        self.attach_quantity_stock()
        self.snapshot = InventorySnapshot(
            bot_name="Bot-A", game_id="cs2", app_id=730, context_id="2",
            refreshed_at=datetime.now(timezone.utc),
            items=(Item("asset-1", True, amount=2),), asf_verified=True,
        )
        with self.assertRaisesRegex(DeliveryRecoveryBlocked, "меньше единиц стека"):
            self.service.release(
                order_id="order-recovery", leg_id="leg-recovery",
                actor="operator", reason="No Steam offer exists",
            )
        with self.database.connect() as db:
            state = db.execute(
                "SELECT state FROM gd_stock_allocations WHERE allocation_id='stock-recovery'"
            ).fetchone()[0]
        self.assertEqual(state, "active")

    def test_cancel_order_releases_all_proven_unsent_legs_atomically(self):
        result = self.service.cancel_order(
            order_id="order-recovery", actor="operator-1",
            reason="Buyer cancelled before any Steam offer was sent",
        )
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(result["released_legs"], 1)
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT status FROM gd_orders WHERE order_id='order-recovery'"
            ).fetchone()[0], "CANCELLED")
            self.assertEqual(db.execute(
                "SELECT status FROM gd_delivery_legs WHERE leg_id='leg-recovery'"
            ).fetchone()[0], "CANCELLED")
            self.assertEqual(db.execute(
                "SELECT state FROM gd_resource_reservations WHERE reservation_id='reservation-recovery'"
            ).fetchone()[0], "released")
            action = db.execute(
                "SELECT action_type,actor,reason,evidence_json FROM gd_delivery_operator_actions"
            ).fetchone()
        self.assertEqual(action["action_type"], "release")
        self.assertEqual(action["actor"], "operator-1")
        self.assertTrue(__import__("json").loads(action["evidence_json"])["order_cancelled"])

    def test_cancel_order_fails_closed_when_offer_absence_is_not_confirmed(self):
        service = self.make_service(no_offer=False)
        with self.assertRaises(DeliveryRecoveryBlocked):
            service.cancel_order(
                order_id="order-recovery", actor="operator-1", reason="cancel",
            )
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT status FROM gd_orders WHERE order_id='order-recovery'"
            ).fetchone()[0], "ACTION_REQUIRED")
            self.assertEqual(db.execute(
                "SELECT state FROM gd_resource_reservations WHERE reservation_id='reservation-recovery'"
            ).fetchone()[0], "active")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_operator_actions"
            ).fetchone()[0], 0)

    def test_cancel_order_blocks_unknown_or_already_accepted_leg(self):
        with self.database.connect() as db:
            db.execute("UPDATE gd_delivery_legs SET status='UNKNOWN' WHERE leg_id='leg-recovery'")
        with self.assertRaises(DeliveryRecoveryBlocked):
            self.service.cancel_order(
                order_id="order-recovery", actor="operator-1", reason="cancel",
            )
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT state FROM gd_resource_reservations WHERE reservation_id='reservation-recovery'"
            ).fetchone()[0], "active")
        with self.database.connect() as db:
            db.execute("UPDATE gd_delivery_legs SET status='ACCEPTED' WHERE leg_id='leg-recovery'")
        with self.assertRaises(DeliveryRecoveryBlocked):
            self.service.cancel_order(
                order_id="order-recovery", actor="operator-1", reason="cancel",
            )

    def test_cancel_order_requires_action_required_and_rejects_second_cancel(self):
        with self.database.connect() as db:
            db.execute("UPDATE gd_orders SET status='PROCESSING' WHERE order_id='order-recovery'")
        with self.assertRaises(DeliveryRecoveryBlocked):
            self.service.cancel_order(
                order_id="order-recovery", actor="operator-1", reason="cancel",
            )
        with self.database.connect() as db:
            db.execute("UPDATE gd_orders SET status='ACTION_REQUIRED' WHERE order_id='order-recovery'")
        self.service.cancel_order(
            order_id="order-recovery", actor="operator-1", reason="cancel",
        )
        with self.assertRaises(DeliveryRecoveryBlocked):
            self.service.cancel_order(
                order_id="order-recovery", actor="operator-1", reason="second cancel",
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
            prepared = db.execute(
                "SELECT status,result_json FROM gd_delivery_attempts WHERE retry_generation=1"
            ).fetchone()
            self.assertEqual(prepared["status"], "prepared")
            import json
            proof_expiry = json.loads(prepared["result_json"])["recovery_evidence"]["inventory"]["expires_at"]
            self.assertTrue(proof_expiry)
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


    def test_manual_override_releases_unknown_quantity_stock_and_invalidates_snapshot(self):
        self.attach_quantity_stock()
        with self.assertRaises(DeliveryRecoveryBlocked):
            self.service.manual_release_reservation(
                resource_kind="stock_allocation", resource_id="stock-recovery",
                actor="operator-3", reason="Unknown offer reviewed",
                reason_code="unknown", steam_status="unknown", confirmed=False,
            )

        result = self.service.manual_release_reservation(
            resource_kind="stock_allocation", resource_id="stock-recovery",
            actor="operator-3", reason="Unknown offer reviewed manually",
            reason_code="unknown", steam_status="unknown",
            related_offer_ids=["offer-unknown-1"], confirmed=True, override_confirmed=True,
        )
        self.assertTrue(result["local_only"])
        self.assertFalse(result["steam_offer_cancelled"])
        self.assertTrue(result["snapshot_refresh_required"])
        with self.database.connect() as db:
            allocation = db.execute(
                "SELECT state FROM gd_stock_allocations WHERE allocation_id='stock-recovery'"
            ).fetchone()
            observation = db.execute(
                "SELECT availability_state FROM gd_stock_observations "
                "WHERE observation_id=(SELECT observation_id FROM gd_stock_allocations "
                "WHERE allocation_id='stock-recovery')"
            ).fetchone()
            action = db.execute(
                "SELECT order_id,leg_id,previous_state,resulting_state,steam_status,"
                "related_offer_ids_json,reason_code,actor,confirmed,evidence_json "
                "FROM gd_reservation_manual_actions WHERE action_id=?",
                (result["action_id"],),
            ).fetchone()
        self.assertEqual(allocation["state"], "released")
        self.assertEqual(observation["availability_state"], "stale")
        self.assertEqual(action["order_id"], "order-recovery")
        self.assertEqual(action["leg_id"], "leg-recovery")
        self.assertEqual(action["previous_state"], "active")
        self.assertEqual(action["resulting_state"], "released")
        self.assertEqual(action["steam_status"], "unknown")
        self.assertEqual(action["related_offer_ids_json"], '["offer-unknown-1"]')
        self.assertEqual(action["reason_code"], "unknown")
        self.assertEqual(action["actor"], "operator-3")
        self.assertEqual(action["confirmed"], 1)
        evidence = __import__("json").loads(action["evidence_json"])
        self.assertTrue(evidence["local_reservation_only"])
        self.assertFalse(evidence["steam_offer_cancelled"])
        self.assertTrue(evidence["snapshot_refresh_required"])

    def test_manual_override_marks_accepted_stock_consumed_not_free(self):
        self.attach_quantity_stock()
        result = self.service.manual_release_reservation(
            resource_kind="stock_allocation", resource_id="stock-recovery",
            actor="operator-4", reason="Steam acceptance confirmed",
            reason_code="other", steam_status="accepted", confirmed=True,
        )
        self.assertEqual(result["status"], "consumed")
        with self.database.connect() as db:
            row = db.execute(
                "SELECT state,consumed_revision FROM gd_stock_allocations "
                "WHERE allocation_id='stock-recovery'"
            ).fetchone()
        self.assertEqual(row["state"], "consumed")
        self.assertGreater(row["consumed_revision"], 0)

    def test_orphaned_legacy_reservation_is_visible_and_manually_removable(self):
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO gd_resource_reservations "
                "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,"
                "bot_name,asset_id,units,state) "
                "VALUES('reservation-orphan','legacy','missing-order-legacy','cs2',730,'2',"
                "'Bot-A','asset-orphan',1,'active')"
            )
        visible = self.service.list_active_reservations()
        orphan = next(row for row in visible if row["resource_id"] == "reservation-orphan")
        self.assertTrue(orphan["orphaned"])
        result = self.service.manual_release_reservation(
            resource_kind="resource_reservation", resource_id="reservation-orphan",
            actor="operator-5", reason="Legacy orphan reviewed",
            reason_code="other", steam_status="unknown", confirmed=True, override_confirmed=True,
        )
        self.assertIsNone(result["order_id"])
        self.assertEqual(result["status"], "released")
        with self.database.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT state FROM gd_resource_reservations WHERE reservation_id='reservation-orphan'"
                ).fetchone()[0],
                "released",
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM gd_reservation_manual_actions WHERE resource_id='reservation-orphan'"
                ).fetchone()[0],
                1,
            )

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
