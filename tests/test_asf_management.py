import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QTableWidgetItem
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

    @patch("app.asf_management.QMessageBox.question")
    @patch("app.asf_management.QMessageBox.information")
    @patch("app.asf_management.QMessageBox.warning")
    def test_import_accounts_uses_first_delimiter_when_password_contains_other_separators(
        self, warning, information, question
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            txt = root / "accounts.txt"
            ma_dir = root / "maFiles"
            config_dir = root / "config"
            ma_dir.mkdir()
            config_dir.mkdir()
            login = "bot-account"
            password = "pw;with|delimiters:and-more"
            txt.write_text(f"{login}:{password}\n", encoding="utf-8")
            (ma_dir / f"{login}.maFile").write_text(
                json.dumps({"shared_secret": "fake-secret", "identity_secret": "fake-identity"}),
                encoding="utf-8",
            )

            self.widget.txt_path.setText(str(txt))
            self.widget.mafiles_path.setText(str(ma_dir))
            self.widget.config_path.setText(str(config_dir))
            question.return_value = QMessageBox.StandardButton.Yes

            self.widget.import_accounts()

            target = config_dir / f"{login}.json"
            self.assertTrue(target.is_file(), "A valid account row must import without a warning")
            imported = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(imported["SteamLogin"], login)
            self.assertEqual(imported["SteamPassword"], password)
            warning.assert_not_called()

    @patch("app.asf_management.ASFClient.get_status", return_value={"Success": True})
    @patch("app.asf_management.ASFClient.start_bot")
    @patch("app.asf_management.QMessageBox.question")
    def test_start_selected_sends_command_only_for_selected_bot(self, question, start_bot, get_status):
        question.return_value = QMessageBox.StandardButton.Yes
        self.widget.table.setRowCount(2)
        self.widget.table.setItem(0, 0, QTableWidgetItem("bot-a"))
        self.widget.table.setItem(0, 1, QTableWidgetItem("Отключён"))
        self.widget.table.setItem(1, 0, QTableWidgetItem("bot-b"))
        self.widget.table.setItem(1, 1, QTableWidgetItem("Отключён"))
        self.widget.table.selectRow(0)
        with patch.object(self.widget, "refresh_bots"):
            self.widget.start_selected_bots()
        start_bot.assert_called_once_with("bot-a")

    @patch("app.asf_management.ASFClient.get_bots", return_value={"bot-a": False, "bot-b": False})
    @patch("app.asf_management.ASFClient.start_bot")
    @patch("app.asf_management.QMessageBox.question")
    def test_start_all_sends_start_command_for_each_bot(self, question, start_bot, get_bots):
        question.return_value = QMessageBox.StandardButton.Yes
        with patch.object(self.widget, "refresh_bots"):
            self.widget.start_all_bots()
        self.assertEqual([call.args[0] for call in start_bot.call_args_list], ["bot-a", "bot-b"])

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

    @patch("app.asf_management.QTimer.singleShot")
    @patch("app.asf_management.ASFClient.restart", side_effect=OSError("IPC unavailable"))
    @patch("app.asf_management.QMessageBox.question")
    def test_restart_action_launches_asf_when_not_running(self, question, restart, single_shot):
        from PySide6.QtWidgets import QMessageBox
        question.return_value = QMessageBox.StandardButton.Yes
        with patch.object(self.widget, "_ipc_port_is_open", return_value=False), \
             patch.object(self.widget, "start_asf") as start_asf:
            self.widget.restart_asf()
        restart.assert_called_once_with(confirmed=True)
        start_asf.assert_called_once_with()
        self.assertIn("Запускаю его через GameDelivery", self.widget.status.text())

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



    @patch("app.asf_management.ASFClient.get_bots", return_value={})
    def test_refresh_falls_back_to_disabled_configs_when_asf_has_no_active_bots(self, get_bots):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)
            (config / "bot-a.json").write_text(
                json.dumps({"Enabled": False, "SteamPassword": "never-display"}), encoding="utf-8"
            )
            (config / "bot-b.json").write_text(
                json.dumps({"Enabled": True}), encoding="utf-8"
            )
            (config / "ASF.json").write_text(json.dumps({"IPCPassword": "secret"}), encoding="utf-8")
            self.widget.config_path.setText(str(config))

            self.widget.refresh_bots()

            self.assertEqual(self.widget.table.rowCount(), 2)
            self.assertEqual(self.widget.table.item(0, 0).text(), "bot-a")
            self.assertEqual(self.widget.table.item(0, 1).text(), "Отключён (config)")
            self.assertEqual(self.widget.table.item(1, 0).text(), "bot-b")
            self.assertNotIn("never-display", self.widget.status.text())
            self.assertNotIn("secret", self.widget.status.text())
            self.assertIn("Найдено конфигураций: 2", self.widget.status.text())

    @patch("app.asf_management.ASFClient.get_bots", side_effect=OSError("IPC unavailable"))
    def test_refresh_falls_back_to_configs_when_ipc_fails(self, get_bots):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "bot-a.json").write_text(json.dumps({"Enabled": False}), encoding="utf-8")
            self.widget.config_path.setText(temp)

            self.widget.refresh_bots()

            self.assertEqual(self.widget.table.rowCount(), 1)
            self.assertEqual(self.widget.table.item(0, 0).text(), "bot-a")
            self.assertIn("статусы активности неизвестны", self.widget.status.text())


    @patch("app.asf_management.QFileDialog.getOpenFileName")
    def test_selecting_asf_executable_auto_detects_sibling_config(self, choose_file):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            executable = home / "ArchiSteamFarm.exe"
            executable.write_bytes(b"test")
            config = home / "config"
            config.mkdir()
            (config / "bot-a.json").write_text(json.dumps({"Enabled": False}), encoding="utf-8")
            choose_file.return_value = (str(executable), "Executable (*.exe)")
            self.widget.choose_asf_executable()
            self.assertEqual(self.widget.asf_executable_path.text(), str(executable))
            self.assertEqual(self.widget.config_path.text(), str(config))

    @patch("app.asf_management.ASFClient.get_bots", return_value={})
    def test_config_scan_ignores_non_bot_json_and_asf_global_config(self, get_bots):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)
            (config / "bot-a.json").write_text(
                json.dumps({"Enabled": False, "SteamLogin": "user-a"}), encoding="utf-8"
            )
            (config / "ASF.json").write_text(
                json.dumps({"IPCPassword": "must-not-list"}), encoding="utf-8"
            )
            (config / "notes.json").write_text(
                json.dumps({"description": "not an ASF bot"}), encoding="utf-8"
            )
            self.widget.config_path.setText(temp)
            self.widget.refresh_bots()
            self.assertEqual(self.widget.table.rowCount(), 1)
            self.assertEqual(self.widget.table.item(0, 0).text(), "bot-a")

    def test_refresh_merges_disabled_configs_with_active_ipc_bots(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)
            (config / "bot-disabled.json").write_text(
                json.dumps({"Enabled": False}), encoding="utf-8"
            )
            (config / "bot-active.json").write_text(
                json.dumps({"Enabled": True}), encoding="utf-8"
            )
            self.widget.config_path.setText(str(config))
            with patch("app.asf_management.ASFClient.get_bots", return_value={"bot-active": True}):
                self.widget.refresh_bots()
            self.assertEqual(self.widget.table.rowCount(), 2)
            rows = {
                self.widget.table.item(row, 0).text(): self.widget.table.item(row, 1).text()
                for row in range(self.widget.table.rowCount())
            }
            self.assertEqual(rows["bot-disabled"], "Отключён (config)")
            self.assertEqual(rows["bot-active"], "Запущен")
            self.assertIn("всего конфигураций: 2", self.widget.status.text())

    @patch("app.asf_management.QMessageBox.question")
    def test_start_all_queues_configured_bots_and_launches_asf_when_ipc_is_down(self, question):
        question.return_value = QMessageBox.StandardButton.Yes
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "bot-a.json").write_text(json.dumps({"Enabled": False}), encoding="utf-8")
            self.widget.config_path.setText(temp)
            with patch("app.asf_management.ASFClient.get_bots", side_effect=OSError("IPC unavailable")), \
                 patch.object(self.widget, "start_asf") as start_asf:
                self.widget.start_all_bots()
                self.assertEqual(self.widget._pending_start_bot_names, ["bot-a"])
                start_asf.assert_called_once_with()

    def test_asf_log_sanitizer_redacts_credentials_and_proxy_uri(self):
        raw = '{"SteamPassword":"steam-secret","IPCPassword":"ipc-secret"} socks5://proxy-user:proxy-secret@proxy.example:1080'
        safe = self.widget._sanitize_asf_output_line(raw)
        for secret in ("steam-secret", "ipc-secret", "proxy-user", "proxy-secret"):
            self.assertNotIn(secret, safe)
        self.assertIn("[REDACTED]", safe)

    def test_asf_output_lines_are_published_for_main_journal(self):
        emitted = []
        self.widget.log_line.connect(emitted.append)
        self.widget._on_asf_output_line("ASF ready")
        self.assertEqual(emitted, ["ASF ready"])
        self.assertIn("ASF ready", self.widget._asf_console_lines)

    def test_ipc_poll_stops_waiting_when_managed_asf_exits_early(self):
        from types import SimpleNamespace

        self.widget._asf_process = SimpleNamespace(poll=lambda: 17)
        with patch.object(self.widget, "_close_asf_log") as close_log, \
             patch.object(self.widget, "refresh_bots") as refresh_bots, \
             patch("app.asf_management.QTimer.singleShot") as single_shot:
            self.widget._poll_asf_ipc()

        close_log.assert_called_once_with()
        refresh_bots.assert_called_once_with()
        single_shot.assert_not_called()
        self.assertIsNone(self.widget._asf_process)
        self.assertIn("код выхода 17", self.widget.status.text())
        self.assertIn("до готовности IPC", self.widget.status.text())

    @patch("app.asf_management.QTimer.singleShot")
    @patch("app.asf_management.subprocess.Popen")
    @patch("app.asf_management.ASFClient.get_status", side_effect=OSError("IPC unavailable"))
    @patch("app.asf_management.ASFOutputReader.start")
    def test_start_asf_launches_process_with_process_required(self, reader_start, get_status, popen, single_shot):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            executable = home / "ArchiSteamFarm.exe"
            executable.write_bytes(b"test executable")
            process = popen.return_value
            process.poll.return_value = None
            self.widget.asf_executable_path.setText(str(executable))
            with patch("app.asf_management.Path.home", return_value=home), \
                 patch.object(self.widget, "_ipc_port_is_open", return_value=False):
                self.widget.start_asf()
            args, kwargs = popen.call_args
            self.assertEqual(args[0], [str(executable), "--process-required"])
            self.assertEqual(kwargs["cwd"], str(home))
            self.assertTrue((home / ".gamedelivery" / "asf-supervisor.log").is_file())
            self.assertIn("GameDelivery запустил ASF", self.widget.status.text())
            self.widget._close_asf_log()
            self.widget._asf_process = None

    def test_import_rolls_back_partial_mafile_and_prior_accounts_on_copy_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            txt = root / "accounts.txt"
            mafiles = root / "maFiles"
            config = root / "config"
            mafiles.mkdir()
            config.mkdir()
            txt.write_text("login1:password1\nlogin2:password2\n", encoding="utf-8")
            (mafiles / "login1.maFile").write_text('{"account_name":"login1"}', encoding="utf-8")
            (mafiles / "login2.maFile").write_text('{"account_name":"login2"}', encoding="utf-8")

            self.widget.txt_path.setText(str(txt))
            self.widget.mafiles_path.setText(str(mafiles))
            self.widget.config_path.setText(str(config))

            def copy_with_partial_failure(source, destination):
                source_path = Path(source)
                destination_path = Path(destination)
                destination_path.write_bytes(source_path.read_bytes())
                if source_path.name == "login2.maFile":
                    raise OSError("simulated copy error")

            with patch(
                "app.asf_management.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ), patch("app.asf_management.QMessageBox.warning") as warning, patch(
                "app.asf_management.QMessageBox.information"
            ), patch(
                "app.asf_management.shutil.copy2",
                side_effect=copy_with_partial_failure,
            ):
                self.widget.import_accounts()

            warning.assert_called_once()
            self.assertEqual(list(config.iterdir()), [])
            self.assertIn("Импорт не выполнен", warning.call_args.args[1])


    def test_enable_bots_rolls_back_current_json_when_write_is_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)
            target = config / "bot-a.json"
            original = b'{"Enabled": false, "SteamLogin": "bot-a"}\n'
            target.write_bytes(original)
            self.widget.config_path.setText(str(config))
            original_write_text = Path.write_text

            def write_with_partial_failure(path, data, encoding=None, errors=None, newline=None):
                if path == target:
                    path.write_bytes(b'{"Enabled":')
                    raise OSError("simulated partial write")
                return original_write_text(
                    path, data, encoding=encoding, errors=errors, newline=newline
                )

            with patch(
                "app.asf_management.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ), patch(
                "pathlib.Path.write_text", autospec=True, side_effect=write_with_partial_failure
            ), patch("app.asf_management.QMessageBox.warning") as warning:
                self.widget._set_bots_enabled(["bot-a"])

            warning.assert_called_once()
            self.assertEqual(target.read_bytes(), original)

    def test_shutdown_managed_asf_terminates_process_and_joins_reader(self):
        process = unittest.mock.Mock()
        process.poll.side_effect = [None, None, 0]
        reader = unittest.mock.Mock()
        reader.isRunning.side_effect = [True, False]
        log_handle = unittest.mock.Mock()
        self.widget._asf_process = process
        self.widget._asf_output_reader = reader
        self.widget._asf_log_handle = log_handle

        with patch.object(self.widget, "_client") as client:
            client.return_value.exit.side_effect = OSError("IPC unavailable")
            self.widget.shutdown_managed_asf()

        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)
        reader.wait.assert_called_once_with(1500)
        log_handle.close.assert_called_once()
        self.assertIsNone(self.widget._asf_log_handle)

    def test_shutdown_managed_asf_kills_process_if_graceful_termination_times_out(self):
        process = unittest.mock.Mock()
        process.poll.side_effect = [None, None, None, 0]
        process.wait.side_effect = [subprocess.TimeoutExpired(cmd="ASF", timeout=3), None]
        reader = unittest.mock.Mock()
        reader.isRunning.return_value = False
        self.widget._asf_process = process
        self.widget._asf_output_reader = reader

        with patch.object(self.widget, "_client") as client:
            client.return_value.exit.side_effect = OSError("IPC unavailable")
            self.widget.shutdown_managed_asf()

        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_args_list, [
            unittest.mock.call(timeout=3),
            unittest.mock.call(timeout=2),
        ])


    def test_shutdown_requests_reader_interruption_before_waiting_and_closing_log(self):
        process = unittest.mock.Mock()
        process.poll.return_value = 0
        reader = unittest.mock.Mock()
        reader.isRunning.return_value = True
        log_handle = unittest.mock.Mock()
        events = []
        reader.requestInterruption.side_effect = lambda: events.append("interrupt")
        reader.wait.side_effect = lambda timeout: events.append(f"wait:{timeout}")
        log_handle.close.side_effect = lambda: events.append("close")
        self.widget._asf_process = process
        self.widget._asf_output_reader = reader
        self.widget._asf_log_handle = log_handle

        with patch.object(self.widget, "_client") as client:
            client.return_value.exit.return_value = None
            self.widget.shutdown_managed_asf()

        reader.requestInterruption.assert_called_once_with()
        self.assertEqual(
            reader.wait.call_args_list,
            [unittest.mock.call(1500), unittest.mock.call(1000)],
        )
        log_handle.close.assert_called_once()
        self.assertEqual(events, ["interrupt", "wait:1500", "wait:1000", "close"])



if __name__ == "__main__":
    unittest.main()
