import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from app.main import MainWindow


class AppStartupSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

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
