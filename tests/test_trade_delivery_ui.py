import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from app.main import MainWindow
from core.database.connection import connect_sqlite
from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from integrations.playerok.sync import PlayerokDealSyncService
from games.steam_cards.reservations import AssetReservationStore
from games.steam_cards.selection import CardCopy
from games.steam_cards.snapshot import InventorySnapshotStore
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
    def __init__(self, *_args, **_kwargs):
        self.values = {}
    def value(self, key, default=None):
        return self.values.get(key, default)
    def setValue(self, key, value):
        self.values[key] = value
    def sync(self):
        pass
    def status(self):
        return 0


def _tuple_row(_cursor, row):
    return tuple(row)


class TradeDeliveryUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls._ipc_test_mode_patch = patch.dict(
            os.environ, {"GAMEDELIVERY_TEST_ASF_IPC_URL": "http://127.0.0.1:1243"}
        )
        cls._ipc_test_mode_patch.start()
        cls.addClassCleanup(cls._ipc_test_mode_patch.stop)
        cls._settings_patch = patch("app.main.QSettings", side_effect=MemorySettings)
        cls._settings_patch.start()
        cls.addClassCleanup(cls._settings_patch.stop)

    @staticmethod
    def _preview():
        return {
            "success": True,
            "mode": "preview-only",
            "sendEnabled": False,
            "orderId": "GD-ORDER-1",
            "botName": "Bot A",
            "targetSteamId64": 76561197960389184,
            "assetCount": 1,
            "assets": [
                {"assetId": "1234567890", "name": "Example Card", "tradable": True}
            ],
        }

    @staticmethod
    def _send_result():
        return {
            "success": True,
            "mode": "sent",
            "sendEnabled": True,
            "duplicatePrevented": False,
            "orderId": "GD-ORDER-1",
            "botName": "Bot A",
            "targetSteamId64": 76561197960389184,
            "assetCount": 1,
            "assets": [{"assetId": "1234567890"}],
            "offerIds": ["9876543210"],
            "confirmationStatus": "handled",
            "message": "Offer created.",
            "tradeAccepted": False,
        }

    def _populate_order(self, window, database):
        window._ensure_delivery_order_schema()
        safe_trade_url = window._trade_credentials.store_trade_url(
            "GD-ORDER-1",
            "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123",
        )
        with connect_sqlite(database, row_factory=_tuple_row) as db:
            db.execute(
                "INSERT INTO delivery_orders "
                "(order_id, recipient_trade_url, status, asset_count, source_platform) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "GD-ORDER-1",
                    safe_trade_url,
                    "reserved — не отправлен",
                    1,
                    "manual",
                ),
            )
            db.execute(
                "INSERT INTO delivery_order_assets (order_id, bot_name, asset_id) VALUES (?, ?, ?)",
                ("GD-ORDER-1", "Bot A", "1234567890"),
            )
        AssetReservationStore(database).reserve(
            "GD-ORDER-1", [("Bot A", "1234567890")]
        )
        window._load_saved_orders()
        window.orders.selectRow(0)

    def _make_database(self, database):
        InventorySnapshotStore(database).replace(
            [CardCopy("1234567890", "Example Game", "Example Card")],
            bot_name="Bot A",
        )

    def test_order_asset_picker_shows_only_normal_trading_cards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            InventorySnapshotStore(database).replace([
                CardCopy("normal", "Example Game", "Normal Card"),
                CardCopy("foil", "Example Game", "Foil Card", is_foil=True),
                CardCopy("item", "Counter-Strike 2", "AK-47", is_trading_card=False),
                CardCopy("locked", "Example Game", "Untradable Card", is_tradable=False),
            ], bot_name="Bot A")
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database):
                window = MainWindow()
                try:
                    labels = [window.order_assets.item(i).text() for i in range(window.order_assets.count())]
                    self.assertEqual(len(labels), 2)
                    self.assertTrue(any("Normal Card" in label for label in labels))
                    self.assertTrue(any("Untradable Card" in label for label in labels))
                    self.assertFalse(any("Foil Card" in label for label in labels))
                    self.assertFalse(any("AK-47" in label for label in labels))
                    locked_item = next(
                        window.order_assets.item(i)
                        for i in range(window.order_assets.count())
                        if "Untradable Card" in window.order_assets.item(i).text()
                    )
                    self.assertFalse(locked_item.flags() & Qt.ItemFlag.ItemIsSelectable)
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_prepare_order_rejects_non_card_even_if_injected_into_selection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            InventorySnapshotStore(database).replace([
                CardCopy("not-card", "Counter-Strike 2", "AK-47", is_trading_card=False),
            ], bot_name="Bot A")
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database):
                window = MainWindow()
                window._trade_credentials = SteamTradeCredentialStore(backend=MemoryKeyring())
                try:
                    window.order_recipient.setText(
                        "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123"
                    )
                    from PySide6.QtWidgets import QListWidgetItem
                    injected = QListWidgetItem("Injected item")
                    injected.setData(Qt.ItemDataRole.UserRole, ("Bot A", "not-card"))
                    window.order_assets.addItem(injected)
                    injected.setSelected(True)
                    with patch("app.main.QMessageBox.warning") as warning:
                        window._prepare_delivery_order()
                    warning.assert_called_once()
                    with connect_sqlite(database, row_factory=_tuple_row) as db:
                        orders = db.execute("SELECT COUNT(*) FROM delivery_orders").fetchone()[0]
                        reservations = db.execute("SELECT COUNT(*) FROM steam_asset_reservations").fetchone()[0]
                    self.assertEqual(orders, 0)
                    self.assertEqual(reservations, 0)
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_playerok_import_without_canonical_account_does_not_create_legacy_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            self._make_database(database)
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database):
                window = MainWindow()
                window._trade_credentials = SteamTradeCredentialStore(backend=MemoryKeyring())
                try:
                    window._import_marketplace_order({
                        "platform": "playerok", "deal_id": "deal-unpaid", "order_id": "deal-unpaid",
                        "account_user_id": "seller-1", "title": "Test card", "buyer": "buyer", "status": "SENT",
                    })
                    with connect_sqlite(database) as db:
                        self.assertEqual(db.execute(
                            "SELECT COUNT(*) FROM delivery_orders WHERE source_order_id='deal-unpaid'"
                        ).fetchone()[0], 0)
                        self.assertEqual(db.execute(
                            "SELECT COUNT(*) FROM delivery_order_assets WHERE order_id LIKE 'GD-%'"
                        ).fetchone()[0], 0)
                        self.assertEqual(db.execute(
                            "SELECT COUNT(*) FROM steam_asset_reservations"
                        ).fetchone()[0], 0)
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_reimported_paid_status_cannot_unblock_historical_baseline_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            self._make_database(database)
            ledger_database = Database(database)
            ledger = DeliveryLedger(ledger_database)
            PlayerokDealSyncService(ledger_database)
            config = ledger.create_listing_config_version(
                listing_id="listing-baseline", source_platform="playerok",
                external_listing_id="external-listing-baseline", delivery_quantity_per_deal=1,
                item_selectors={"app_id": 753, "context_id": 6, "item_type": "steam_card_set"},
                bot_pool=["Bot A"],
            )
            ledger.create_deal(
                deal_id="deal-baseline", source_platform="playerok", external_deal_id="deal-baseline",
                chat_id="chat-baseline", listing_id=config["listing_id"],
                config_version_id=config["config_version_id"], platform_status="PAID",
            )
            # A historical Deal must be tested through its real canonical Order.
            # Without this row, the UI cannot select the ledger order and the test
            # accidentally falls through to the manual-order path and opens a modal.
            ledger.create_order(
                order_id="order-baseline", deal_id="deal-baseline", total_quantity=1,
            )
            with ledger_database.connect() as db:
                db.execute("UPDATE gd_deals SET playerok_account_id='seller-1',is_historical=1 WHERE deal_id='deal-baseline'")
                db.execute(
                    "INSERT INTO playerok_sync_state(account_user_id,baseline_completed) VALUES('seller-1',1)"
                )

            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database):
                window = MainWindow()
                window._trade_credentials = SteamTradeCredentialStore(backend=MemoryKeyring())
                try:
                    order = {
                        "platform": "playerok", "deal_id": "deal-baseline", "order_id": "deal-baseline",
                        "account_user_id": "seller-1",
                        "title": "Historical listing", "buyer": "buyer", "status": "SENT",
                    }
                    window._import_marketplace_order(order)
                    window._import_marketplace_order({**order, "status": "PAID"})
                    self.assertEqual(window.orders.rowCount(), 1)
                    selected_context = window.orders.item(0, 0).data(Qt.ItemDataRole.UserRole)
                    self.assertEqual(selected_context["kind"], "ledger")
                    self.assertEqual(selected_context["order_id"], "order-baseline")
                    window.orders.selectRow(0)
                    window.order_recipient.setText(
                        "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123"
                    )
                    window.order_assets.item(0).setSelected(True)
                    with patch("app.main.QMessageBox.warning") as warning, \
                         patch("app.main.QMessageBox.information") as information, \
                         patch("app.main.ASFClient") as asf_client:
                        window._prepare_delivery_order()
                    warning.assert_called_once()
                    information.assert_not_called()
                    asf_client.assert_not_called()
                    with connect_sqlite(database) as db:
                        self.assertEqual(db.execute(
                            "SELECT COUNT(*) FROM delivery_orders WHERE source_order_id='deal-baseline'"
                        ).fetchone()[0], 0)
                        self.assertEqual(db.execute(
                            "SELECT is_historical FROM gd_deals WHERE deal_id='deal-baseline'"
                        ).fetchone()[0], 1)
                        self.assertEqual(db.execute(
                            "SELECT COUNT(*) FROM steam_asset_reservations"
                        ).fetchone()[0], 0)
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def _run_with_window(self, database, body):
        self._make_database(database)
        with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database):
            window = MainWindow()
            window._trade_credentials = SteamTradeCredentialStore(backend=MemoryKeyring())
            try:
                self._populate_order(window, database)
                body(window)
            finally:
                window.close()
                window.deleteLater()
                self.app.processEvents()

    def test_send_button_starts_disabled_without_selected_reserved_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            self._make_database(database)
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database):
                window = MainWindow()
                try:
                    self.assertFalse(window.send_offer_button.isEnabled())
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_auto_send_switch_defaults_off_and_off_mode_keeps_prepared_reserves(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            InventorySnapshotStore(database).replace(
                [CardCopy("1234567890", "Example Game", "Example Card")], bot_name="Bot A"
            )
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database):
                window = MainWindow()
                window._trade_credentials = SteamTradeCredentialStore(backend=MemoryKeyring())
                try:
                    self.assertFalse(window.auto_send_checkbox.isChecked())
                    self.assertIn("ВЫКЛ", window.auto_send_checkbox.text())
                    window.order_recipient.setText(
                        "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123"
                    )
                    card = next(
                        window.order_assets.item(i)
                        for i in range(window.order_assets.count())
                        if "Example Card" in window.order_assets.item(i).text()
                    )
                    card.setSelected(True)
                    with patch("app.main.QMessageBox.information"):
                        window._prepare_delivery_order()
                    with connect_sqlite(database, row_factory=_tuple_row) as db:
                        order = db.execute(
                            "SELECT status FROM delivery_orders ORDER BY created_at DESC LIMIT 1"
                        ).fetchone()
                        reservation_count = db.execute(
                            "SELECT COUNT(*) FROM steam_asset_reservations"
                        ).fetchone()[0]
                    self.assertEqual(order[0], "reserved — не отправлен")
                    self.assertEqual(reservation_count, 1)
                    status_cell = window.orders.item(0, 4)
                    self.assertEqual(status_cell.text(), "Готов к ручной отправке")
                    window.auto_send_checkbox.click()
                    self.assertTrue(window._delivery_send_policy.auto_send_enabled())
                    window.auto_send_checkbox.click()
                    self.assertFalse(window._delivery_send_policy.auto_send_enabled())
                    with connect_sqlite(database, row_factory=_tuple_row) as db:
                        self.assertEqual(
                            db.execute("SELECT status FROM delivery_orders LIMIT 1").fetchone()[0],
                            "reserved — не отправлен",
                        )
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_successful_send_persists_offer_id_and_pending_recipient_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"

            def exercise(window):
                with patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                     patch("app.main.QMessageBox.information") as info, \
                     patch("app.main.QMessageBox.warning") as warning, \
                     patch("app.main.ASFClient.preview_trade_offer", return_value=self._preview()), \
                     patch("app.main.ASFClient.send_trade_offer", return_value=self._send_result()) as send:
                    window._send_selected_order_with_asf()

                send.assert_called_once_with(
                    order_id="GD-ORDER-1",
                    bot_name="Bot A",
                    trade_url="https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123",
                    asset_ids=["1234567890"],
                )
                warning.assert_not_called()
                info.assert_called_once()
                with connect_sqlite(database, row_factory=_tuple_row) as db:
                    row = db.execute(
                        "SELECT status, trade_offer_id, trade_confirmation_status, recipient_trade_url, recipient_steam_id64 "
                        "FROM delivery_orders WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()
                    reservation = db.execute(
                        "SELECT bot_name, asset_id FROM steam_asset_reservations WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchall()
                self.assertEqual(
                    row,
                    (
                        "sent — ожидание покупателя",
                        "9876543210",
                        "handled",
                        "https://steamcommunity.com/tradeoffer/new/?partner=123456",
                        "76561197960389184",
                    ),
                )
                self.assertIsNone(
                    window._trade_credentials._backend.get_password(
                        window._trade_credentials.SERVICE, "GD-ORDER-1"
                    )
                )
                self.assertEqual(reservation, [("Bot A", "1234567890")])
                self.assertFalse(window.send_offer_button.isEnabled())

            self._run_with_window(database, exercise)

    def test_unknown_send_result_locks_order_against_automatic_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"

            def exercise(window):
                with patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                     patch("app.main.QMessageBox.warning"), \
                     patch("app.main.ASFClient.preview_trade_offer", return_value=self._preview()), \
                     patch(
                         "app.main.ASFClient.send_trade_offer",
                         return_value={
                             "success": False,
                             "mode": "send",
                             "sendEnabled": True,
                             "resultUnknown": True,
                             "error": "Result unknown.",
                         },
                     ) as send:
                    window._send_selected_order_with_asf()
                    window._send_selected_order_with_asf()

                send.assert_called_once()
                with connect_sqlite(database, row_factory=_tuple_row) as db:
                    status, recipient_snapshot = db.execute(
                        "SELECT status, recipient_steam_id64 FROM delivery_orders WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()
                self.assertEqual(status, "неизвестен результат отправки — проверь исходящие Steam")
                self.assertEqual(recipient_snapshot, "76561197960389184")
                self.assertIsNone(
                    window._trade_credentials._backend.get_password(
                        window._trade_credentials.SERVICE, "GD-ORDER-1"
                    )
                )
                self.assertFalse(window.send_offer_button.isEnabled())

            self._run_with_window(database, exercise)

    def test_cancel_reserved_order_releases_assets_without_sending_trade(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"

            def exercise(window):
                with connect_sqlite(database, row_factory=_tuple_row) as db:
                    before = db.execute(
                        "SELECT bot_name, asset_id FROM steam_asset_reservations WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchall()
                self.assertEqual(before, [("Bot A", "1234567890")])

                with (
                    patch(
                        "app.main.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes,
                    ),
                    patch("app.main.QMessageBox.warning") as warning,
                    patch("app.main.ASFClient.send_trade_offer") as send,
                ):
                    window._cancel_selected_order()

                warning.assert_not_called()
                send.assert_not_called()
                with connect_sqlite(database, row_factory=_tuple_row) as db:
                    status = db.execute(
                        "SELECT status FROM delivery_orders WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()[0]
                    reservations = db.execute(
                        "SELECT bot_name, asset_id FROM steam_asset_reservations WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchall()
                self.assertEqual(status, "cancelled — резерв снят")
                self.assertEqual(reservations, [])
                self.assertIsNone(
                    window._trade_credentials._backend.get_password(
                        window._trade_credentials.SERVICE, "GD-ORDER-1"
                    )
                )

            self._run_with_window(database, exercise)

    def test_cancel_unknown_send_result_marks_order_cancelled_but_keeps_asset_reserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"

            def exercise(window):
                with connect_sqlite(database, row_factory=_tuple_row) as db:
                    db.execute(
                        "UPDATE delivery_orders SET status = ? WHERE order_id = ?",
                        ("неизвестен результат отправки — проверь исходящие Steam", "GD-ORDER-1"),
                    )
                with patch(
                    "app.main.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ), patch("app.main.QMessageBox.warning") as warning:
                    window._cancel_selected_order()

                warning.assert_not_called()
                with connect_sqlite(database, row_factory=_tuple_row) as db:
                    status = db.execute(
                        "SELECT status FROM delivery_orders WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()[0]
                    reservations = db.execute(
                        "SELECT bot_name, asset_id FROM steam_asset_reservations WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchall()
                self.assertEqual(status, "cancelled — результат отправки требует сверки")
                self.assertEqual(reservations, [("Bot A", "1234567890")])
                self.assertIsNone(
                    window._trade_credentials._backend.get_password(
                        window._trade_credentials.SERVICE, "GD-ORDER-1"
                    )
                )
                self.assertFalse(window.send_offer_button.isEnabled())

            self._run_with_window(database, exercise)


    def test_reconcile_manually_cancelled_unknown_offer_releases_reservation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"

            def exercise(window):
                with connect_sqlite(database, row_factory=_tuple_row) as db:
                    db.execute(
                        "UPDATE delivery_orders SET status = ?, trade_offer_id = ? WHERE order_id = ?",
                        ("cancelled — результат отправки требует сверки", "9876543210", "GD-ORDER-1"),
                    )
                with patch(
                    "app.main.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ), patch("app.main.QMessageBox.warning") as warning:
                    window._reconcile_cancelled_order_reservation()

                warning.assert_not_called()
                with connect_sqlite(database, row_factory=_tuple_row) as db:
                    row = db.execute(
                        "SELECT status, trade_confirmation_status FROM delivery_orders WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()
                    reservations = db.execute(
                        "SELECT COUNT(*) FROM steam_asset_reservations WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()[0]
                self.assertEqual(
                    row,
                    ("cancelled — предложение Steam отменено оператором", "cancelled_by_operator"),
                )
                self.assertEqual(reservations, 0)

            self._run_with_window(database, exercise)

    def test_delete_reserved_order_removes_order_assets_and_releases_reservation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"

            def exercise(window):
                with patch(
                    "app.main.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ), patch("app.main.QMessageBox.warning") as warning:
                    window._delete_selected_order()

                warning.assert_not_called()
                with connect_sqlite(database, row_factory=_tuple_row) as db:
                    order_count = db.execute(
                        "SELECT COUNT(*) FROM delivery_orders WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()[0]
                    asset_count = db.execute(
                        "SELECT COUNT(*) FROM delivery_order_assets WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()[0]
                    reservation_count = db.execute(
                        "SELECT COUNT(*) FROM steam_asset_reservations WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()[0]
                self.assertEqual((order_count, asset_count, reservation_count), (0, 0, 0))

            self._run_with_window(database, exercise)

    def test_delete_unknown_or_sent_order_is_blocked_until_reconciled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"

            def exercise(window):
                with connect_sqlite(database, row_factory=_tuple_row) as db:
                    db.execute(
                        "UPDATE delivery_orders SET status = ? WHERE order_id = ?",
                        ("неизвестен результат отправки — проверь исходящие Steam", "GD-ORDER-1"),
                    )
                with patch("app.main.QMessageBox.warning") as warning:
                    window._delete_selected_order()
                warning.assert_called_once()
                with connect_sqlite(database, row_factory=_tuple_row) as db:
                    order_count = db.execute(
                        "SELECT COUNT(*) FROM delivery_orders WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()[0]
                    reservation_count = db.execute(
                        "SELECT COUNT(*) FROM steam_asset_reservations WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()[0]
                self.assertEqual((order_count, reservation_count), (1, 1))

            self._run_with_window(database, exercise)

    def test_game_inventory_delivery_does_not_enter_card_only_order_flow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            self._make_database(database)
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database):
                window = MainWindow()
                try:
                    with patch("app.main.QMessageBox.information") as info:
                        window._explain_game_delivery_unavailable(
                            "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123",
                            [("Bot A", "cs2-asset-1")],
                        )
                    info.assert_called_once()
                    self.assertEqual(window.orders.rowCount(), 0)
                    with connect_sqlite(database, row_factory=_tuple_row) as db:
                        reservations = db.execute(
                            "SELECT COUNT(*) FROM steam_asset_reservations"
                        ).fetchone()[0]
                    self.assertEqual(reservations, 0)
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_user_cancellation_does_not_send_offer_or_change_order_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"

            def exercise(window):
                with patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.No), \
                     patch("app.main.QMessageBox.warning") as warning, \
                     patch("app.main.ASFClient.preview_trade_offer", return_value=self._preview()), \
                     patch("app.main.ASFClient.send_trade_offer") as send:
                    window._send_selected_order_with_asf()

                send.assert_not_called()
                warning.assert_not_called()
                with connect_sqlite(database, row_factory=_tuple_row) as db:
                    status = db.execute(
                        "SELECT status FROM delivery_orders WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()[0]
                self.assertEqual(status, "reserved — не отправлен")

            self._run_with_window(database, exercise)


    def test_preview_requires_exact_current_asset_reservations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"

            def exercise(window):
                with connect_sqlite(database, row_factory=_tuple_row) as db:
                    db.execute(
                        "DELETE FROM steam_asset_reservations WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    )

                client = MagicMock()
                client.preview_trade_offer.return_value = {
                    "orderId": "GD-ORDER-1",
                    "botName": "Bot A",
                    "targetSteamId64": 76561197960389184,
                    "assetCount": 1,
                    "assets": [{"assetId": "1234567890", "name": "Example Card"}],
                }
                with (
                    patch("app.main.ASFClient", return_value=client),
                    patch("app.main.QMessageBox.warning") as warning,
                    patch("app.main.QMessageBox.information"),
                ):
                    window._preview_selected_order_with_asf()

                client.preview_trade_offer.assert_not_called()
                warning.assert_called_once()

            self._run_with_window(database, exercise)

    def test_legacy_trade_url_is_migrated_out_of_sqlite_on_schema_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            self._make_database(database)
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database):
                window = MainWindow()
                window._trade_credentials = SteamTradeCredentialStore(backend=MemoryKeyring())
                try:
                    with connect_sqlite(database, row_factory=_tuple_row) as db:
                        db.execute(
                            "INSERT INTO delivery_orders "
                            "(order_id, recipient_trade_url, status, asset_count, source_platform) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                "GD-LEGACY-1",
                                "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123",
                                "reserved — не отправлен",
                                1,
                                "manual",
                            ),
                        )

                    window._ensure_delivery_order_schema()

                    with connect_sqlite(database, row_factory=_tuple_row) as db:
                        stored_url = db.execute(
                            "SELECT recipient_trade_url FROM delivery_orders WHERE order_id = ?",
                            ("GD-LEGACY-1",),
                        ).fetchone()[0]
                    self.assertEqual(
                        stored_url,
                        "https://steamcommunity.com/tradeoffer/new/?partner=123456",
                    )
                    self.assertEqual(
                        window._trade_credentials.load_trade_url("GD-LEGACY-1", stored_url),
                        "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123",
                    )
                    self.assertNotIn(b"Abc_123", database.read_bytes())
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_order_details_never_display_trade_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"

            def exercise(window):
                with patch("app.main.QMessageBox.information") as info:
                    window._show_order_details(0)
                rendered = str(info.call_args.args[2])
                self.assertNotIn("Abc_123", rendered)
                self.assertIn("Токен Steam хранится отдельно", rendered)

            self._run_with_window(database, exercise)


if __name__ == "__main__":
    unittest.main()
