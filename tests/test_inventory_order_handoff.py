import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QTabWidget, QWidget,
)

from app.main import MainWindow


class InventoryOrderHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self, *, selectable=True):
        window = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(window)
        window.order_recipient = QLineEdit()
        window.order_bot = QComboBox()
        window.order_bot.addItem("Bot A", "Bot A")
        window.order_assets = QListWidget()
        item = QListWidgetItem("AK-47 | Redline · asset 1001")
        item.setData(Qt.ItemDataRole.UserRole, ("Bot A", "1001"))
        if not selectable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        window.order_assets.addItem(item)
        window._load_order_bots = lambda: None
        window._load_order_assets = lambda: None
        window.tabs = QTabWidget()
        window.tabs.addTab(QWidget(), "Заказы")
        return window, item

    def test_inventory_selection_is_preselected_in_order_form(self):
        window, item = self.make_window()

        with patch("app.main.QMessageBox.warning") as warning:
            window._open_order_with_trade_url(
                "https://steamcommunity.com/tradeoffer/new/?partner=123&token=abc",
                [("Bot A", "1001")],
            )

        warning.assert_not_called()
        self.assertEqual(window.order_recipient.text(),
                         "https://steamcommunity.com/tradeoffer/new/?partner=123&token=abc")
        self.assertTrue(item.isSelected())
        self.assertEqual(window.tabs.currentIndex(), 0)

    def test_inventory_selection_from_multiple_bots_is_not_partially_transferred(self):
        window, item = self.make_window()

        with patch("app.main.QMessageBox.warning") as warning:
            window._open_order_with_trade_url(
                "https://steamcommunity.com/tradeoffer/new/?partner=123&token=abc",
                [("Bot A", "1001"), ("Bot B", "2002")],
            )

        warning.assert_called_once()
        self.assertFalse(item.isSelected())
        self.assertEqual(window.tabs.currentIndex(), 0)

    def test_reserved_or_unselectable_asset_is_not_transferred(self):
        window, item = self.make_window(selectable=False)

        with patch("app.main.QMessageBox.warning") as warning:
            window._open_order_with_trade_url(
                "https://steamcommunity.com/tradeoffer/new/?partner=123&token=abc",
                [("Bot A", "1001")],
            )

        warning.assert_called_once()
        self.assertFalse(item.isSelected())

    def test_steam_card_adapter_rejects_invalid_asset_id_types_before_matching(self):
        invalid_ids = (None, True, 123, 12.5, "", " \t\n")
        for asset_id in invalid_ids:
            with self.subTest(asset_id=asset_id):
                window, item = self.make_window()
                with patch("app.main.QMessageBox.warning") as warning:
                    window._open_order_with_trade_url(
                        "https://steamcommunity.com/tradeoffer/new/?partner=123&token=abc",
                        [("Bot A", asset_id)],
                    )
                warning.assert_called_once()
                self.assertFalse(item.isSelected())
                self.assertEqual(window.order_recipient.text(), "")

    def test_game_adapter_rejects_invalid_asset_id_types_before_snapshot_lookup(self):
        invalid_ids = (None, True, 123, 12.5, "", " \t\n")
        for asset_id in invalid_ids:
            with self.subTest(asset_id=asset_id):
                window = MainWindow.__new__(MainWindow)
                QMainWindow.__init__(window)
                with patch("app.main.QMessageBox.warning") as warning:
                    window._open_game_order_with_trade_url(
                        "https://steamcommunity.com/tradeoffer/new/?partner=123&token=abc",
                        [("Bot A", asset_id)],
                    )
                warning.assert_called_once()
                self.assertFalse(hasattr(window, "game_inventory_panel"))

    def test_game_adapter_preserves_valid_asset_id_string(self):
        window = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(window)
        asset_id = "0001001"
        item = SimpleNamespace(
            bot_name="Bot A", asset_id=asset_id, appid=730, context_id=2,
            is_tradable=True, amount=1,
        )
        window.game_inventory_panel = SimpleNamespace(cs2_all_instances=[item])
        window.order_recipient = QLineEdit()
        window._load_saved_orders = lambda: None
        window.orders = MagicMock()
        window.orders.rowCount.return_value = 0
        window.delivery_pipeline = MagicMock()
        window.delivery_pipeline.prepare_cs2_order.return_value = SimpleNamespace(
            status="reserved", message="", reserved_assets=(("Bot A", asset_id),),
        )
        window._auto_send_enabled = lambda: False
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [{
            "bot_name": "Bot A", "updated_at": "2026-11-17T10:00:00+00:00",
        }]

        with patch("app.main.connect_sqlite") as connect, \
                patch("app.main.Database"), \
                patch("app.main.DeliveryLedger") as ledger_factory, \
                patch("app.main.QMessageBox.information") as information, \
                patch("app.main.QMessageBox.warning") as warning:
            connect.return_value.__enter__.return_value = connection
            window._open_game_order_with_trade_url(
                "https://steamcommunity.com/tradeoffer/new/?partner=123&token=abc",
                [("Bot A", asset_id)],
            )

        warning.assert_not_called()
        information.assert_called_once()
        ledger = ledger_factory.return_value
        self.assertEqual(
            ledger.create_listing_config_version.call_args.kwargs["item_selectors"]["asset_keys"],
            ["bot a::0001001"],
        )
        prepared_snapshots = window.delivery_pipeline.prepare_cs2_order.call_args.kwargs["snapshots"]
        self.assertEqual(prepared_snapshots["Bot A"].items[0].asset_id, asset_id)


if __name__ == "__main__":
    unittest.main()
