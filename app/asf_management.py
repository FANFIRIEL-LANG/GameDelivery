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
        self.restart_button = QPushButton("Перезапустить ASF…")
        self.inventory_button = QPushButton("Показать инвентарь выбранного бота")
        self.export_inventory_button = QPushButton("Экспорт инвентаря JSON")
        self._last_inventory = None
        self._last_inventory_bot = None
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.restart_button)
        buttons.addWidget(self.inventory_button)
        buttons.addWidget(self.export_inventory_button)
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
        self.restart_button.clicked.connect(self.restart_asf)
        self.inventory_button.clicked.connect(self.show_inventory)
        self.export_inventory_button.clicked.connect(self.export_inventory)
        self.export_inventory_button.setEnabled(False)

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

    def restart_asf(self):
        answer = QMessageBox.question(
            self,
            "Перезапуск ASF",
            "Перезапустить весь экземпляр ASF? Это временно отключит все боты. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText("Перезапуск ASF отменён.")
            return
        self.restart_button.setEnabled(False)
        try:
            self._client().restart(confirmed=True)
            self.status.setText(
                "Команда перезапуска отправлена ASF. Подключение может временно пропасть."
            )
        except (ASFError, ValueError, OSError) as exc:
            self.status.setText(f"Не удалось перезапустить ASF: {exc}")
        finally:
            self.restart_button.setEnabled(True)

    def show_inventory(self):
        row = self.table.currentRow()
        if row < 0 or self.table.item(row, 0) is None:
            self.status.setText("Сначала выбери бота в списке.")
            return
        bot_name = self.table.item(row, 0).text()
        self.inventory_button.setEnabled(False)
        try:
            payload = self._client().get_bot_inventory(bot_name)
            self._last_inventory = payload
            self._last_inventory_bot = bot_name
            self.export_inventory_button.setEnabled(True)
            if isinstance(payload, dict):
                safe_payload = dict(payload)
                for key in list(safe_payload):
                    if any(secret in str(key).casefold() for secret in ("password", "token", "secret", "apikey")):
                        safe_payload.pop(key, None)
                rendered = json.dumps(safe_payload, ensure_ascii=False, indent=2)
            else:
                rendered = json.dumps(payload, ensure_ascii=False, indent=2)
            dialog = QMessageBox(self)
            dialog.setWindowTitle(f"Инвентарь ASF — {bot_name}")
            dialog.setText(f"Ответ ASF для бота {bot_name}")
            dialog.setDetailedText(rendered)
            dialog.setIcon(QMessageBox.Icon.Information)
            dialog.exec()
            self.status.setText(f"Инвентарь получен через ASF для бота {bot_name}.")
        except (ASFError, ValueError, OSError) as exc:
            self.status.setText(f"Не удалось получить инвентарь {bot_name}: {exc}")
        finally:
            self.inventory_button.setEnabled(True)

    def export_inventory(self):
        if self._last_inventory is None or not self._last_inventory_bot:
            self.status.setText("Сначала загрузи инвентарь выбранного бота.")
            return
        default_name = f"{self._last_inventory_bot}-inventory.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт инвентаря ASF", str(Path.home() / default_name), "JSON (*.json)"
        )
        if not path:
            return
        try:
            payload = json.dumps(self._last_inventory, ensure_ascii=False, indent=2)
            Path(path).write_text(payload, encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "Экспорт инвентаря", f"Не удалось сохранить JSON: {exc}")
            return
        self.status.setText(f"Инвентарь бота {self._last_inventory_bot} сохранён в JSON.")
