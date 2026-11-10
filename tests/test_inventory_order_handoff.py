import os
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
