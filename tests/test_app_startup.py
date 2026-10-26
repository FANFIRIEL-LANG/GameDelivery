import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from app.main import MainWindow
from games.steam_cards.selection import CardCopy
from games.steam_cards.snapshot import InventorySnapshotStore


class AppStartupSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_trade_url_validation_accepts_steam_url_and_rejects_other_hosts(self):
        valid = "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123"
        self.assertEqual(MainWindow._validate_trade_url(valid), valid)
        with self.assertRaises(ValueError):
            MainWindow._validate_trade_url("https://evil.example/tradeoffer/new/?partner=1&token=abcdef")

    def test_selected_snapshot_asset_is_reserved_and_order_persists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            InventorySnapshotStore(database).replace(
                [CardCopy("asset-123", "Example Game", "Trading Card")],
                bot_name="Bot A",
            )
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database), \
                 patch("app.main.QMessageBox.information") as info, \
                 patch("app.main.QMessageBox.warning") as warning:
                window = MainWindow()
                try:
                    self.assertEqual(window.order_assets.count(), 1)
                    window.order_assets.item(0).setSelected(True)
                    window.order_recipient.setText(
                        "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123"
                    )
                    window._prepare_delivery_order()
                    warning.assert_not_called()
                    info.assert_called_once()
                    with sqlite3.connect(database) as db:
                        order = db.execute(
                            "SELECT asset_count, status FROM delivery_orders"
                        ).fetchone()
                        reservation = db.execute(
                            "SELECT bot_name, asset_id FROM steam_asset_reservations"
                        ).fetchone()
                    self.assertEqual(order[0], 1)
                    self.assertIn("не отправлен", order[1])
                    self.assertEqual(reservation, ("Bot A", "asset-123"))
                    with sqlite3.connect(database) as db:
                        order_asset = db.execute(
                            "SELECT order_id, bot_name, asset_id FROM delivery_order_assets"
                        ).fetchone()
                    self.assertEqual(order_asset[1:], ("Bot A", "asset-123"))
                    window._load_saved_orders()
                    self.assertEqual(window.orders.rowCount(), 1)
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_main_window_constructs_with_expected_tabs(self):
        window = MainWindow()
        try:
            self.assertEqual(window.windowTitle(), "GameDelivery — локальная панель")
            self.assertEqual(
                [window.tabs.tabText(i) for i in range(window.tabs.count())],
                ["Заказы", "Инвентарь", "Модули игр", "Журнал", "Настройки", "ASF-боты"],
            )
            self.assertGreaterEqual(window.width(), 900)
            self.assertGreaterEqual(window.height(), 600)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
