import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox
from app.main import MainWindow
from games.steam_cards.reservations import AssetReservationStore
from games.steam_cards.selection import CardCopy
from games.steam_cards.snapshot import InventorySnapshotStore


class TradeDeliveryUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

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
        with sqlite3.connect(database) as db:
            db.execute(
                "INSERT INTO delivery_orders "
                "(order_id, recipient_trade_url, status, asset_count, source_platform) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "GD-ORDER-1",
                    "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123",
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

    def _run_with_window(self, database, body):
        self._make_database(database)
        with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database):
            window = MainWindow()
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
                with sqlite3.connect(database) as db:
                    row = db.execute(
                        "SELECT status, trade_offer_id, trade_confirmation_status "
                        "FROM delivery_orders WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()
                    reservation = db.execute(
                        "SELECT bot_name, asset_id FROM steam_asset_reservations WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchall()
                self.assertEqual(row, ("sent — ожидание покупателя", "9876543210", "handled"))
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
                with sqlite3.connect(database) as db:
                    status = db.execute(
                        "SELECT status FROM delivery_orders WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()[0]
                self.assertEqual(status, "неизвестен результат отправки — проверь исходящие Steam")
                self.assertFalse(window.send_offer_button.isEnabled())

            self._run_with_window(database, exercise)

    def test_cancel_reserved_order_releases_assets_without_sending_trade(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"

            def exercise(window):
                with sqlite3.connect(database) as db:
                    before = db.execute(
                        "SELECT bot_name, asset_id FROM steam_asset_reservations WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchall()
                self.assertEqual(before, [("Bot A", "1234567890")])

                with patch(
                    "app.main.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ), patch("app.main.QMessageBox.warning") as warning, \\
                     patch("app.main.ASFClient.send_trade_offer") as send:
                    window._cancel_selected_order()

                warning.assert_not_called()
                send.assert_not_called()
                with sqlite3.connect(database) as db:
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

            self._run_with_window(database, exercise)

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
                with sqlite3.connect(database) as db:
                    status = db.execute(
                        "SELECT status FROM delivery_orders WHERE order_id = ?",
                        ("GD-ORDER-1",),
                    ).fetchone()[0]
                self.assertEqual(status, "reserved — не отправлен")

            self._run_with_window(database, exercise)


if __name__ == "__main__":
    unittest.main()
