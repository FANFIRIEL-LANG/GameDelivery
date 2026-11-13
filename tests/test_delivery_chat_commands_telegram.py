from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.chat_commands import BuyerChatCommandRegistry
from core.delivery.dispatcher import DeliveryDispatcher
from core.delivery.reconciliation import DeliveryReconciliation
from integrations.asf.trade_credentials import SteamTradeCredentialStore
from integrations.telegram.outbox import (
    BuyerPlatformMessageOutbox, TelegramBotOutbox, TelegramCredentialStore,
    TelegramOperatorReplyRouter,
)


class MemoryKeyring:
    def __init__(self):
        self.values = {}
    def set_password(self, service, account, password):
        self.values[(service, account)] = password
    def get_password(self, service, account):
        return self.values.get((service, account))
    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class FakeASF:
    def __init__(self):
        self.sent = []
        self.inventory = {}
        self.status_calls = []
    def get_bot_inventory(self, bot_name, *, app_id, context_id):
        return self.inventory[bot_name]
    def send_trade_offer(self, *, order_id, bot_name, trade_url, asset_ids):
        self.sent.append((order_id, bot_name, trade_url, tuple(asset_ids)))
        return {"success": True, "mode": "sent", "sendEnabled": True,
                "orderId": order_id, "botName": bot_name,
                "targetSteamId64": 76561197960265851, "assetCount": len(asset_ids),
                "offerIds": ["99001"], "assets": [{"assetId": x} for x in asset_ids]}
    def get_trade_offer_status(self, **kwargs):
        self.status_calls.append(kwargs)
        return {"success": False, "found": False, "status": "unknown"}


class FakeReconciliation:
    def __init__(self):
        self.calls = []
    def reconcile(self, *, order_id=None):
        self.calls.append(order_id)
        return []


class ChatCommandsAndTelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "delivery.sqlite3")
        self.ledger = DeliveryLedger(self.db)
        self.asf = FakeASF()
        self.reconciliation = FakeReconciliation()
        self.keyring = MemoryKeyring()
        self.trade_credentials = SteamTradeCredentialStore(backend=self.keyring)
        self.trade_credentials.store_trade_url(
            "order-1", "https://steamcommunity.com/tradeoffer/new/?partner=123&token=trade_secret"
        )
        self.dispatcher = DeliveryDispatcher(self.db, self.asf, self.trade_credentials)
        self._create_order()

    def tearDown(self):
        self.temp.cleanup()

    def _create_order(self, status="PRIVACY_WAIT"):
        config = self.ledger.create_listing_config_version(
            listing_id="listing-1", source_platform="playerok",
            external_listing_id="listing-external-1", delivery_quantity_per_deal=1,
            item_selectors={"app_id": 730, "context_id": 2}, bot_pool=["Bot-A"],
        )
        self.ledger.create_deal(
            deal_id="deal-1", source_platform="playerok", external_deal_id="deal-ext-1",
            chat_id="buyer-chat-1", listing_id="listing-1",
            config_version_id=config["config_version_id"], platform_status="PAID",
            payload={"buyer_steam_id64": "76561197960265851",
                     "stored_trade_url": "https://steamcommunity.com/tradeoffer/new/?partner=123"},
        )
        self.ledger.create_order(order_id="order-1", deal_id="deal-1", total_quantity=1, status=status)
        self.ledger.add_order_line(
            order_line_id="line-1", order_id="order-1", line_number=1,
            item_type="cs2_item", item_name="Skin",
            item_selector={"app_id": 730, "context_id": 2}, quantity=1,
        )
        self.ledger.add_delivery_leg(
            leg_id="leg-1", order_line_id="line-1", bot_name="Bot-A",
            app_id=730, context_id=2, asset_ids=["asset-1"], status="PRIVACY_WAIT",
        )
        self.ledger.create_delivery_attempt(
            order_id="order-1", leg_id="leg-1", retry_generation=0,
            status="privacy_wait", result={"error": "private"},
        )
        with self.db.connect() as db:
            db.execute("""
                INSERT INTO gd_resource_reservations
                    (reservation_id,owner_type,order_line_id,game_id,app_id,context_id,
                     bot_name,asset_id,units,state)
                VALUES ('reservation-1','order','line-1','cs2',730,'2','Bot-A','asset-1',1,'active')
            """)

    def _commands(self, custom=None):
        return BuyerChatCommandRegistry(
            self.db, self.dispatcher, self.reconciliation,
            inventory_client=self.asf, custom_commands=custom,
        )

    def test_privacy_command_rechecks_inventory_and_dispatches_generation_plus_one(self):
        self.asf.inventory["Bot-A"] = {
            "Success": True, "Result": {"Bot-A": {"Assets": [{"assetid": "asset-1", "tradable": True}]}}
        }
        result = self._commands().handle(
            text="!настроил приватность", chat_id="buyer-chat-1",
            deal_id="deal-1", buyer_identity="76561197960265851", identity_verified=True,
        )
        self.assertEqual(result.status, "SENT_TO_STEAM")
        self.assertEqual(len(self.asf.sent), 1)
        with self.db.connect() as db:
            attempt = db.execute(
                "SELECT retry_generation,idempotency_key FROM gd_delivery_attempts "
                "WHERE order_id='order-1' ORDER BY retry_generation DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(attempt["retry_generation"], 1)
        self.assertEqual(attempt["idempotency_key"], "order-1:leg-1:1")

    def test_privacy_command_is_ignored_for_stale_states_and_wrong_identity_or_chat(self):
        commands = self._commands()
        wrong = commands.handle(
            text="!настроил приватность", chat_id="another-chat", deal_id="deal-1",
            buyer_identity="76561197960265851", identity_verified=True,
        )
        self.assertEqual(wrong.status, "unmatched")
        foreign = commands.handle(
            text="!настроил приватность", chat_id="buyer-chat-1", deal_id="deal-1",
            buyer_identity="76561197960000000", identity_verified=True,
        )
        self.assertEqual(foreign.status, "ignored_identity")
        self.assertEqual(len(self.asf.sent), 0)
        for state in ("SENT_TO_STEAM", "UNKNOWN", "DELIVERED", "CANCELED"):
            with self.db.connect() as db:
                db.execute("UPDATE gd_orders SET status=? WHERE order_id='order-1'", (state,))
            stale = commands.handle(
                text="!настроил приватность", chat_id="buyer-chat-1", deal_id="deal-1",
                buyer_identity="76561197960265851", identity_verified=True,
            )
            self.assertEqual(stale.status, "stale_ignored")
        self.assertEqual(len(self.reconciliation.calls), 0)

    def test_unknown_leg_routes_privacy_command_to_reconciliation_not_retry(self):
        with self.db.connect() as db:
            db.execute("UPDATE gd_orders SET status='PRIVACY_WAIT' WHERE order_id='order-1'")
            db.execute("UPDATE gd_delivery_legs SET status='UNKNOWN' WHERE leg_id='leg-1'")
        result = self._commands().handle(
            text="!настроил приватность", chat_id="buyer-chat-1", deal_id="deal-1",
            buyer_identity="76561197960265851", identity_verified=True,
        )
        self.assertEqual(result.status, "reconciliation_required")
        self.assertEqual(self.reconciliation.calls, ["order-1"])
        self.assertEqual(len(self.asf.sent), 0)

    def test_status_and_support_commands_are_read_only_and_support_alerts_operator(self):
        commands = self._commands()
        status = commands.handle(text="!статус", chat_id="buyer-chat-1", deal_id="deal-1")
        self.assertTrue(status.handled)
        support = commands.handle(text="!поддержка", chat_id="buyer-chat-1", deal_id="deal-1")
        self.assertTrue(support.handled)
        with self.db.connect() as db:
            order = db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0]
            event = db.execute(
                "SELECT payload_json FROM gd_delivery_events WHERE order_id='order-1' AND event_type='operator_support_request'"
            ).fetchone()
            attempts = db.execute("SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'").fetchone()[0]
        self.assertEqual(order, "PRIVACY_WAIT")
        self.assertIsNotNone(event)
        self.assertEqual(attempts, 1)

    def test_custom_reply_templates_are_extensible_but_cannot_override_builtin(self):
        commands = self._commands({"!магазин": "Режим работы: ежедневно."})
        result = commands.handle(text="!магазин", chat_id="buyer-chat-1", deal_id="deal-1")
        self.assertEqual(result.message, "Режим работы: ежедневно.")
        with self.assertRaises(ValueError):
            self._commands({"!статус": "override"})

    def test_telegram_outbox_retries_api_failure_without_throwing_and_deduplicates(self):
        credentials = TelegramCredentialStore(backend=self.keyring)
        credentials.configure(bot_token="123456:secret", proxy_url="socks5h://proxy.example:1080", proxy_username="proxy-user", proxy_password="proxy-secret")
        calls = []
        def sender(token, payload, proxy):
            calls.append((token, payload, proxy))
            raise RuntimeError("API down")
        outbox = TelegramBotOutbox(self.db, credentials, sender, event_filters={"UNKNOWN": True})
        first = outbox.enqueue(
            event_type="UNKNOWN", chat_id="-100123", order_id="order-1",
            payload={"text": "unknown"}, idempotency_key="unknown:order-1",
        )
        duplicate = outbox.enqueue(
            event_type="UNKNOWN", chat_id="-100123", order_id="order-1",
            payload={"text": "unknown"}, idempotency_key="unknown:order-1",
        )
        self.assertEqual(first, duplicate)
        result = outbox.deliver_once()
        self.assertEqual(result[0]["status"], "retry")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]["url"], "socks5h://proxy.example:1080")
        self.assertEqual(calls[0][2]["username"], "proxy-user")
        with self.db.connect() as db:
            row = db.execute("SELECT status,attempts FROM telegram_outbox WHERE outbox_id=?", (first,)).fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("retry", 1))

    def test_pending_delivery_events_are_moved_to_filtered_telegram_outbox(self):
        credentials = TelegramCredentialStore(backend=self.keyring)
        credentials.configure(bot_token="123456:secret")
        outbox = TelegramBotOutbox(
            self.db, credentials, lambda *args: {"ok": True},
            event_filters={"unknown": True, "support_request": False},
        )
        with self.db.connect() as db:
            db.execute(
                "INSERT INTO gd_delivery_events(event_id,order_id,event_type,payload_json) VALUES(?,?,?,?)",
                ("event:test-telegram", "order-1", "telegram_notification", json.dumps({"reason": "UNKNOWN"})),
            )
            db.execute(
                "INSERT INTO gd_delivery_events(event_id,order_id,event_type,payload_json) VALUES(?,?,?,?)",
                ("event:test-filtered", "order-1", "operator_support_request", json.dumps({"reason": "support"})),
            )
        self.assertEqual(outbox.enqueue_pending_delivery_events(operator_chat_id="-1001"), 1)
        with self.db.connect() as db:
            queued = db.execute("SELECT event_type,status FROM telegram_outbox WHERE order_id='order-1'").fetchall()
            statuses = {row["event_id"]: row["status"] for row in db.execute(
                "SELECT event_id,status FROM gd_delivery_events WHERE event_id LIKE 'event:test-%'"
            ).fetchall()}
        self.assertEqual([(row["event_type"], row["status"]) for row in queued], [("unknown", "pending")])
        self.assertEqual(statuses["event:test-telegram"], "queued")
        self.assertEqual(statuses["event:test-filtered"], "filtered")

    def test_telegram_operator_reply_routes_only_allowlisted_reply_to_bound_deal(self):
        credentials = TelegramCredentialStore(backend=self.keyring)
        credentials.configure(bot_token="123456:secret")
        outbox = TelegramBotOutbox(self.db, credentials, lambda *args: {"ok": True, "result": {"message_id": 555}})
        outbox.enqueue(
            event_type="operator_trade_notice", chat_id="-1001", order_id="order-1",
            payload={"chat_id": "-1001", "text": "Заказ order-1"},
            idempotency_key="operator-notice:order-1",
        )
        outbox.deliver_once()
        buyer_outbox = BuyerPlatformMessageOutbox(self.db)
        router = TelegramOperatorReplyRouter(
            self.db, allowed_user_ids={"1001"}, allowed_chat_ids={"-1001"},
            buyer_message_enqueue=buyer_outbox.enqueue,
        )
        self.assertFalse(router.handle_reply(
            from_user_id="2002", operator_chat_id="-1001",
            reply_to_message_id="555", text="Чек получен",
        ))
        self.assertTrue(router.handle_reply(
            from_user_id="1001", operator_chat_id="-1001",
            reply_to_message_id="555", text="Чек получен",
        ))
        with self.db.connect() as db:
            queued = db.execute(
                "SELECT deal_id,chat_id,message_text,status FROM gd_buyer_message_outbox"
            ).fetchone()
        self.assertEqual(
            (queued["deal_id"], queued["chat_id"], queued["message_text"], queued["status"]),
            ("deal-1", "buyer-chat-1", "Чек получен", "pending"),
        )


if __name__ == "__main__":
    unittest.main()
