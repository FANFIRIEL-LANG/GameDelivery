from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.chat_commands import BuyerChatCommandRegistry
from core.delivery.dispatcher import DeliveryDispatcher
from core.delivery.reconciliation import DeliveryReconciliation
from core.delivery.recipes import RecipeItem, SteamAssetSpec
from core.delivery.send_policy import DeliverySendPolicy
from integrations.asf.trade_credentials import SteamTradeCredentialStore
from integrations.telegram.outbox import (
    BuyerPlatformMessageOutbox, TelegramBotOutbox, TelegramCredentialStore,
    TelegramOperatorReplyRouter, BuyerPlatformMessageDispatcher, TelegramRuntimeCoordinator,
)
from integrations.playerok.sync import PlayerokDealSyncService


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
    def send_trade_offer(self, *, order_id, bot_name, trade_url, asset_ids, app_id=None, context_id=None):
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


class ChatCommandsAndTelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "delivery.sqlite3")
        self.ledger = DeliveryLedger(self.db)
        self.playerok_sync = PlayerokDealSyncService(self.db)
        self.asf = FakeASF()
        self.reconciliation = FakeReconciliation()
        self.keyring = MemoryKeyring()
        self.trade_credentials = SteamTradeCredentialStore(backend=self.keyring)
        self.trade_credentials.store_trade_url(
            "order-1", "https://steamcommunity.com/tradeoffer/new/?partner=123&token=trade_secret"
        )
        self.send_policy = DeliverySendPolicy(MemorySettings())
        self.send_policy.set_auto_send_enabled(True)
        self.dispatcher = DeliveryDispatcher(
            self.db, self.asf, self.trade_credentials, send_policy=self.send_policy
        )
        self._create_order()

    def tearDown(self):
        self.temp.cleanup()

    def _create_order(self, status="PRIVACY_WAIT"):
        item = RecipeItem(
            item_id="test-asset", position=1, kind="steam_asset", spec_version=1,
            quantity_per_order=1, required=True, stock_source_id=None,
            executor_key="steam_trade_offer",
            spec=SteamAssetSpec(
                "command-test", 730, "2", {"market_hash_name": "Skin"}, ("Bot-A",)
            ),
        )
        config = self.ledger.create_recipe_config_version(
            listing_id="listing-1", source_platform="playerok",
            external_listing_id="listing-external-1", recipe_items=[item],
        )
        self.ledger.create_deal(
            deal_id="deal-1", source_platform="playerok", external_deal_id="deal-ext-1",
            chat_id="buyer-chat-1", listing_id="listing-1",
            config_version_id=config["config_version_id"], platform_status="PAID",
            payload={"buyer_steam_id64": "76561197960265851",
                     "stored_trade_url": "https://steamcommunity.com/tradeoffer/new/?partner=123"},
        )
        self.ledger.create_order(order_id="order-1", deal_id="deal-1", total_quantity=1, status="reserved")
        with self.db.connect() as db:
            recipe = self.ledger.recipe_for_config_version(
                db, config["config_version_id"], allow_legacy=True
            )
        item = recipe.items[0]
        self.ledger.add_order_line(
            order_line_id="line-1", order_id="order-1", line_number=1,
            item_type=item.kind, item_name=item.display_name,
            item_selector={
                "config_version_id": config["config_version_id"],
                "recipe_item_id": item.item_id, "recipe_item": item.to_dict(),
            }, quantity=item.quantity_per_order,
        )
        self.ledger.add_delivery_leg(
            leg_id="leg-1", order_line_id="line-1", bot_name="Bot-A",
            app_id=730, context_id=2, asset_ids=["asset-1"], status="reserved",
        )
        with self.db.connect() as db:
            db.execute("""
                INSERT INTO gd_resource_reservations
                    (reservation_id,owner_type,order_line_id,game_id,app_id,context_id,
                     bot_name,asset_id,units,state)
                VALUES ('reservation-1','order','line-1','cs2',730,'2','Bot-A','asset-1',1,'active')
            """)
        self.ledger.bind_order_recipient("order-1", "76561197960265851")
        with self.db.connect() as db:
            db.execute(
                "INSERT INTO playerok_sync_state(account_user_id,baseline_completed,baseline_completed_at) "
                "VALUES('seller-1',1,'2026-01-01T00:00:00+00:00')"
            )
            db.execute(
                "UPDATE gd_deals SET playerok_account_id='seller-1',listing_resolution_status='resolved',"
                "is_historical=0 WHERE deal_id='deal-1'"
            )
            db.execute("UPDATE gd_orders SET status=? WHERE order_id='order-1'", (status,))
            db.execute("UPDATE gd_delivery_legs SET status='PRIVACY_WAIT' WHERE leg_id='leg-1'")
        self.ledger.create_delivery_attempt(
            order_id="order-1", leg_id="leg-1", retry_generation=0,
            status="privacy_wait", result={"error": "private"},
        )

    def _commands(self, custom=None):
        return BuyerChatCommandRegistry(
            self.db, self.dispatcher, self.reconciliation,
            inventory_client=self.asf, custom_commands=custom,
        )

    def test_privacy_command_fails_closed_without_gui_browser_freshness_bridge(self):
        self.asf.inventory["Bot-A"] = {
            "Success": True, "Result": {"Bot-A": {"Assets": [{"assetid": "asset-1", "tradable": True}]}}
        }
        result = self._commands().handle(
            text="!настроил приватность", chat_id="buyer-chat-1",
            deal_id="deal-1", buyer_identity="76561197960265851", identity_verified=True,
        )
        self.assertEqual(result.status, "playerok_freshness_required")
        self.assertEqual(len(self.asf.sent), 0)
        self.assertIn("свежая проверка статуса", result.message)
        with self.db.connect() as db:
            attempt = db.execute(
                "SELECT retry_generation,idempotency_key FROM gd_delivery_attempts "
                "WHERE order_id='order-1' ORDER BY retry_generation DESC LIMIT 1"
            ).fetchone()
            leg_status = db.execute(
                "SELECT status FROM gd_delivery_legs WHERE leg_id='leg-1'"
            ).fetchone()[0]
        self.assertEqual(attempt["retry_generation"], 0)
        self.assertEqual(attempt["idempotency_key"], "order-1:leg-1:0")
        self.assertEqual(leg_status, "reserved")

    def test_privacy_command_rechecks_inventory_and_prepares_manual_send_while_auto_is_off(self):
        self.send_policy.set_auto_send_enabled(False)
        self.asf.inventory["Bot-A"] = {
            "Success": True, "Result": {"Bot-A": {"Assets": [{"assetid": "asset-1", "tradable": True}]}}
        }
        result = self._commands().handle(
            text="!настроил приватность", chat_id="buyer-chat-1",
            deal_id="deal-1", buyer_identity="76561197960265851", identity_verified=True,
        )

        self.assertEqual(result.status, "ready_for_manual_send")
        self.assertTrue(result.message)
        self.assertIn("Bot-A", self.asf.inventory)
        self.assertEqual(self.asf.sent, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute(
                "SELECT status FROM gd_orders WHERE order_id='order-1'"
            ).fetchone()[0], "reserved")
            self.assertEqual(db.execute(
                "SELECT status FROM gd_delivery_legs WHERE leg_id='leg-1'"
            ).fetchone()[0], "reserved")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'"
            ).fetchone()[0], 1)

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

    def test_telegram_outbox_rejects_unbound_enqueue_values(self):
        credentials = TelegramCredentialStore(backend=self.keyring)
        outbox = TelegramBotOutbox(self.db, credentials)
        with self.assertRaises(ValueError):
            outbox.enqueue(event_type=" ", chat_id="-1001", payload={})
        with self.assertRaises(ValueError):
            outbox.enqueue(event_type="unknown", chat_id=" ", payload={})
        with self.assertRaises(ValueError):
            outbox.enqueue(event_type="unknown", chat_id="-1001", payload=[])

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

    def test_telegram_outbox_stops_after_bounded_failures(self):
        credentials = TelegramCredentialStore(backend=self.keyring)
        credentials.configure(bot_token="123456:secret")
        outbox = TelegramBotOutbox(
            self.db, credentials,
            lambda *args: (_ for _ in ()).throw(RuntimeError("API down")),
        )
        outbox_id = outbox.enqueue(
            event_type="UNKNOWN", chat_id="-100123", order_id="order-1",
            payload={"text": "bounded"}, idempotency_key="bounded:order-1",
        )
        with self.db.connect() as db:
            db.execute("UPDATE telegram_outbox SET attempts=4 WHERE outbox_id=?", (outbox_id,))
        result = outbox.deliver_once()
        self.assertEqual(result[0]["status"], "failed")
        with self.db.connect() as db:
            row = db.execute("SELECT status,attempts,next_attempt_at FROM telegram_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        self.assertEqual((row["status"], row["attempts"], row["next_attempt_at"]), ("failed", 5, None))

    def test_stale_telegram_claim_is_recovered_after_worker_crash(self):
        credentials = TelegramCredentialStore(backend=self.keyring)
        credentials.configure(bot_token="123456:secret")
        calls = []
        outbox = TelegramBotOutbox(
            self.db, credentials,
            lambda token, payload, proxy: calls.append(payload) or {"ok": True},
        )
        outbox_id = outbox.enqueue(
            event_type="UNKNOWN", chat_id="-100123", order_id="order-1",
            payload={"text": "recovery"}, idempotency_key="recovery:order-1",
        )
        with self.db.connect() as db:
            db.execute(
                "UPDATE telegram_outbox SET status='sending',updated_at='2000-01-01T00:00:00.000Z' WHERE outbox_id=?",
                (outbox_id,),
            )
        result = outbox.deliver_once()
        self.assertEqual(result[0]["status"], "sent")
        self.assertEqual(len(calls), 1)
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM telegram_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()[0], "sent")

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


