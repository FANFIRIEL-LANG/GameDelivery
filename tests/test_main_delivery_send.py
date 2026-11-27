import sqlite3
import threading
import tempfile
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QMessageBox

from app.main import MainWindow
from core.database.connection import connect_sqlite
from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.send_policy import DeliverySendPolicy
from core.delivery.playerok_freshness import PlayerokFreshnessGate
from tests.playerok_freshness_fixtures import reader_completed_snapshot
from integrations.playerok.sync import PlayerokDealSyncService


class DeliverySendFlowTests(unittest.TestCase):
    ORDER_ID = "GD-SEND-TEST"
    BOT_NAME = "bot-a"
    ASSET_ID = "123"
    TRADE_URL = "https://steamcommunity.com/tradeoffer/new/?partner=12345&token=Tok-abc_1"
    SAFE_TRADE_URL = "https://steamcommunity.com/tradeoffer/new/?partner=12345"
    STEAM_ID64 = 76561197960265728 + 12345

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

    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def _database(self, path: Path):
        connection = sqlite3.connect(path)
        try:
            db = connection
            db.execute("""
                CREATE TABLE delivery_orders (
                    order_id TEXT PRIMARY KEY,
                    recipient_trade_url TEXT NOT NULL,
                    recipient_steam_id64 TEXT,
                    status TEXT NOT NULL,
                    source_platform TEXT NOT NULL DEFAULT 'manual',
                    source_order_id TEXT,
                    source_deal_id TEXT,
                    source_status TEXT NOT NULL DEFAULT '',
                    trade_offer_id TEXT NOT NULL DEFAULT '',
                    trade_confirmation_status TEXT NOT NULL DEFAULT ''
                )
            """)
            db.execute("""
                CREATE TABLE delivery_order_assets (
                    order_id TEXT NOT NULL,
                    bot_name TEXT NOT NULL,
                    asset_id TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE steam_asset_reservations (
                    bot_name TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    PRIMARY KEY (bot_name, asset_id)
                )
            """)
            db.execute(
                "INSERT INTO delivery_orders "
                "(order_id, recipient_trade_url, recipient_steam_id64, status) VALUES (?, ?, ?, ?)",
                (self.ORDER_ID, self.SAFE_TRADE_URL, str(self.STEAM_ID64), "reserved — не отправлен"),
            )
            db.execute(
                "INSERT INTO delivery_order_assets (order_id, bot_name, asset_id) VALUES (?, ?, ?)",
                (self.ORDER_ID, self.BOT_NAME, self.ASSET_ID),
            )
            db.execute(
                "INSERT INTO steam_asset_reservations (bot_name, asset_id, order_id) VALUES (?, ?, ?)",
                (self.BOT_NAME, self.ASSET_ID, self.ORDER_ID),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _query(path: Path, sql: str, params=(), *, all_rows=False):
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute(sql, params)
            return cursor.fetchall() if all_rows else cursor.fetchone()
        finally:
            connection.close()

    def _fake_window(self, path: Path):
        status_bar = MagicMock()
        window = SimpleNamespace(
            _database_path=path,
            _delivery_send_policy=DeliverySendPolicy(self.MemorySettings()),
            asf_management=SimpleNamespace(
                ipc_password=SimpleNamespace(text=lambda: ""),
                _client=MagicMock(),
            ),
            _selected_order_id=lambda: self.ORDER_ID,
            _validate_trade_url=lambda value: value,
            _trade_credentials=SimpleNamespace(
                load_trade_url=lambda order_id, stored_url: self.TRADE_URL,
                recipient_steam_id64_from_url=lambda stored_url: self.STEAM_ID64,
                delete_trade_url=lambda order_id: None,
            ),
            _delete_trade_credential_best_effort=lambda order_id: None,
            _load_saved_orders=MagicMock(),
            _load_order_assets=MagicMock(),
            _update_order_action_buttons=MagicMock(),
            send_offer_button=MagicMock(),
            statusBar=lambda: status_bar,
            _status_bar=status_bar,
        )
        window._bind_legacy_order_recipient_snapshot = MethodType(
            MainWindow._bind_legacy_order_recipient_snapshot, window
        )
        window._claim_legacy_order_for_send = MethodType(
            MainWindow._claim_legacy_order_for_send, window
        )
        window._dispatch_legacy_after_freshness = MethodType(
            MainWindow._dispatch_legacy_after_freshness, window
        )
        window._legacy_playerok_identity = MethodType(
            MainWindow._legacy_playerok_identity, window
        )
        window._finish_async_send_ui = MethodType(MainWindow._finish_async_send_ui, window)
        return window

    @staticmethod
    def _preview(order_id, bot_name, steam_id, assets):
        return {
            "success": True,
            "mode": "preview-only",
            "sendEnabled": False,
            "orderId": order_id,
            "botName": bot_name,
            "targetSteamId64": steam_id,
            "assetCount": len(assets),
            "assets": [{"assetId": value} for value in assets],
        }

    def test_confirmed_send_persists_offer_id_and_pending_state(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            window = self._fake_window(db_path)
            client = window.asf_management._client.return_value
            client.preview_trade_offer.return_value = self._preview(
                self.ORDER_ID, self.BOT_NAME, self.STEAM_ID64, [self.ASSET_ID]
            )
            client.send_trade_offer.return_value = {
                "success": True,
                "mode": "sent",
                "sendEnabled": True,
                "orderId": self.ORDER_ID,
                "botName": self.BOT_NAME,
                "targetSteamId64": self.STEAM_ID64,
                "assetCount": 1,
                "assets": [{"assetId": self.ASSET_ID}],
                "offerIds": ["456789"],
                "confirmationStatus": "pending",
                "tradeAccepted": False,
            }

            with patch("app.main.ASFClient", return_value=client):
                with patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                    with patch("app.main.QMessageBox.information"):
                        with patch("app.main.QMessageBox.warning") as warning:
                            MainWindow._send_selected_order_with_asf(window)

            client.preview_trade_offer.assert_called_once_with(
                order_id=self.ORDER_ID,
                bot_name=self.BOT_NAME,
                trade_url=self.TRADE_URL,
                asset_ids=[self.ASSET_ID],
            )
            client.send_trade_offer.assert_called_once_with(
                order_id=self.ORDER_ID,
                bot_name=self.BOT_NAME,
                trade_url=self.TRADE_URL,
                asset_ids=[self.ASSET_ID],
            )
            with connect_sqlite(db_path) as db:
                row = db.execute(
                    "SELECT status, trade_offer_id, trade_confirmation_status "
                    "FROM delivery_orders WHERE order_id = ?",
                    (self.ORDER_ID,),
                ).fetchone()
            self.assertEqual(tuple(row), ("sent — требуется подтверждение Steam", "456789", "pending"))
            warning.assert_not_called()

    def test_non_paid_legacy_playerok_order_stops_before_asf_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            with connect_sqlite(db_path) as db:
                db.execute(
                    "UPDATE delivery_orders SET source_platform='playerok',source_order_id='deal-1',"
                    "source_deal_id='deal-1',source_status='SENT' WHERE order_id=?",
                    (self.ORDER_ID,),
                )
            window = self._fake_window(db_path)
            client = window.asf_management._client.return_value

            with patch("app.main.ASFClient", return_value=client), \
                 patch("app.main.QMessageBox.warning") as warning:
                MainWindow._send_selected_order_with_asf(window)

            warning.assert_called_once()
            client.preview_trade_offer.assert_not_called()
            client.send_trade_offer.assert_not_called()
            with connect_sqlite(db_path) as db:
                self.assertEqual(db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id=?", (self.ORDER_ID,)
                ).fetchone()[0], "reserved — не отправлен")

    def test_legacy_sync_playerok_path_refuses_even_when_local_status_is_paid(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            db = Database(db_path)
            PlayerokDealSyncService(db)
            ledger = DeliveryLedger(db)
            config = ledger.create_listing_config_version(
                listing_id="listing-1", source_platform="playerok",
                external_listing_id="external-listing-1", delivery_quantity_per_deal=1,
                item_selectors={"app_id": 730, "context_id": 2}, bot_pool=[self.BOT_NAME],
            )
            ledger.create_deal(
                deal_id="canonical-deal-1", source_platform="playerok",
                external_deal_id="deal-1", chat_id="chat-1",
                listing_id=config["listing_id"], config_version_id=config["config_version_id"],
                platform_status="PAID",
            )
            with db.connect() as conn:
                conn.execute(
                    "UPDATE gd_deals SET playerok_account_id='account-1',is_historical=0 "
                    "WHERE deal_id='canonical-deal-1'"
                )
                conn.execute(
                    "UPDATE delivery_orders SET source_platform='playerok',source_order_id='deal-1',"
                    "source_deal_id='deal-1',source_status='PAID' WHERE order_id=?",
                    (self.ORDER_ID,),
                )
            window = self._fake_window(db_path)
            client = window.asf_management._client.return_value

            with patch("app.main.QMessageBox.warning") as warning:
                MainWindow._send_selected_order_with_asf(window)

            warning.assert_called_once()
            self.assertIn("Legacy-путь Playerok отключён", warning.call_args.args[2])
            client.preview_trade_offer.assert_not_called()
            client.send_trade_offer.assert_not_called()

    def test_legacy_claim_rechecks_playerok_status_after_preview_before_send(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            with connect_sqlite(db_path) as db:
                db.execute(
                    "UPDATE delivery_orders SET source_platform='playerok',source_order_id='deal-1',"
                    "source_deal_id='deal-1',source_status='SENT' WHERE order_id=?",
                    (self.ORDER_ID,),
                )
            window = self._fake_window(db_path)
            window._delivery_send_policy.set_auto_send_enabled(True)
            client = window.asf_management._client.return_value
            assets = self._query(
                db_path,
                "SELECT bot_name,asset_id FROM delivery_order_assets WHERE order_id=? "
                "ORDER BY bot_name,asset_id", (self.ORDER_ID,), all_rows=True,
            )

            with patch("app.main.QMessageBox.warning") as warning:
                MainWindow._on_async_trade_preview(
                    window, self.ORDER_ID, self.BOT_NAME, self.TRADE_URL,
                    [self.ASSET_ID], assets, client,
                    self._preview(self.ORDER_ID, self.BOT_NAME, self.STEAM_ID64, [self.ASSET_ID]),
                    None, automatic=True,
                )

            warning.assert_called_once()
            client.send_trade_offer.assert_not_called()
            with connect_sqlite(db_path) as db:
                self.assertEqual(db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id=?", (self.ORDER_ID,)
                ).fetchone()[0], "reserved — не отправлен")

    def test_manual_delivery_asks_once_for_the_whole_multi_leg_order(self):
        policy = DeliverySendPolicy(self.MemorySettings())
        dispatched = []
        completed = []

        class Dispatcher:
            def dispatch_leg(_self, **kwargs):
                dispatched.append((kwargs["leg_id"], kwargs["manual_authorization"]))
                self.assertTrue(policy.run_manual_dispatch_claim(
                    kwargs["manual_authorization"], kwargs["order_id"], kwargs["leg_id"],
                    lambda: {"_claimed": True},
                )[0])
                return {"dispatched": True, "status": "SENT_TO_STEAM"}

        window = SimpleNamespace(
            _delivery_send_policy=policy,
            _trade_credentials=SimpleNamespace(
                store_trade_url=lambda order, url: "safe-url",
                recipient_steam_id64_from_url=lambda url: 76561197960265728 + 12345,
            ),
            delivery_pipeline=SimpleNamespace(
                ledger=SimpleNamespace(bind_order_recipient=MagicMock())
            ),
            _database_path=":temporary:",
            _playerok_freshness_gate=None,
            _finish_async_send_ui=MagicMock(),
            _on_ledger_dispatch_complete=lambda order, result, error: completed.append(
                (order, result, error)
            ),
            statusBar=lambda: MagicMock(),
            _run_asf_call_async=lambda operation, callback: callback(operation(), None),
        )
        legs = [{"leg_id": "leg-a"}, {"leg_id": "leg-b"}]
        previews = [
            {"leg_id": "leg-a", "bot_name": "Bot A", "app_id": 753, "context_id": 6,
             "asset_ids": ["asset-a"], "steam_id64": "7656119"},
            {"leg_id": "leg-b", "bot_name": "Bot B", "app_id": 730, "context_id": 2,
             "asset_ids": ["asset-b"], "steam_id64": "7656119"},
        ]

        with patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as question, \
             patch("app.main.Database"), \
             patch("app.main.DeliveryDispatcher", return_value=Dispatcher()):
            MainWindow._on_ledger_previews_ready(
                window, "order-1", "https://steamcommunity.com/tradeoffer/new/",
                legs, object(), previews, None, automatic=False,
            )

        question.assert_called_once()
        self.assertEqual(question.call_args.args[1], "Подтвердить отправку всего заказа")
        self.assertEqual([leg for leg, _grant in dispatched], ["leg-a", "leg-b"])
        self.assertIs(dispatched[0][1], dispatched[1][1])
        self.assertEqual(completed[0][0], "order-1")
        self.assertEqual(len(completed[0][1]), 2)

    def test_manual_order_continues_other_legs_after_one_leg_exception(self):
        policy = DeliverySendPolicy(self.MemorySettings())
        attempted = []
        completed = []

        class Dispatcher:
            def dispatch_leg(_self, **kwargs):
                attempted.append(kwargs["leg_id"])
                if kwargs["leg_id"] == "leg-a":
                    raise TimeoutError("simulated transport timeout")
                return {"dispatched": True, "status": "SENT_TO_STEAM"}

        window = SimpleNamespace(
            _delivery_send_policy=policy,
            _trade_credentials=SimpleNamespace(store_trade_url=lambda order, url: "safe-url"),
            delivery_pipeline=SimpleNamespace(
                ledger=SimpleNamespace(bind_order_recipient=MagicMock())
            ),
            _database_path=":temporary:", _playerok_freshness_gate=None,
            _finish_async_send_ui=MagicMock(),
            _on_ledger_dispatch_complete=lambda order, result, error: completed.append(result),
            statusBar=lambda: MagicMock(),
            _run_asf_call_async=lambda operation, callback: callback(operation(), None),
        )
        legs = [{"leg_id": "leg-a"}, {"leg_id": "leg-b"}]
        previews = [
            {"leg_id": "leg-a", "bot_name": "Bot A", "app_id": 753,
             "context_id": 6, "asset_ids": ["asset-a"], "steam_id64": "7656119"},
            {"leg_id": "leg-b", "bot_name": "Bot B", "app_id": 730,
             "context_id": 2, "asset_ids": ["asset-b"], "steam_id64": "7656119"},
        ]

        with patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as question, \
             patch("app.main.Database"), \
             patch("app.main.DeliveryDispatcher", return_value=Dispatcher()):
            MainWindow._on_ledger_previews_ready(
                window, "order-1", "https://steamcommunity.com/tradeoffer/new/",
                legs, object(), previews, None, automatic=False,
            )

        question.assert_called_once()
        self.assertEqual(attempted, ["leg-a", "leg-b"])
        self.assertEqual(completed[0][0]["status"], "UNKNOWN")
        self.assertEqual(completed[0][1]["status"], "SENT_TO_STEAM")

    def test_canonical_playerok_flow_refreshes_each_leg_and_stops_after_second_check_fails(self):
        gate = PlayerokFreshnessGate()
        lease = gate.try_acquire_order("order-1")
        policy = DeliverySendPolicy(self.MemorySettings())
        grant = policy.issue_order_manual_authorization("order-1", ("leg-1", "leg-2"))
        checks = []
        dispatches = []
        parent_results = []
        completed = []
        legs = [{"leg_id": "leg-1"}, {"leg_id": "leg-2"}]
        identity = {"account_user_id": "account-1", "deal_id": "deal-1"}

        class Dispatcher:
            def record_order_leg_outcome(_self, **kwargs):
                parent_results.append(kwargs)

            def dispatch_leg(self, **kwargs):
                dispatches.append((kwargs["leg_id"], kwargs["manual_authorization"]))
                policy.consume_manual_authorization(
                    kwargs["manual_authorization"], "order-1", kwargs["leg_id"]
                )
                return {"dispatched": True, "status": "SENT_TO_STEAM"}

        window = SimpleNamespace(
            _playerok_freshness_gate=gate,
            _delivery_send_policy=policy,
            _request_playerok_freshness=None,
            _run_asf_call_async=lambda operation, callback: callback(operation(), None),
            statusBar=lambda: MagicMock(),
            _on_ledger_dispatch_complete=lambda order, results, error: completed.append((order, results, error)),
        )

        def request(order, leg, expected, current_lease, callback):
            checks.append(leg)
            if leg == "leg-2":
                callback(None, "browser timeout")
                return
            proof = gate.issue_from_complete_snapshot(
                reader_completed_snapshot("account-1", [{"deal_id": "deal-1", "status": "PAID"}]),
                expected_account_user_id="account-1", deal_id="deal-1",
                order_id=order, leg_id=leg, lease=current_lease,
            )
            callback(proof, None)

        window._request_playerok_freshness = request
        window._dispatch_playerok_legs_sequentially = MethodType(
            MainWindow._dispatch_playerok_legs_sequentially, window
        )
        try:
            MainWindow._dispatch_playerok_legs_sequentially(
                window, "order-1", "https://steamcommunity.com/tradeoffer/new/",
                legs, Dispatcher(), "safe-url", identity, lease, grant, 0, [],
            )
        finally:
            gate.release_order(lease)

        self.assertEqual(checks, ["leg-1", "leg-2"])
        self.assertEqual(dispatches, [("leg-1", grant)])
        self.assertEqual(parent_results[0]["leg_id"], "leg-2")
        self.assertEqual(parent_results[0]["result"]["status"], "PLAYEROK_FRESHNESS_REQUIRED")
        self.assertEqual(completed[0][1][0]["status"], "SENT_TO_STEAM")
        self.assertEqual(completed[0][1][1]["status"], "PLAYEROK_FRESHNESS_REQUIRED")

    def test_canonical_playerok_flow_checks_each_successful_leg_separately(self):
        gate = PlayerokFreshnessGate()
        lease = gate.try_acquire_order("order-1")
        policy = DeliverySendPolicy(self.MemorySettings())
        grant = policy.issue_order_manual_authorization("order-1", ("leg-1", "leg-2"))
        checks = []
        dispatches = []
        completed = []
        identity = {"account_user_id": "account-1", "deal_id": "deal-1"}

        class Dispatcher:
            def dispatch_leg(self, **kwargs):
                dispatches.append((kwargs["leg_id"], kwargs["manual_authorization"]))
                policy.consume_manual_authorization(
                    kwargs["manual_authorization"], "order-1", kwargs["leg_id"]
                )
                return {"dispatched": True, "status": "SENT_TO_STEAM"}

        window = SimpleNamespace(
            _playerok_freshness_gate=gate,
            _delivery_send_policy=policy,
            _run_asf_call_async=lambda operation, callback: callback(operation(), None),
            statusBar=lambda: MagicMock(),
            _on_ledger_dispatch_complete=lambda order, results, error: completed.append(results),
        )

        def request(order, leg, expected, current_lease, callback):
            checks.append(leg)
            proof = gate.issue_from_complete_snapshot(
                reader_completed_snapshot("account-1", [{"deal_id": "deal-1", "status": "PAID"}]),
                expected_account_user_id="account-1", deal_id="deal-1",
                order_id=order, leg_id=leg, lease=current_lease,
            )
            callback(proof, None)

        window._request_playerok_freshness = request
        window._dispatch_playerok_legs_sequentially = MethodType(
            MainWindow._dispatch_playerok_legs_sequentially, window
        )
        try:
            MainWindow._dispatch_playerok_legs_sequentially(
                window, "order-1", "https://steamcommunity.com/tradeoffer/new/",
                [{"leg_id": "leg-1"}, {"leg_id": "leg-2"}], Dispatcher(),
                "safe-url", identity, lease, grant, 0, [],
            )
        finally:
            gate.release_order(lease)

        self.assertEqual(checks, ["leg-1", "leg-2"])
        self.assertEqual(dispatches, [("leg-1", grant), ("leg-2", grant)])
        self.assertEqual(len(completed[0]), 2)

    def test_browser_freshness_request_is_started_on_qt_gui_thread(self):
        called = []

        class Panel:
            def read_sales_for_delivery(self, account_id, callback):
                called.append((account_id, QThread.currentThread()))
                callback(None, "test failure")

        window = SimpleNamespace(marketplace_panel=Panel())
        results = []
        MainWindow._request_playerok_freshness(
            window, "order-1", "leg-1",
            {"account_user_id": "account-1", "deal_id": "deal-1"},
            object(), lambda proof, error: results.append((proof, error)),
        )
        self.assertEqual(called[0][0], "account-1")
        self.assertEqual(called[0][1], self.qt_app.thread())
        self.assertIsNone(results[0][0])
        self.assertEqual(results[0][1], "test failure")

    def test_browser_freshness_accepts_reader_issued_snapshot_object(self):
        gate = PlayerokFreshnessGate()
        lease = gate.try_acquire_order("order-1")
        snapshot = reader_completed_snapshot("account-1", [
            {"deal_id": "deal-1", "status": "PAID"},
        ])
        class DatabaseContext:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def execute(self, *_args):
                return self
            def fetchone(self):
                return {
                    "platform_status": "PAID",
                    "is_historical": 0,
                    "playerok_account_id": "account-1",
                }

        class Panel:
            def read_sales_for_delivery(self, account_id, callback):
                self.account_id = account_id
                callback(snapshot, None)

        panel = Panel()
        window = SimpleNamespace(
            marketplace_panel=panel,
            _playerok_freshness_gate=gate,
            _database_path=":memory:",
        )
        results = []
        with patch("app.main.connect_sqlite", return_value=DatabaseContext()):
            MainWindow._request_playerok_freshness(
                window, "order-1", "leg-1",
                {"account_user_id": "account-1", "deal_id": "deal-1"},
                lease, lambda proof, error: results.append((proof, error)),
            )
        try:
            self.assertEqual(panel.account_id, "account-1")
            self.assertIsNone(results[0][1])
            self.assertIsNotNone(results[0][0])
        finally:
            gate.release_order(lease)

    def test_browser_freshness_request_from_worker_is_rejected_without_webengine_call(self):
        class Panel:
            def read_sales_for_delivery(self, *_args):
                raise AssertionError("WebEngine must not be called from a worker thread")

        window = SimpleNamespace(marketplace_panel=Panel())
        results = []
        worker = threading.Thread(target=lambda: MainWindow._request_playerok_freshness(
            window, "order-1", "leg-1",
            {"account_user_id": "account-1", "deal_id": "deal-1"},
            object(), lambda proof, error: results.append((proof, error)),
        ))
        worker.start()
        worker.join()
        self.assertIsNone(results[0][0])
        self.assertIn("GUI-потока", results[0][1])

    def test_async_send_finish_releases_order_lease(self):
        gate = PlayerokFreshnessGate()
        lease = gate.try_acquire_order("order-1")
        window = SimpleNamespace(
            _playerok_freshness_gate=gate,
            _active_send_lease=lease,
            _active_send_identity={"deal_id": "deal-1"},
            _active_send_order_id="order-1",
            send_offer_button=MagicMock(),
            _load_saved_orders=MagicMock(),
            _update_order_action_buttons=MagicMock(),
        )
        MainWindow._finish_async_send_ui(window)
        replacement = gate.try_acquire_order("order-1")
        self.assertIsNotNone(replacement)
        self.assertIsNone(window._active_send_lease)
        self.assertIsNone(window._active_send_identity)
        gate.release_order(replacement)

    def test_canonical_playerok_guard_reason_is_shown_to_user(self):
        window = SimpleNamespace(
            _finish_async_send_ui=MagicMock(),
            _load_saved_orders=MagicMock(),
        )
        with patch("app.main.QMessageBox.warning") as warning, \
             patch("app.main.QMessageBox.information") as information:
            MainWindow._on_ledger_dispatch_complete(
                window, "order-1", [{
                    "dispatched": False,
                    "status": "PLAYEROK_NOT_ELIGIBLE",
                    "reason": "Статус сделки Playerok изменился.",
                }], None,
            )

        warning.assert_called_once()
        self.assertIn("Steam offers не создавались", warning.call_args.args[2])
        self.assertIn("Статус сделки Playerok изменился", warning.call_args.args[2])
        information.assert_not_called()

    def test_auto_dispatch_off_blocks_post_preflight_send_and_preserves_reservation(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            window = self._fake_window(db_path)
            client = window.asf_management._client.return_value
            preview = self._preview(self.ORDER_ID, self.BOT_NAME, self.STEAM_ID64, [self.ASSET_ID])
            assets = self._query(
                db_path,
                "SELECT bot_name,asset_id FROM delivery_order_assets WHERE order_id=? "
                "ORDER BY bot_name,asset_id", (self.ORDER_ID,), all_rows=True,
            )

            with patch("app.main.QMessageBox.question") as question:
                with patch("app.main.QMessageBox.warning"):
                    MainWindow._on_async_trade_preview(
                        window, self.ORDER_ID, self.BOT_NAME, self.TRADE_URL,
                        [self.ASSET_ID], assets, client, preview, None, automatic=True,
                    )

            question.assert_not_called()
            client.send_trade_offer.assert_not_called()
            row = self._query(
                db_path, "SELECT status FROM delivery_orders WHERE order_id=?", (self.ORDER_ID,)
            )
            reservation = self._query(
                db_path, "SELECT COUNT(*) FROM steam_asset_reservations WHERE order_id=?", (self.ORDER_ID,)
            )[0]
            self.assertEqual(row[0], "reserved — не отправлен")
            self.assertEqual(reservation, 1)

    def test_auto_dispatch_on_requires_successful_preview_and_skips_manual_dialog(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            window = self._fake_window(db_path)
            window._delivery_send_policy.set_auto_send_enabled(True)
            client = window.asf_management._client.return_value
            preview = self._preview(self.ORDER_ID, self.BOT_NAME, self.STEAM_ID64, [self.ASSET_ID])
            assets = self._query(
                db_path,
                "SELECT bot_name,asset_id FROM delivery_order_assets WHERE order_id=? "
                "ORDER BY bot_name,asset_id", (self.ORDER_ID,), all_rows=True,
            )
            window._run_asf_call_async = MagicMock()

            with patch("app.main.QMessageBox.question") as question:
                MainWindow._on_async_trade_preview(
                    window, self.ORDER_ID, self.BOT_NAME, self.TRADE_URL,
                    [self.ASSET_ID], assets, client, preview, None,
                    automatic=True,
                )

            question.assert_not_called()
            window._run_asf_call_async.assert_called_once()
            status = self._query(
                db_path, "SELECT status FROM delivery_orders WHERE order_id=?", (self.ORDER_ID,)
            )[0]
            self.assertEqual(status, "sending — запрос ASF")

    def test_unknown_result_locks_order_and_does_not_reset_for_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            window = self._fake_window(db_path)
            client = window.asf_management._client.return_value
            client.preview_trade_offer.return_value = self._preview(
                self.ORDER_ID, self.BOT_NAME, self.STEAM_ID64, [self.ASSET_ID]
            )
            client.send_trade_offer.return_value = {
                "success": False,
                "mode": "send",
                "sendEnabled": True,
                "resultUnknown": True,
                "error": "Do not retry",
            }

            with patch("app.main.ASFClient", return_value=client):
                with patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                    with patch("app.main.QMessageBox.warning") as warning:
                        MainWindow._send_selected_order_with_asf(window)

            with connect_sqlite(db_path) as db:
                status = db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id = ?",
                    (self.ORDER_ID,),
                ).fetchone()[0]
            self.assertEqual(status, "неизвестен результат отправки — проверь исходящие Steam")
            self.assertEqual(warning.call_count, 1)
            client.send_trade_offer.assert_called_once()

    def test_unknown_result_persists_known_offer_ids_for_manual_inspection(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            window = self._fake_window(db_path)
            client = window.asf_management._client.return_value
            client.preview_trade_offer.return_value = self._preview(
                self.ORDER_ID, self.BOT_NAME, self.STEAM_ID64, [self.ASSET_ID]
            )
            client.send_trade_offer.return_value = {
                "success": False,
                "mode": "send",
                "sendEnabled": True,
                "resultUnknown": True,
                "orderId": self.ORDER_ID,
                "botName": self.BOT_NAME,
                "targetSteamId64": self.STEAM_ID64,
                "assetCount": 1,
                "assets": [{"assetId": self.ASSET_ID}],
                "offerIds": ["456789", "123456"],
                "error": "ASF could not confirm full success.",
            }

            with patch("app.main.ASFClient", return_value=client):
                with patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                    with patch("app.main.QMessageBox.warning"):
                        MainWindow._send_selected_order_with_asf(window)

            with connect_sqlite(db_path) as db:
                row = db.execute(
                    "SELECT status, trade_offer_id, trade_confirmation_status "
                    "FROM delivery_orders WHERE order_id = ?",
                    (self.ORDER_ID,),
                ).fetchone()
            self.assertEqual(
                tuple(row),
                ("неизвестен результат отправки — проверь исходящие Steam", "123456, 456789", "unknown"),
            )
            client.send_trade_offer.assert_called_once()

    def test_unknown_send_result_displays_full_asf_exception_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            window = self._fake_window(db_path)
            client = window.asf_management._client.return_value
            client.preview_trade_offer.return_value = self._preview(
                self.ORDER_ID, self.BOT_NAME, self.STEAM_ID64, [self.ASSET_ID]
            )
            client.send_trade_offer.return_value = {
                "success": False,
                "mode": "send",
                "sendEnabled": True,
                "resultUnknown": True,
                "error": "ASF send failed.",
                "exceptionType": "System.MissingMethodException",
                "exceptionMessage": "Missing method details",
                "missingMethod": "MissingMethod details",
                "targetSite": "ArchiWebHandler.SendTradeOffer",
                "stackTrace": "at TradeDelivery.ProcessSendCommand()",
            }

            with patch("app.main.ASFClient", return_value=client):
                with patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                    with patch("app.main.QMessageBox.warning") as warning:
                        MainWindow._send_selected_order_with_asf(window)

            message = warning.call_args.args[2]
            self.assertIn("System.MissingMethodException", message)
            self.assertIn("MissingMethod details", message)
            self.assertIn("ArchiWebHandler.SendTradeOffer", message)
            self.assertIn("at TradeDelivery.ProcessSendCommand()", message)

    def test_cancel_confirmation_does_not_send_or_change_order(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            window = self._fake_window(db_path)
            client = window.asf_management._client.return_value
            client.preview_trade_offer.return_value = self._preview(
                self.ORDER_ID, self.BOT_NAME, self.STEAM_ID64, [self.ASSET_ID]
            )

            with patch("app.main.ASFClient", return_value=client):
                with patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
                    MainWindow._send_selected_order_with_asf(window)

            client.send_trade_offer.assert_not_called()
            with connect_sqlite(db_path) as db:
                status = db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id = ?",
                    (self.ORDER_ID,),
                ).fetchone()[0]
            self.assertEqual(status, "reserved — не отправлен")


    def test_missing_reservation_blocks_send_before_live_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            with connect_sqlite(db_path) as db:
                db.execute(
                    "DELETE FROM steam_asset_reservations WHERE order_id = ?",
                    (self.ORDER_ID,),
                )
            window = self._fake_window(db_path)
            client = window.asf_management._client.return_value

            with patch("app.main.ASFClient", return_value=client):
                with patch("app.main.QMessageBox.warning") as warning:
                    MainWindow._send_selected_order_with_asf(window)

            client.preview_trade_offer.assert_not_called()
            client.send_trade_offer.assert_not_called()
            warning.assert_called_once()
            with connect_sqlite(db_path) as db:
                status = db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id = ?",
                    (self.ORDER_ID,),
                ).fetchone()[0]
            self.assertEqual(status, "reserved — не отправлен")

    def test_reservation_removed_after_confirmation_blocks_send(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            window = self._fake_window(db_path)
            client = window.asf_management._client.return_value
            client.preview_trade_offer.return_value = self._preview(
                self.ORDER_ID, self.BOT_NAME, self.STEAM_ID64, [self.ASSET_ID]
            )

            def remove_reservation_then_confirm(*args, **kwargs):
                with connect_sqlite(db_path) as db:
                    db.execute(
                        "DELETE FROM steam_asset_reservations WHERE order_id = ?",
                        (self.ORDER_ID,),
                    )
                return QMessageBox.StandardButton.Yes

            with patch("app.main.ASFClient", return_value=client):
                with patch(
                    "app.main.QMessageBox.question",
                    side_effect=remove_reservation_then_confirm,
                ):
                    with patch("app.main.QMessageBox.warning") as warning:
                        MainWindow._send_selected_order_with_asf(window)

            client.preview_trade_offer.assert_called_once()
            client.send_trade_offer.assert_not_called()
            warning.assert_called_once()
            with connect_sqlite(db_path) as db:
                status = db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id = ?",
                    (self.ORDER_ID,),
                ).fetchone()[0]
            self.assertEqual(status, "reserved — не отправлен")

    def test_local_secret_file_failure_leaves_order_retryable(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            window = self._fake_window(db_path)
            client = window.asf_management._client.return_value
            client.preview_trade_offer.return_value = self._preview(
                self.ORDER_ID, self.BOT_NAME, self.STEAM_ID64, [self.ASSET_ID]
            )
            client.send_trade_offer.side_effect = OSError("Cannot write local one-use secret")

            with patch("app.main.ASFClient", return_value=client):
                with patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                    with patch("app.main.QMessageBox.warning"):
                        MainWindow._send_selected_order_with_asf(window)

            with connect_sqlite(db_path) as db:
                status = db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id = ?",
                    (self.ORDER_ID,),
                ).fetchone()[0]
            self.assertEqual(status, "reserved — не отправлен")
            client.send_trade_offer.assert_called_once()

    def test_recovery_of_interrupted_send_deletes_trade_credential(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            with connect_sqlite(db_path) as db:
                db.execute(
                    "UPDATE delivery_orders SET status = ?, trade_confirmation_status = '' WHERE order_id = ?",
                    ("sending — запрос ASF", self.ORDER_ID),
                )

            window = self._fake_window(db_path)
            window._delete_trade_credential_best_effort = MagicMock()

            recovered = MainWindow._recover_interrupted_delivery_orders(window)

            self.assertEqual(recovered, 1)
            window._delete_trade_credential_best_effort.assert_called_once_with(self.ORDER_ID)
            with connect_sqlite(db_path) as db:
                row = db.execute(
                    "SELECT status, trade_confirmation_status FROM delivery_orders WHERE order_id = ?",
                    (self.ORDER_ID,),
                ).fetchone()
            self.assertEqual(
                tuple(row),
                ("неизвестен результат отправки — проверь исходящие Steam", "unknown"),
            )


if __name__ == "__main__":
    unittest.main()
