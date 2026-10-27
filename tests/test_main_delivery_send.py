import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QMessageBox

from app.main import MainWindow


class DeliverySendFlowTests(unittest.TestCase):
    ORDER_ID = "GD-SEND-TEST"
    BOT_NAME = "bot-a"
    ASSET_ID = "123"
    TRADE_URL = "https://steamcommunity.com/tradeoffer/new/?partner=12345&token=Tok-abc_1"
    STEAM_ID64 = 76561197960265728 + 12345

    def _database(self, path: Path):
        with sqlite3.connect(path) as db:
            db.execute("""
                CREATE TABLE delivery_orders (
                    order_id TEXT PRIMARY KEY,
                    recipient_trade_url TEXT NOT NULL,
                    status TEXT NOT NULL,
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
                "(order_id, recipient_trade_url, status) VALUES (?, ?, ?)",
                (self.ORDER_ID, self.TRADE_URL, "reserved — не отправлен"),
            )
            db.execute(
                "INSERT INTO delivery_order_assets (order_id, bot_name, asset_id) VALUES (?, ?, ?)",
                (self.ORDER_ID, self.BOT_NAME, self.ASSET_ID),
            )
            db.execute(
                "INSERT INTO steam_asset_reservations (bot_name, asset_id, order_id) VALUES (?, ?, ?)",
                (self.BOT_NAME, self.ASSET_ID, self.ORDER_ID),
            )

    def _fake_window(self, path: Path):
        status_bar = MagicMock()
        return SimpleNamespace(
            _database_path=path,
            asf_management=SimpleNamespace(ipc_password=SimpleNamespace(text=lambda: "")),
            _selected_order_id=lambda: self.ORDER_ID,
            _validate_trade_url=lambda value: value,
            _load_saved_orders=MagicMock(),
            _load_order_assets=MagicMock(),
            statusBar=lambda: status_bar,
            _status_bar=status_bar,
        )

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
            client = MagicMock()
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
            with sqlite3.connect(db_path) as db:
                row = db.execute(
                    "SELECT status, trade_offer_id, trade_confirmation_status "
                    "FROM delivery_orders WHERE order_id = ?",
                    (self.ORDER_ID,),
                ).fetchone()
            self.assertEqual(row, ("sent — требуется подтверждение Steam", "456789", "pending"))
            warning.assert_not_called()

    def test_unknown_result_locks_order_and_does_not_reset_for_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            window = self._fake_window(db_path)
            client = MagicMock()
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

            with sqlite3.connect(db_path) as db:
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
            client = MagicMock()
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

            with sqlite3.connect(db_path) as db:
                row = db.execute(
                    "SELECT status, trade_offer_id, trade_confirmation_status "
                    "FROM delivery_orders WHERE order_id = ?",
                    (self.ORDER_ID,),
                ).fetchone()
            self.assertEqual(
                row,
                ("неизвестен результат отправки — проверь исходящие Steam", "123456, 456789", "unknown"),
            )
            client.send_trade_offer.assert_called_once()

    def test_cancel_confirmation_does_not_send_or_change_order(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            window = self._fake_window(db_path)
            client = MagicMock()
            client.preview_trade_offer.return_value = self._preview(
                self.ORDER_ID, self.BOT_NAME, self.STEAM_ID64, [self.ASSET_ID]
            )

            with patch("app.main.ASFClient", return_value=client):
                with patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
                    MainWindow._send_selected_order_with_asf(window)

            client.send_trade_offer.assert_not_called()
            with sqlite3.connect(db_path) as db:
                status = db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id = ?",
                    (self.ORDER_ID,),
                ).fetchone()[0]
            self.assertEqual(status, "reserved — не отправлен")


    def test_missing_reservation_blocks_send_before_live_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "delivery.sqlite3"
            self._database(db_path)
            with sqlite3.connect(db_path) as db:
                db.execute(
                    "DELETE FROM steam_asset_reservations WHERE order_id = ?",
                    (self.ORDER_ID,),
                )
            window = self._fake_window(db_path)
            client = MagicMock()

            with patch("app.main.ASFClient", return_value=client):
                with patch("app.main.QMessageBox.warning") as warning:
                    MainWindow._send_selected_order_with_asf(window)

            client.preview_trade_offer.assert_not_called()
            client.send_trade_offer.assert_not_called()
            warning.assert_called_once()
            with sqlite3.connect(db_path) as db:
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
            client = MagicMock()
            client.preview_trade_offer.return_value = self._preview(
                self.ORDER_ID, self.BOT_NAME, self.STEAM_ID64, [self.ASSET_ID]
            )

            def remove_reservation_then_confirm(*args, **kwargs):
                with sqlite3.connect(db_path) as db:
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
            with sqlite3.connect(db_path) as db:
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
            client = MagicMock()
            client.preview_trade_offer.return_value = self._preview(
                self.ORDER_ID, self.BOT_NAME, self.STEAM_ID64, [self.ASSET_ID]
            )
            client.send_trade_offer.side_effect = OSError("Cannot write local one-use secret")

            with patch("app.main.ASFClient", return_value=client):
                with patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                    with patch("app.main.QMessageBox.warning"):
                        MainWindow._send_selected_order_with_asf(window)

            with sqlite3.connect(db_path) as db:
                status = db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id = ?",
                    (self.ORDER_ID,),
                ).fetchone()[0]
            self.assertEqual(status, "reserved — не отправлен")
            client.send_trade_offer.assert_called_once()

if __name__ == "__main__":
    unittest.main()
