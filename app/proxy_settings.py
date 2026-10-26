from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from integrations.asf.proxy_config import SteamProxy
from integrations.asf.proxy_store import ProxyStore


def _database_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    folder = base / "GameDelivery"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "gamedelivery.sqlite3"


class ProxySettingsWidget(QWidget):
    """Manage per-ASF-bot proxy profiles; does not apply them to a live ASF process yet."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = ProxyStore(_database_path())
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        intro = QLabel(
            "Один прокси на Steam-аккаунт ASF. Цель настройки — веб-запросы и "
            "Steam-соединение через WebSocket. Пока профили сохраняются локально; "
            "применение к работающему ASF ещё не включено."
        )
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        root.addWidget(intro)

        form = QFormLayout()
        self.bot_name = QLineEdit()
        self.bot_name.setPlaceholderText("Имя бота из ASF")
        self.scheme = QComboBox()
        self.scheme.addItems(["http", "https", "socks4", "socks4a", "socks5"])
        self.host = QLineEdit()
        self.host.setPlaceholderText("proxy.example")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(1080)
        self.username = QLineEdit()
        self.username.setPlaceholderText("Необязательно")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Необязательно")
        form.addRow("Steam-аккаунт / ASF bot", self.bot_name)
        form.addRow("Тип прокси", self.scheme)
        form.addRow("Сервер", self.host)
        form.addRow("Порт", self.port)
        form.addRow("Логин прокси", self.username)
        form.addRow("Пароль прокси", self.password)
        root.addLayout(form)

        buttons = QHBoxLayout()
        save = QPushButton("Сохранить профиль")
        save.clicked.connect(self.save_profile)
        clear = QPushButton("Очистить поля")
        clear.clicked.connect(self.clear_fields)
        delete = QPushButton("Удалить выбранный")
        delete.clicked.connect(self.delete_selected)
        buttons.addWidget(save)
        buttons.addWidget(clear)
        buttons.addStretch()
        buttons.addWidget(delete)
        root.addLayout(buttons)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["ASF-бот", "Тип", "Сервер", "Порт", "Авторизация", "Режим"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self.load_selected)
        root.addWidget(self.table, 1)

        self.status = QLabel("Пароли не отображаются в таблице и не сохраняются в обычных настройках.")
        self.status.setWordWrap(True)
        self.status.setObjectName("muted")
        root.addWidget(self.status)

    def refresh(self):
        self.table.setRowCount(0)
        for profile in self.store.list_profiles():
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                profile["bot_name"], profile["scheme"], profile["host"], str(profile["port"]),
                "Да" if profile["username"] else "Нет", "Сохранён · не применён",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, profile["bot_name"])
                self.table.setItem(row, column, item)

    def save_profile(self):
        bot_name = self.bot_name.text().strip()
        username = self.username.text().strip() or None
        password = self.password.text() or None
        if bool(username) != bool(password):
            QMessageBox.warning(self, "Прокси", "Укажи и логин, и пароль прокси либо оставь оба поля пустыми.")
            return
        try:
            proxy = SteamProxy(
                self.scheme.currentText(), self.host.text(), self.port.value(), username, password
            )
            self.store.save(bot_name, proxy)
        except (ValueError, RuntimeError, OSError) as exc:
            QMessageBox.warning(self, "Не удалось сохранить", str(exc))
            return
        self.password.clear()
        self.refresh()
        self.status.setText("Профиль сохранён локально. К работающему ASF он ещё не применён.")

    def load_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return
        bot_item = self.table.item(row, 0)
        if bot_item is None:
            return
        profile_name = bot_item.data(Qt.ItemDataRole.UserRole) or bot_item.text()
        profile = next((p for p in self.store.list_profiles() if p["bot_name"] == profile_name), None)
        if not profile:
            return
        self.bot_name.setText(str(profile["bot_name"]))
        self.scheme.setCurrentText(str(profile["scheme"]))
        self.host.setText(str(profile["host"]))
        self.port.setValue(int(profile["port"]))
        self.username.setText(str(profile["username"] or ""))
        self.password.clear()

    def delete_selected(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Прокси", "Сначала выбери профиль в таблице.")
            return
        bot_item = self.table.item(row, 0)
        if bot_item is None:
            return
        bot_name = str(bot_item.data(Qt.ItemDataRole.UserRole) or bot_item.text())
        answer = QMessageBox.question(
            self, "Удалить прокси", f"Удалить сохранённый профиль для {bot_name}?"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.delete(bot_name)
        except (RuntimeError, OSError) as exc:
            QMessageBox.warning(self, "Не удалось удалить", str(exc))
            return
        self.clear_fields()
        self.refresh()
        self.status.setText("Профиль удалён из локального хранилища. Настройки уже работающего ASF не менялись.")

    def clear_fields(self):
        self.bot_name.clear()
        self.scheme.setCurrentText("http")
        self.host.clear()
        self.port.setValue(1080)
        self.username.clear()
        self.password.clear()
        self.table.clearSelection()
