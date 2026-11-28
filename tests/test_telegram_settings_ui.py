from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from app.telegram_settings import TelegramSettingsWidget
from integrations.telegram.outbox import TelegramCredentialStore


class _Keyring:
    def __init__(self):
        self.values = {}

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def get_password(self, service, account):
        return self.values.get((service, account))

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class TelegramSettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_event_filter_persists_without_credentials_or_api_call(self):
        with tempfile.TemporaryDirectory() as temp:
            settings_path = Path(temp) / "settings.ini"
            settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
            credentials = TelegramCredentialStore(_Keyring())
            first = TelegramSettingsWidget(settings=settings, credentials=credentials)
            first.event_checks["unknown"].setChecked(False)
            first.save()
            settings.sync()
            first.deleteLater()

            restored_settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
            second = TelegramSettingsWidget(settings=restored_settings, credentials=credentials)
            self.assertFalse(second.event_checks["unknown"].isChecked())
            self.assertTrue(second.event_checks["new_order"].isChecked())
            second.deleteLater()
            self.app.processEvents()

    def test_clearing_proxy_removes_stale_keyring_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = _Keyring()
            settings = QSettings(str(Path(temp) / "settings.ini"), QSettings.Format.IniFormat)
            credentials = TelegramCredentialStore(backend)
            credentials.configure(
                bot_token="123456:secret",
                proxy_url="socks5h://proxy.example:1080",
                proxy_username="proxy-user",
                proxy_password="proxy-secret",
            )
            widget = TelegramSettingsWidget(settings=settings, credentials=credentials)
            widget.token.setText("123456:secret")
            widget.proxy.clear()
            widget.proxy_user.clear()
            widget.save()
            self.assertIsNone(credentials.proxy())
            self.assertNotIn((credentials.SERVICE, "proxy_password"), backend.values)
            widget.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
