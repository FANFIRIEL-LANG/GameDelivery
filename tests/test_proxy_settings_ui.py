import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from app.proxy_settings import ProxySettingsWidget
from integrations.asf.proxy_config import SteamProxy
from integrations.asf.proxy_store import ProxyStore


class PasswordDeleteError(Exception):
    pass


class FakeKeyring:
    class errors:
        PasswordDeleteError = PasswordDeleteError
        PasswordNotFoundException = PasswordDeleteError

    def __init__(self):
        self.priority = 1
        self.passwords = {}

    def get_keyring(self):
        return self

    def get_password(self, service, username):
        return self.passwords.get((service, username))

    def set_password(self, service, username, password):
        self.passwords[(service, username)] = password

    def delete_password(self, service, username):
        key = (service, username)
        if key not in self.passwords:
            raise PasswordDeleteError("missing")
        del self.passwords[key]


class ProxySettingsUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = Path(self.temp_dir.name) / "gamedelivery.sqlite3"
        self.keyring = FakeKeyring()
        self.database_patch = patch("app.proxy_settings._database_path", return_value=self.database)
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        self.keyring_patch = patch.object(ProxyStore, "_keyring", return_value=self.keyring)
        self.keyring_patch.start()
        self.addCleanup(self.keyring_patch.stop)
        self.widget = ProxySettingsWidget()
        self.addCleanup(self._close_widget)

    def _close_widget(self):
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def test_import_proxy_txt_populates_pool_without_showing_passwords(self):
        txt = Path(self.temp_dir.name) / "proxies.txt"
        txt.write_text(
            "\n".join([
                "socks5://one.example:1080;proxy-user;hidden-pass-one",
                "http://two.example:8080",
                "socks5://three.example:1082|other-user|hidden-pass-three",
            ]),
            encoding="utf-8",
        )
        self.widget.proxy_txt_path.setText(str(txt))
        with patch(
            "app.proxy_settings.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ), patch("app.proxy_settings.QMessageBox.warning") as warning:
            self.widget.import_proxy_txt()

        warning.assert_not_called()
        pool = self.widget.store.list_pool()
        self.assertEqual([item["host"] for item in pool], [
            "one.example", "two.example", "three.example"
        ])
        self.assertEqual(self.widget.pool_table.rowCount(), 3)
        self.assertEqual(self.widget.store.get_pool_password(pool[0]["proxy_id"]), "hidden-pass-one")
        self.assertEqual(self.widget.store.get_pool_password(pool[2]["proxy_id"]), "hidden-pass-three")
        visible = "\n".join(
            [self.widget.pool_status.text()]
            + [self.widget.pool_table.item(row, col).text()
               for row in range(self.widget.pool_table.rowCount())
               for col in range(self.widget.pool_table.columnCount())
               if self.widget.pool_table.item(row, col) is not None]
        )
        self.assertNotIn("hidden-pass-one", visible)
        self.assertNotIn("hidden-pass-three", visible)

    def test_apply_selected_sends_only_proxy_fields_and_skips_global_change_when_declined(self):
        self.widget.store.save(
            "bot-a",
            SteamProxy("socks5", "proxy.example", 1080, "proxy-user", "private-proxy-password"),
        )
        self.widget.refresh()
        self.assertGreater(self.widget.table.rowCount(), 0)
        self.widget.table.selectRow(0)
        client = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()

        with patch.object(self.widget, "_ask_ipc_password", return_value=("ipc-password", True)), \
             patch("app.proxy_settings.ASFClient", return_value=client), \
             patch(
                 "app.proxy_settings.QMessageBox.question",
                 return_value=QMessageBox.StandardButton.No,
             ) as question, \
             patch("app.proxy_settings.QMessageBox.warning") as warning:
            self.widget.apply_selected()

        client.apply_proxy_settings.assert_called_once_with(
            "bot-a",
            {
                "WebProxy": "socks5://proxy.example:1080",
                "WebProxyUsername": "proxy-user",
                "WebProxyPassword": "private-proxy-password",
            },
        )
        question.assert_called_once()
        warning.assert_not_called()
        self.assertNotIn("private-proxy-password", self.widget.status.text())
        self.assertNotIn("private-proxy-password", self.widget.pool_status.text())

    def test_auto_distribute_writes_disabled_bot_configs_before_any_bot_starts(self):
        import json
        from PySide6.QtCore import QSettings

        config_dir = Path(self.temp_dir.name) / "asf-config"
        config_dir.mkdir()
        (config_dir / "bot-a.json").write_text(json.dumps({"Enabled": False}), encoding="utf-8")
        (config_dir / "bot-b.json").write_text(json.dumps({"Enabled": False}), encoding="utf-8")
        self.widget.store.add_pool_proxies([
            SteamProxy("socks5", "one.example", 1080, "proxy-user", "proxy-pass"),
            SteamProxy("http", "two.example", 8080),
        ])
        settings = QSettings("FANFIRIEL-LANG", "GameDelivery")
        settings.setValue("asf/config_path", str(config_dir))
        settings.sync()
        self.addCleanup(lambda: settings.remove("asf/config_path"))

        with patch("app.proxy_settings.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \\
             patch("app.proxy_settings.ASFClient") as client, \\
             patch("app.proxy_settings.QMessageBox.warning") as warning:
            self.widget.auto_distribute_pool()

        client.assert_not_called()
        warning.assert_not_called()
        first = json.loads((config_dir / "bot-a.json").read_text(encoding="utf-8"))
        second = json.loads((config_dir / "bot-b.json").read_text(encoding="utf-8"))
        self.assertFalse(first["Enabled"])
        self.assertFalse(second["Enabled"])
        self.assertEqual(first["WebProxy"], "socks5://one.example:1080")
        self.assertEqual(first["WebProxyUsername"], "proxy-user")
        self.assertEqual(first["WebProxyPassword"], "proxy-pass")
        self.assertEqual(second["WebProxy"], "http://two.example:8080")
        self.assertIn("до запуска", self.widget.pool_status.text().casefold())

    def test_auto_distribute_pool_rotates_through_configured_bots_and_preserves_credentials(self):
        import json
        from PySide6.QtCore import QSettings

        config_dir = Path(self.temp_dir.name) / "asf-config"
        config_dir.mkdir()
        for name in ("bot-c", "bot-a", "bot-b"):
            (config_dir / f"{name}.json").write_text(json.dumps({"Enabled": False}), encoding="utf-8")
        proxies = [
            SteamProxy("socks5", "one.example", 1080, "proxy-one", "pass-one"),
            SteamProxy("http", "two.example", 8080),
        ]
        self.widget.store.add_pool_proxies(proxies)
        settings = QSettings("FANFIRIEL-LANG", "GameDelivery")
        settings.setValue("asf/config_path", str(config_dir))
        settings.sync()
        self.addCleanup(lambda: settings.remove("asf/config_path"))

        with patch("app.proxy_settings.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \\
             patch("app.proxy_settings.ASFClient") as client, \\
             patch("app.proxy_settings.QMessageBox.warning") as warning:
            self.widget.auto_distribute_pool()

        client.assert_not_called()
        warning.assert_not_called()
        profiles = {item["bot_name"]: item for item in self.widget.store.list_profiles()}
        self.assertEqual(
            {name: profiles[name]["host"] for name in ("bot-a", "bot-b", "bot-c")},
            {"bot-a": "one.example", "bot-b": "two.example", "bot-c": "one.example"},
        )
        self.assertEqual(self.widget.store.get_password("bot-a"), "pass-one")
        self.assertIsNone(self.widget.store.get_password("bot-b"))
        self.assertEqual(self.widget.store.get_password("bot-c"), "pass-one")
        for name, host in (("bot-a", "one.example"), ("bot-b", "two.example"), ("bot-c", "one.example")):
            payload = json.loads((config_dir / f"{name}.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["Enabled"])
            self.assertIn(host, payload["WebProxy"])
            self.assertTrue((config_dir / f"{name}.json.gamedelivery.bak").exists())
        self.assertIn("до запуска", self.widget.pool_status.text().casefold())



if __name__ == "__main__":
    unittest.main()
