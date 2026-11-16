from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.dispatcher import DeliveryDispatcher
from core.delivery.reconciliation import DeliveryReconciliation
from core.delivery.send_policy import DeliverySendPolicy
from integrations.asf.trade_credentials import SteamTradeCredentialStore


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def set_password(self, service, account, password):
        self.values[(service, account)] = password

    def get_password(self, service, account):
        return self.values.get((service, account))

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class MemorySettings:
    def __init__(self):
        self.values = {}

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        pass

    def status(self):
        return 0


class FakeASF:
    def __init__(self):
        self.send_result = {}
        self.send_error = {}
        self.statuses = {}
        self.sent = []
        self.inventory_refreshes = []

    def send_trade_offer(self, *, order_id, bot_name, trade_url, asset_ids, app_id=753, context_id=6):
        self.sent.append((order_id, bot_name, trade_url, tuple(asset_ids), app_id, context_id))
        if bot_name in self.send_error:
            raise self.send_error[bot_name]
        return self.send_result[bot_name]

    def get_trade_offer_status(self, *, bot_name, trade_offer_id):
        return self.statuses[(bot_name, trade_offer_id)]

    def get_bot_inventory(self, bot_name, *, app_id, context_id):
        self.inventory_refreshes.append((bot_name, app_id, context_id))
        return {"Success": True}


class DeliveryDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "delivery.sqlite3")
        self.ledger = DeliveryLedger(self.db)
        self.keyring = MemoryKeyring()
        self.credentials = SteamTradeCredentialStore(backend=self.keyring)
        self.asf = FakeASF()
        self.send_policy = DeliverySendPolicy(MemorySettings())
        self.send_policy.set_auto_send_enabled(True)
        self.sent_offers = self.root / "sent_offers.json"
        self.reconciler = DeliveryReconciliation(
            self.db, self.asf, sent_offers_path=self.sent_offers
        )
        self.dispatcher = DeliveryDispatcher(
            self.db, self.asf, self.credentials,
            restricted_buyer_message="Настройте Steam Guard и повторно обратитесь в магазин.",
            send_policy=self.send_policy,
        )
        self.trade_url = self.credentials.store_trade_url(
            "order-1", "https://steamcommunity.com/tradeoffer/new/?partner=123&token=secret_token"
        )

    def tearDown(self):
        self.temp.cleanup()

    def create_order(self, order_id="order-1", bots=("Bot-A",)):
        config = self.ledger.create_listing_config_version(
            listing_id=f"listing-{order_id}",
            source_platform="playerok",
            external_listing_id=f"listing-external-{order_id}",
            delivery_quantity_per_deal=1,
            item_selectors={"game_id": "cs2", "app_id": 730, "context_id": 2},
            bot_pool=list(bots),
        )
        self.ledger.create_deal(
            deal_id=f"deal-{order_id}",
            source_platform="playerok",
            external_deal_id=f"deal-external-{order_id}",
            chat_id=f"chat-{order_id}",
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            platform_status="PAID",
        )
        self.ledger.create_order(order_id=order_id, deal_id=f"deal-{order_id}", total_quantity=1)
        line_id = f"line-{order_id}"
        self.ledger.add_order_line(
            order_line_id=line_id, order_id=order_id, line_number=1,
            item_type="cs2_item", item_name="AK-47",
            item_selector={"game_id": "cs2", "app_id": 730, "context_id": 2},
            quantity=len(bots),
        )
        legs = []
        for index, bot in enumerate(bots, start=1):
            leg_id = f"leg-{order_id}-{index}"
            self.ledger.add_delivery_leg(
                leg_id=leg_id, order_line_id=line_id, bot_name=bot,
                app_id=730, context_id=2, asset_ids=[str(index * 100)],
                status="reserved",
            )
            with self.db.connect() as db:
                db.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,order_line_id,game_id,app_id,context_id,"
                    "bot_name,asset_id,units,state) VALUES(?,?,?,?,?,?,?,?,1,'active')",
                    (f"reservation-{order_id}-{index}", "order", line_id, "cs2", 730,
                     "2", bot, str(index * 100)),
                )
            legs.append(leg_id)
        if order_id != "order-1":
            self.credentials.store_trade_url(
                order_id, "https://steamcommunity.com/tradeoffer/new/?partner=123&token=secret_token"
            )
        return legs

    def dispatch(self, order_id, leg_id):
        safe_url = self.trade_url if order_id == "order-1" else (
            "https://steamcommunity.com/tradeoffer/new/?partner=123"
        )
        return self.dispatcher.dispatch_leg(
            order_id=order_id, leg_id=leg_id, stored_trade_url=safe_url
        )

    def test_transport_failure_becomes_unknown_and_reconciliation_recovers_offer_without_resend(self):
        leg = self.create_order()[0]
        self.asf.send_error["Bot-A"] = TimeoutError("IPC timeout")
        result = self.dispatch("order-1", leg)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(len(self.asf.sent), 1)
        self.assertEqual(self.asf.sent[0][-2:], (730, 2))
        retry = self.dispatch("order-1", leg)
        self.assertFalse(retry["dispatched"])
        self.assertEqual(len(self.asf.sent), 1, "UNKNOWN must prohibit automatic resend")

        self.sent_offers.write_text(json.dumps({
            "order-1": {
                "OrderId": "order-1", "BotName": "Bot-A",
                "AssetIds": ["100"], "OfferIds": ["90001"], "State": "sent",
            }
        }), encoding="utf-8")
        self.asf.statuses[("Bot-A", "90001")] = {
            "success": True, "mode": "trade-status", "found": True,
            "tradeOfferId": "90001", "botName": "Bot-A", "status": "Accepted",
        }

        reconciled = self.reconciler.reconcile(order_id="order-1")
        self.assertEqual(reconciled[0]["status"], "ACCEPTED")
        self.assertEqual(len(self.asf.sent), 1, "reconciliation must never resend the leg")
        with self.db.connect() as db:
            row = db.execute("SELECT status, trade_offer_id FROM gd_delivery_legs WHERE leg_id=?", (leg,)).fetchone()
            order = db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()
        self.assertEqual((row["status"], row["trade_offer_id"]), ("ACCEPTED", "90001"))
        self.assertEqual(order["status"], "DELIVERED")

    def test_startup_recovery_converts_interrupted_dispatch_to_unknown_before_polling(self):
        leg = self.create_order()[0]
        with self.db.connect() as db:
            db.execute(
                "UPDATE gd_delivery_legs SET status='DISPATCHING' WHERE leg_id=?", (leg,)
            )
        recovered = DeliveryReconciliation.recover_interrupted_dispatches(self.db)
        self.assertEqual(recovered, 1)
        with self.db.connect() as db:
            status = db.execute(
                "SELECT status FROM gd_delivery_legs WHERE leg_id=?", (leg,)
            ).fetchone()[0]
        self.assertEqual(status, "UNKNOWN")
        # Normal reconciliation may only read/recover; it must never create a new offer.
        result = self.reconciler.reconcile(order_id="order-1")
        self.assertEqual(result[0]["status"], "UNKNOWN")
        self.assertEqual(self.asf.sent, [])

    def test_global_off_blocks_dispatch_without_attempt_or_external_effects(self):
        leg = self.create_order()[0]
        off_dispatcher = DeliveryDispatcher(self.db, self.asf, self.credentials)

        result = off_dispatcher.dispatch_leg(
            order_id="order-1", leg_id=leg, stored_trade_url=self.trade_url
        )

        self.assertFalse(result["dispatched"])
        self.assertEqual(result["status"], "AUTO_SEND_DISABLED")
        self.assertEqual(self.asf.sent, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT status FROM gd_delivery_legs WHERE leg_id=?", (leg,)
            ).fetchone()[0], "reserved")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_events WHERE order_id='order-1'"
            ).fetchone()[0], 0)

    def test_manual_grant_dispatches_exact_leg_while_global_auto_is_off_and_is_audited(self):
        leg = self.create_order()[0]
        self.send_policy.set_auto_send_enabled(False)
        self.asf.send_result["Bot-A"] = {"success": True, "offerIds": ["manual-offer"]}
        grant = self.send_policy.issue_manual_authorization("order-1", leg)

        result = self.dispatcher.dispatch_leg(
            order_id="order-1", leg_id=leg, stored_trade_url=self.trade_url,
            manual_authorization=grant,
        )

        self.assertTrue(result["dispatched"])
        self.assertEqual(len(self.asf.sent), 1)
        self.assertFalse(self.send_policy.auto_send_enabled())
        self.assertFalse(self.send_policy.validate_manual_authorization(grant, "order-1", leg))
        with self.db.connect() as db:
            attempt = db.execute(
                "SELECT result_json FROM gd_delivery_attempts WHERE order_id='order-1'"
            ).fetchone()
        audit = json.loads(attempt["result_json"])
        self.assertEqual(audit["dispatch_authorization"]["mode"], "manual")
        self.assertEqual(audit["dispatch_authorization"]["authorization_id"], grant.authorization_id)

    def test_dispatch_rejects_reservation_mismatch_before_creating_attempt(self):
        leg = self.create_order()[0]
        with self.db.connect() as db:
            db.execute("UPDATE gd_resource_reservations SET state='released'")

        result = self.dispatch( "order-1", leg)

        self.assertFalse(result["dispatched"])
        self.assertIn("reservation", result["reason"])
        self.assertEqual(self.asf.sent, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'"
            ).fetchone()[0], 0)

    def test_dispatch_claim_serializes_legs_of_the_same_order(self):
        first_leg, second_leg = self.create_order(bots=("Bot-A", "Bot-B"))
        self.ledger.create_delivery_attempt(
            order_id="order-1", leg_id=first_leg, retry_generation=0,
            status="dispatching",
        )
        with self.db.connect() as db:
            db.execute("UPDATE gd_delivery_legs SET status='DISPATCHING' WHERE leg_id=?", (first_leg,))
            db.execute("UPDATE gd_orders SET status='PROCESSING' WHERE order_id='order-1'")

        result = self.dispatch("order-1", second_leg)

        self.assertFalse(result["dispatched"])
        self.assertIn("another leg", result["reason"])
        self.assertEqual(self.asf.sent, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'"
            ).fetchone()[0], 1)

    def test_multi_bot_order_delivers_only_after_both_offers_are_accepted(self):
        legs = self.create_order(bots=("Bot-A", "Bot-B"))
        self.asf.send_result = {
            "Bot-A": {"success": True, "offerIds": ["91001"]},
            "Bot-B": {"success": True, "offerIds": ["91002"]},
        }
        self.dispatch("order-1", legs[0])
        self.dispatch("order-1", legs[1])
        self.asf.statuses[("Bot-A", "91001")] = {
            "success": True, "mode": "trade-status", "found": True,
            "tradeOfferId": "91001", "botName": "Bot-A", "status": "Accepted",
        }
        self.asf.statuses[("Bot-B", "91002")] = {
            "success": True, "mode": "trade-status", "found": True,
            "tradeOfferId": "91002", "botName": "Bot-B", "status": "Active",
        }
        self.reconciler.reconcile(order_id="order-1")
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0], "PROCESSING")
        self.asf.statuses[("Bot-B", "91002")]["status"] = "Accepted"
        self.reconciler.reconcile(order_id="order-1")
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0], "DELIVERED")

    def test_privacy_error_holds_reservation_and_queues_buyer_instruction(self):
        leg = self.create_order()[0]
        self.asf.send_result["Bot-A"] = {
            "success": False, "resultUnknown": False,
            "error": "Recipient inventory is private",
        }
        result = self.dispatch("order-1", leg)
        self.assertEqual(result["status"], "PRIVACY_WAIT")
        with self.db.connect() as db:
            order = db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0]
            event = db.execute(
                "SELECT payload_json FROM gd_delivery_events WHERE order_id='order-1' AND event_type='buyer_privacy_instruction'"
            ).fetchone()
        self.assertEqual(order, "PRIVACY_WAIT")
        self.assertIn("!настроил приватность", event["payload_json"])

    def test_trade_restriction_queues_buyer_and_telegram_events_without_retry(self):
        leg = self.create_order()[0]
        self.asf.send_result["Bot-A"] = {
            "success": False, "resultUnknown": False,
            "error": "Recipient has a trade hold; Steam Guard required",
        }
        result = self.dispatch("order-1", leg)
        self.assertEqual(result["status"], "TRADE_RESTRICTED")
        with self.db.connect() as db:
            order = db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0]
            kinds = {row[0] for row in db.execute(
                "SELECT event_type FROM gd_delivery_events WHERE order_id='order-1'"
            ).fetchall()}
        self.assertEqual(order, "TRADE_RESTRICTED")
        self.assertIn("buyer_trade_restricted", kinds)
        self.assertIn("telegram_notification", kinds)
        again = self.dispatch("order-1", leg)
        self.assertFalse(again["dispatched"])

    def test_declined_offer_triggers_inventory_refresh_and_telegram_event(self):
        leg = self.create_order()[0]
        self.asf.send_result["Bot-A"] = {"success": True, "offerIds": ["92001"]}
        self.dispatch("order-1", leg)
        self.asf.statuses[("Bot-A", "92001")] = {
            "success": True, "mode": "trade-status", "found": True,
            "tradeOfferId": "92001", "botName": "Bot-A", "status": "Declined",
        }
        result = self.reconciler.reconcile(order_id="order-1")
        self.assertEqual(result[0]["status"], "DECLINED")
        self.assertEqual(self.asf.inventory_refreshes, [("Bot-A", 730, 2)])
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM gd_delivery_legs WHERE leg_id=?", (leg,)).fetchone()[0], "DECLINED")
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM gd_delivery_events WHERE order_id='order-1' AND event_type='telegram_notification'"
            ).fetchone())


if __name__ == "__main__":
    unittest.main()
