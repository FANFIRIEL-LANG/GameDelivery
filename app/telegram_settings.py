from __future__ import annotations

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from integrations.telegram.outbox import TelegramCredentialStore, telegram_api_request, telegram_test_connection


def _ids(value: str) -> set[str]:
    return {part.strip() for part in value.replace(";", ",").split(",") if part.strip()}


class TelegramSettingsWidget(QWidget):
    status_changed = Signal(str)

    def __init__(self, *, settings: QSettings | None = None, credentials: TelegramCredentialStore | None = None, parent=None):
        super().__init__(parent)
        self.settings = settings or QSettings("FANFIRIEL-LANG", "GameDelivery")
        self.credentials = credentials or TelegramCredentialStore()
        self.token = QLineEdit(); self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.chat_id = QLineEdit(str(self.settings.value("telegram/operator_chat_id", "")))
        self.users = QLineEdit(str(self.settings.value("telegram/allowed_user_ids", "")))
        self.proxy = QLineEdit(str(self.settings.value("telegram/proxy_url", "")))
        self.proxy_user = QLineEdit(str(self.settings.value("telegram/proxy_username", "")))
        self.proxy_password = QLineEdit(); self.proxy_password.setEchoMode(QLineEdit.EchoMode.Password)
        form = QFormLayout(); form.addRow("Bot token", self.token); form.addRow("Operator chat ID", self.chat_id); form.addRow("Allowed user IDs", self.users); form.addRow("Telegram proxy URL", self.proxy); form.addRow("Proxy username", self.proxy_user); form.addRow("Proxy password", self.proxy_password)
        save = QPushButton("Сохранить настройки"); save.clicked.connect(self.save)
        connect = QPushButton("Проверить подключение"); connect.clicked.connect(self.test_connection)
        test = QPushButton("Отправить тестовое сообщение"); test.clicked.connect(self.test_message)
        row = QHBoxLayout(); row.addWidget(save); row.addWidget(connect); row.addWidget(test)
        self.status = QLabel("Telegram не проверен"); self.status.setWordWrap(True)
        layout = QVBoxLayout(self); layout.addLayout(form); layout.addLayout(row); layout.addWidget(self.status)

    def save(self):
        try:
            token = self.token.text().strip()
            if token:
                self.credentials.configure(bot_token=token, proxy_url=self.proxy.text().strip() or None, proxy_username=self.proxy_user.text().strip() or None, proxy_password=self.proxy_password.text() or None)
            self.settings.setValue("telegram/operator_chat_id", self.chat_id.text().strip())
            self.settings.setValue("telegram/allowed_user_ids", ",".join(sorted(_ids(self.users.text()))))
            self.settings.setValue("telegram/proxy_url", self.proxy.text().strip())
            self.settings.sync(); self._status("Настройки Telegram сохранены")
        except Exception as exc:
            self._status("Не удалось сохранить Telegram: " + type(exc).__name__)

    def _credentials_and_proxy(self):
        token = self.credentials.get("bot_token")
        proxy = self.credentials.proxy()
        return token, proxy

    def test_connection(self):
        try:
            token, proxy = self._credentials_and_proxy()
            result = telegram_test_connection(token or "", proxy)
            self._status("Telegram API доступен; авторизация подтверждена" if result.get("ok") else "Telegram API не подтвердил авторизацию")
        except Exception as exc:
            self._status("Проверка Telegram не пройдена: " + type(exc).__name__)

    def test_message(self):
        try:
            token, proxy = self._credentials_and_proxy(); chat_id = self.chat_id.text().strip()
            if not token or not chat_id: raise ValueError("нужны сохранённые token и operator chat ID")
            telegram_api_request(token, "sendMessage", {"chat_id": chat_id, "text": "GameDelivery: тестовое сообщение"}, proxy)
            self._status("Тестовое сообщение Telegram отправлено")
        except Exception as exc:
            self._status("Тестовое сообщение не отправлено: " + type(exc).__name__)

    def _status(self, text: str):
        self.status.setText(text); self.status_changed.emit(text)
