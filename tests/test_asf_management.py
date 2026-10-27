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

    @patch("app.asf_management.ASFClient.restart")
    @patch("app.asf_management.QMessageBox.question")
    def test_restart_requires_confirmation_before_calling_asf(self, question, restart):
        from PySide6.QtWidgets import QMessageBox
        question.return_value = QMessageBox.StandardButton.No
        self.widget.restart_asf()
        restart.assert_not_called()
        self.assertIn("отменён", self.widget.status.text())

    @patch("app.asf_management.ASFClient.restart")
    @patch("app.asf_management.QMessageBox.question")
    def test_confirmed_restart_sends_explicit_confirmation(self, question, restart):
        from PySide6.QtWidgets import QMessageBox
        question.return_value = QMessageBox.StandardButton.Yes
        self.widget.restart_asf()
        restart.assert_called_once_with(confirmed=True)
        self.assertIn("отправлена", self.widget.status.text())

    @patch("app.asf_management.QMessageBox.exec")
    @patch("app.asf_management.ASFClient.get_bot_inventory")
    def test_inventory_uses_selected_bot_and_displays_response(self, get_inventory, dialog_exec):
        get_inventory.return_value = {"Result": [{"assetid": "123", "name": "Trading Card"}]}
        self.widget.table.setRowCount(1)
        from PySide6.QtWidgets import QTableWidgetItem
        self.widget.table.setItem(0, 0, QTableWidgetItem("card-bot"))
        self.widget.table.selectRow(0)
        self.widget.show_inventory()
        get_inventory.assert_called_once_with("card-bot")
        self.assertIn("Инвентарь получен", self.widget.status.text())
        dialog_exec.assert_called_once()

    def test_inventory_sanitization_removes_nested_secrets(self):
        source = {
            "items": [{"assetid": "42", "name": "Card", "metadata": {"api_key": "hidden"}}],
            "authorization": "bearer-secret",
        }
        safe = self.widget._sanitize_inventory(source)
        self.assertEqual(safe, {"items": [{"assetid": "42", "name": "Card", "metadata": {}}]})

    @patch("app.asf_management.QFileDialog.getSaveFileName")
    def test_inventory_export_saves_last_inventory_as_json(self, choose_path):
        with tempfile.TemporaryDirectory() as temp:
            target = str(Path(temp) / "inventory.json")
            choose_path.return_value = (target, "JSON (*.json)")
            self.widget._last_inventory = {"items": [{"assetid": "42", "password": "must-not-export"}]}
            self.widget._last_inventory_export_payload = self.widget._sanitize_inventory(self.widget._last_inventory)
            self.widget._last_inventory_bot = "card-bot"
            self.widget.export_inventory()
            saved_text = Path(target).read_text(encoding="utf-8")
            saved = json.loads(saved_text)
            self.assertNotIn("must-not-export", saved_text)
        self.assertEqual(saved, {"items": [{"assetid": "42"}]})
        self.assertIn("сохранён в JSON", self.widget.status.text())

    def test_inventory_export_requires_loaded_inventory(self):
        self.widget.export_inventory()
        self.assertIn("Сначала загрузи инвентарь", self.widget.status.text())

    @patch("app.asf_management.ASFClient.get_bot_inventory")
    def test_inventory_requires_selected_bot(self, get_inventory):
        self.widget.show_inventory()
        get_inventory.assert_not_called()
        self.assertIn("выбери бота", self.widget.status.text())

    def test_snapshot_refresh_uses_selected_bot_and_ipc_password(self):
        from PySide6.QtWidgets import QTableWidgetItem
        self.widget.table.setRowCount(1)
        self.widget.table.setItem(0, 0, QTableWidgetItem("card-bot"))
        self.widget.table.selectRow(0)
        self.widget.ipc_password.setText("local-ipc-secret")
        with patch("app.asf_management.ASFInventoryController") as controller:
            controller.return_value.refresh.return_value = {"item_count": 12}
            self.widget.refresh_inventory_snapshot()
            controller.return_value.refresh.assert_called_once_with("card-bot", "local-ipc-secret")
        self.assertIn("Элементов: 12", self.widget.status.text())

    def test_successful_snapshot_refresh_emits_bot_name_for_inventory_tab(self):
        from PySide6.QtWidgets import QTableWidgetItem
        emitted = []
        self.widget.inventory_snapshot_updated.connect(emitted.append)
        self.widget.table.setRowCount(1)
        self.widget.table.setItem(0, 0, QTableWidgetItem("card-bot"))
        self.widget.table.selectRow(0)
        with patch("app.asf_management.ASFInventoryController") as controller:
            controller.return_value.refresh.return_value = {"item_count": 12}
            self.widget.refresh_inventory_snapshot()
        self.assertEqual(emitted, ["card-bot"])

    @patch("app.asf_management.ASFClient.get_bots")
    def test_failed_bot_refresh_clears_stale_state_and_export(self, get_bots):
        from PySide6.QtWidgets import QTableWidgetItem
        self.widget.table.setRowCount(1)
        self.widget.table.setItem(0, 0, QTableWidgetItem("old-bot"))
        self.widget._last_inventory = {"items": [{"assetid": "1"}]}
        self.widget._last_inventory_bot = "old-bot"
        self.widget._last_inventory_export_payload = {"items": [{"assetid": "1"}]}
        self.widget.export_inventory_button.setEnabled(True)
        get_bots.side_effect = OSError("IPC unavailable")

        self.widget.refresh_bots()

        self.assertEqual(self.widget.table.rowCount(), 0)
        self.assertIsNone(self.widget._last_inventory)
        self.assertIsNone(self.widget._last_inventory_bot)
        self.assertIsNone(self.widget._last_inventory_export_payload)
        self.assertFalse(self.widget.export_inventory_button.isEnabled())
        self.assertIn("Список очищен", self.widget.status.text())

    @patch("app.asf_management.QMessageBox.warning")
    def test_export_rejects_invalid_name_before_file_dialog(self, warning):
        with patch("app.asf_management.QFileDialog.getSaveFileName") as choose_path:
            self.widget.bot_name.setText("../unsafe")
            self.widget.export_config()
        choose_path.assert_not_called()
        warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
