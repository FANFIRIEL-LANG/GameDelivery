import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from app.asf_management import ASFManagementWidget


class ASFManagementWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = ASFManagementWidget()

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    @patch("app.asf_management.ASFClient.get_bots")
    def test_refresh_lists_loaded_bots_without_secrets(self, get_bots):
        get_bots.return_value = {
            "zeta": {"IsConnected": True, "SteamPassword": "never-display"},
            "Alpha": {"KeepRunning": False},
        }
        self.widget.refresh_bots()
        self.assertEqual(self.widget.table.rowCount(), 2)
        self.assertEqual(self.widget.table.item(0, 0).text(), "Alpha")
        self.assertEqual(self.widget.table.item(1, 0).text(), "zeta")
        self.assertNotIn("never-display", self.widget.status.text())
        self.assertIn("Получено ботов: 2", self.widget.status.text())

    @patch("app.asf_management.QFileDialog.getSaveFileName")
    def test_export_writes_disabled_config_without_steam_password(self, choose_path):
        with tempfile.TemporaryDirectory() as temp:
            target = str(Path(temp) / "new-bot.json")
            choose_path.return_value = (target, "JSON (*.json)")
            self.widget.bot_name.setText("new-bot")
            self.widget.steam_login.setText("steam-user")
            self.widget.export_config()
            payload = json.loads(Path(target).read_text(encoding="utf-8"))
        self.assertEqual(payload, {"Enabled": False, "SteamLogin": "steam-user"})
        self.assertNotIn("SteamPassword", payload)
        self.assertIn("Enabled=false", self.widget.status.text())

    @patch("app.asf_management.QMessageBox.warning")
    def test_export_rejects_invalid_name_before_file_dialog(self, warning):
        with patch("app.asf_management.QFileDialog.getSaveFileName") as choose_path:
            self.widget.bot_name.setText("../unsafe")
            self.widget.export_config()
        choose_path.assert_not_called()
        warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
