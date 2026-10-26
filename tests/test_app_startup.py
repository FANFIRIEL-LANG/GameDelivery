import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from app.main import MainWindow


class AppStartupSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_trade_url_validation_accepts_steam_url_and_rejects_other_hosts(self):
        valid = "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123"
        self.assertEqual(MainWindow._validate_trade_url(valid), valid)
        with self.assertRaises(ValueError):
            MainWindow._validate_trade_url("https://evil.example/tradeoffer/new/?partner=1&token=abcdef")

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
