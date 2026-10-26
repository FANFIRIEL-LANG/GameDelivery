from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from integrations.asf.client import ASFClient, ASFError


class ASFManagementWidget(QWidget):
    """Read active ASF bots and export a disabled, reviewable bot config."""

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        intro = QLabel(
            "Управление ASF через локальный IPC. Список показывает уже загруженных ботов. "
            "Новая конфигурация сохраняется отдельно для ручной проверки и импорта; "
            "бот не создаётся автоматически."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        form = QFormLayout()
        self.ipc_password = QLineEdit()
        self.ipc_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.ipc_password.setPlaceholderText("IPCPassword из локальной конфигурации ASF")
        form.addRow("Пароль ASF IPC", self.ipc_password)
        self.bot_name = QLineEdit()
        self.bot_name.setPlaceholderText("Например, card-bot-2")
        form.addRow("Имя нового бота", self.bot_name)
        self.steam_login = QLineEdit()
        self.steam_login.setPlaceholderText("Необязательно; Steam-пароль не запрашивается")
        form.addRow("SteamLogin", self.steam_login)
        root.addLayout(form)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Обновить список ботов")
        self.export_button = QPushButton("Сохранить конфигурацию нового бота")
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.export_button)
        buttons.addStretch()
        root.addLayout(buttons)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Имя бота", "Состояние"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table, 1)
        self.status = QLabel("ASF не запрашивался.")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.refresh_button.clicked.connect(self.refresh_bots)
        self.export_button.clicked.connect(self.export_config)

    def _client(self) -> ASFClient:
        return ASFClient(ipc_password=self.ipc_password.text() or None)

    def refresh_bots(self):
        self.refresh_button.setEnabled(False)
        try:
            bots = self._client().get_bots()
            self.table.setRowCount(0)
            for name, details in sorted(bots.items(), key=lambda pair: pair[0].casefold()):
                row = self.table.rowCount()
                self.table.insertRow(row)
                self.table.setItem(row, 0, QTableWidgetItem(str(name)))
                if isinstance(details, dict):
                    state = str(details.get("KeepRunning", details.get("IsConnected", "Загружен")))
                else:
                    state = "Загружен"
                self.table.setItem(row, 1, QTableWidgetItem(state))
            self.status.setText(f"Получено ботов: {len(bots)}. Секреты в таблицу не выводятся.")
        except (ASFError, ValueError, OSError) as exc:
            self.status.setText(f"Не удалось получить список ASF: {exc}")
        finally:
            self.refresh_button.setEnabled(True)

    def export_config(self):
        name = self.bot_name.text().strip()
        login = self.steam_login.text().strip() or None
        try:
            filename, payload = ASFClient.serialize_new_bot_config(name, steam_login=login)
        except ValueError as exc:
            QMessageBox.warning(self, "Конфигурация ASF", str(exc))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить конфигурацию ASF", str(Path.home() / filename), "JSON (*.json)"
        )
        if not path:
            return
        try:
            Path(path).write_text(payload, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Конфигурация ASF", f"Не удалось сохранить файл: {exc}")
            return
        self.status.setText(
            f"Конфигурация сохранена: {path}. Enabled=false; Steam-пароль не включён. "
            "Для создания бота вручную проверь файл и импортируй его в каталог config ASF."
        )