class TelegramBuyerBridgeTests(unittest.TestCase):
    def test_telegram_runtime_coordinator_is_disabled_without_token(self):
        from integrations.telegram.outbox import TelegramRuntimeCoordinator
        class EmptyCredentials:
            def get(self, name): return None
        from core.database.db import Database
        import tempfile
        temp = tempfile.TemporaryDirectory(); db = Database(temp.name + "/x.sqlite3")
        coordinator = TelegramRuntimeCoordinator(db, EmptyCredentials(), object())
        self.assertEqual(coordinator.tick(), {"status": "disabled", "processed": 0, "notifications": 0})
        temp.cleanup()

    def test_telegram_runtime_coordinator_persists_update_offset(self):
        from integrations.telegram.outbox import TelegramRuntimeCoordinator
        class Credentials:
            def get(self, name): return "bot-token" if name == "bot_token" else None
            def proxy(self): return None
        class Router:
            def __init__(self): self.calls = []
            def handle_reply(self, **kwargs): self.calls.append(kwargs); return True
        router = Router(); responses = []
        def api(token, method, payload=None, proxy=None):
            responses.append((method, payload))
            return {"ok": True, "result": [{"update_id": 7, "message": {"text": "x"}}]} if method == "getUpdates" else {"ok": True}
        from unittest.mock import patch
        with patch("integrations.telegram.outbox.telegram_api_request", side_effect=api):
            temp = tempfile.TemporaryDirectory(); db = Database(Path(temp.name) / "x.sqlite3")
            coordinator = TelegramRuntimeCoordinator(db, Credentials(), router, sender=lambda *args: {"ok": True})
            first = coordinator.tick(); second = coordinator.tick()
            with db.connect() as conn: offset = conn.execute("SELECT value FROM telegram_runtime_state WHERE key='update_offset'").fetchone()[0]
            self.assertEqual(first["status"], "ok"); self.assertEqual(second["status"], "ok"); self.assertEqual(offset, "8")
            self.assertEqual([payload["offset"] for method, payload in responses if method == "getUpdates"], [0, 8])
            temp.cleanup()

    def test_telegram_api_request_passes_proxy_without_exposing_credentials(self):
        from integrations.telegram.outbox import telegram_api_request
        class Response:
            def raise_for_status(self): pass
            def json(self): return {"ok": True, "result": {}}
        with patch("requests.post", return_value=Response()) as post:
            telegram_api_request("123:secret", "getMe", proxy={"url": "socks5h://proxy.example:1080", "username": "u", "password": "p"})
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["proxies"]["https"], "socks5h://u:p@proxy.example:1080")
        self.assertNotIn("secret", repr(kwargs["proxies"]))

    def test_buyer_dispatch_claim_blocks_retry_after_interruption(self):
        from core.database.db import Database
        import tempfile
        temp = tempfile.TemporaryDirectory(); db = Database(temp.name + "/x.sqlite3")
        with db.connect() as conn:
            conn.executescript("CREATE TABLE gd_deals(deal_id TEXT PRIMARY KEY,chat_id TEXT);")
            conn.execute("INSERT INTO gd_deals VALUES('d','chat-1')")
        outbox = BuyerPlatformMessageOutbox(db)
        outbox.enqueue('d', 'chat-1', 'hello')
        calls = []
        def interrupted_sender(chat, text, callback):
            calls.append((chat, text))  # callback is lost after dispatch began
        dispatcher = BuyerPlatformMessageDispatcher(db)
        self.assertEqual(dispatcher.dispatch_once(interrupted_sender), 1)
        self.assertEqual(dispatcher.dispatch_once(interrupted_sender), 0)
        self.assertEqual(calls, [('chat-1', 'hello')])
        with db.connect() as conn:
            status = conn.execute("SELECT status FROM gd_buyer_message_outbox").fetchone()[0]
        self.assertEqual(status, 'dispatching')
        temp.cleanup()

    def test_buyer_dispatch_unknown_result_is_terminal(self):
        from core.database.db import Database
        import tempfile
        temp = tempfile.TemporaryDirectory(); db = Database(temp.name + "/x.sqlite3")
        with db.connect() as conn:
            conn.executescript("CREATE TABLE gd_deals(deal_id TEXT PRIMARY KEY,chat_id TEXT);")
            conn.execute("INSERT INTO gd_deals VALUES('d','chat-1')")
        outbox = BuyerPlatformMessageOutbox(db)
        message_id = outbox.enqueue('d', 'chat-1', 'hello')
        calls = []
        def unknown_sender(chat, text, callback):
            calls.append(chat); callback(None, 'unknown_result')
        dispatcher = BuyerPlatformMessageDispatcher(db)
        self.assertEqual(dispatcher.dispatch_once(unknown_sender), 1)
        self.assertEqual(dispatcher.dispatch_once(unknown_sender), 0)
        with db.connect() as conn:
            status = conn.execute("SELECT status FROM gd_buyer_message_outbox WHERE message_id=?", (message_id,)).fetchone()[0]
        self.assertEqual((status, calls), ('unknown', ['chat-1']))
        temp.cleanup()

    def test_runtime_dispatch_remains_available_when_telegram_tick_failed(self):
        from core.database.db import Database
        import tempfile
        temp = tempfile.TemporaryDirectory(); db = Database(temp.name + "/x.sqlite3")
        with db.connect() as conn:
            conn.executescript("CREATE TABLE gd_deals(deal_id TEXT PRIMARY KEY,chat_id TEXT);")
            conn.execute("INSERT INTO gd_deals VALUES('d','chat-1')")
        outbox = BuyerPlatformMessageOutbox(db); outbox.enqueue('d', 'chat-1', 'hello')
        coordinator = TelegramRuntimeCoordinator(db, object(), object())
        calls = []
        adapter = lambda chat, text, callback: (calls.append((chat, text)), callback({'ok': True}, None))
        # MainWindow calls this path after its async tick callback, even when
        # the Telegram API operation reported an error.
        coordinator.dispatch_buyer_messages(adapter)
        with db.connect() as conn:
            status = conn.execute("SELECT status FROM gd_buyer_message_outbox").fetchone()[0]
        self.assertEqual((status, calls), ('sent', [('chat-1', 'hello')]))
        temp.cleanup()

    def test_runtime_dispatch_uses_supplied_buyer_adapter(self):
        from core.database.db import Database
        import tempfile
        temp = tempfile.TemporaryDirectory(); db = Database(temp.name + "/x.sqlite3")
        with db.connect() as conn:
            conn.executescript("CREATE TABLE gd_deals(deal_id TEXT PRIMARY KEY,chat_id TEXT);")
            conn.execute("INSERT INTO gd_deals VALUES('d','chat-1')")
        outbox = BuyerPlatformMessageOutbox(db)
        outbox.enqueue('d', 'chat-1', 'hello')
        coordinator = TelegramRuntimeCoordinator(db, object(), object())
        calls = []
        def ui_adapter(chat, text, callback):
            calls.append((chat, text)); callback({'ok': True}, None)
        self.assertEqual(coordinator.dispatch_buyer_messages(ui_adapter), 1)
        with db.connect() as conn:
            status = conn.execute("SELECT status FROM gd_buyer_message_outbox").fetchone()[0]
        self.assertEqual((status, calls), ('sent', [('chat-1', 'hello')]))
        temp.cleanup()

    def test_playerok_outbox_dispatch_uses_exact_chat_and_callback(self):
        from core.database.db import Database
        import tempfile
        temp = tempfile.TemporaryDirectory(); db = Database(temp.name + "/x.sqlite3")
        with db.connect() as conn:
            conn.executescript("CREATE TABLE gd_deals(deal_id TEXT PRIMARY KEY,chat_id TEXT); CREATE TABLE gd_buyer_message_outbox(message_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE,deal_id TEXT,chat_id TEXT,message_text TEXT,status TEXT,attempts INTEGER DEFAULT 0);")
            conn.execute("INSERT INTO gd_deals VALUES('d','chat-1')")
        from integrations.telegram.outbox import BuyerPlatformMessageOutbox
        outbox = BuyerPlatformMessageOutbox(db); message_id = outbox.enqueue('d','chat-1','hello')
        calls=[]
        def sender(chat,text,callback): calls.append((chat,text)); callback({'ok':True},None)
        self.assertEqual(BuyerPlatformMessageDispatcher(db).dispatch_once(sender),1)
        self.assertEqual(calls,[('chat-1','hello')])
        with db.connect() as conn: self.assertEqual(conn.execute("SELECT status FROM gd_buyer_message_outbox WHERE message_id=?",(message_id,)).fetchone()[0], 'sent')
        temp.cleanup()
