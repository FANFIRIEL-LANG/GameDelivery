from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QInputDialog,
)

from integrations.asf.client import ASFClient, ASFError
from integrations.asf.proxy_config import SteamProxy
from integrations.asf.proxy_store import ProxyStore


def _database_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    folder = base / "GameDelivery"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "gamedelivery.sqlite3"


class ProxySettingsWidget(QWidget):
    """Manage per-ASF-bot proxy profiles and apply them through local ASF IPC."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = ProxyStore(_database_path())
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        intro = QLabel(
            "Один прокси на ASF-бота. Применение к ASF настраивает веб-запросы. "
            "Для Steam CM потребуется отдельное глобальное изменение SteamProtocols=4, "
            "которое затронет все боты в этом экземпляре ASF."
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
        apply = QPushButton("Применить к ASF")
        apply.clicked.connect(self.apply_selected)
        delete = QPushButton("Удалить выбранный")
        delete.clicked.connect(self.delete_selected)
        buttons.addWidget(save)
        buttons.addWidget(apply)
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
                "Да" if profile["username"] else "Нет", "Сохранён",
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
        self.status.setText("Профиль сохранён локально. Нажми «Применить к ASF», чтобы записать веб-прокси в выбранного бота.")

    def apply_selected(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "ASF", "Сначала выбери профиль прокси в таблице.")
            return
        bot_item = self.table.item(row, 0)
        if bot_item is None:
            return
        bot_name = str(bot_item.data(Qt.ItemDataRole.UserRole) or bot_item.text())
        profile = next((p for p in self.store.list_profiles() if p["bot_name"] == bot_name), None)
        if not profile:
            QMessageBox.warning(self, "ASF", "Профиль прокси не найден.")
            return

        ipc_password, accepted = QInputDialog.getText(
            self, "Авторизация ASF IPC",
            "Введи IPCPassword из локальной конфигурации ASF:",
            QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return

        try:
            proxy_password = self.store.get_password(bot_name)
            proxy = SteamProxy(
                str(profile["scheme"]), str(profile["host"]), int(profile["port"]),
                str(profile["username"]) if profile["username"] else None,
                proxy_password,
            )
            client = ASFClient(ipc_password=ipc_password or None)
            result = client.apply_proxy_settings(bot_name, proxy.asf_bot_settings())
        except (ValueError, RuntimeError, OSError, ASFError, KeyError) as exc:
            QMessageBox.warning(self, "Не удалось применить прокси", str(exc))
            return

        self.status.setText(f"Веб-прокси сохранён в ASF для {bot_name}. Steam CM ещё не подтверждён.")
        answer = QMessageBox.question(
            self,
            "Глобальная настройка ASF",
            "Чтобы прокси использовался и для Steam CM, нужно установить SteamProtocols=4. "
            "Это глобальная настройка и повлияет на ВСЕ боты этого экземпляра ASF; "
            "возможности переподключения могут измениться. Включить её сейчас?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText(
                f"Веб-прокси применён к {bot_name}. Глобальная настройка не менялась; "
                "прокси Steam CM не подтверждён."
            )
            return
        confirm = QMessageBox.warning(
            self,
            "Подтверди изменение для всех ботов",
            "Последнее подтверждение: SteamProtocols=4 изменит транспорт для всех ботов ASF. "
            "Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            self.status.setText(
                f"Веб-прокси применён к {bot_name}. Глобальная настройка не менялась."
            )
            return
        try:
            global_result = client.apply_global_steam_protocols(
                confirmed_instance_wide_change=True
            )
        except (ValueError, RuntimeError, OSError, ASFError) as exc:
            QMessageBox.warning(
                self, "Не удалось изменить глобальную настройку ASF",
                "Веб-прокси бота сохранён, но SteamProtocols=4 не подтверждён.\n" + str(exc),
            )
            self.status.setText(
                f"Веб-прокси применён к {bot_name}; глобальная настройка не подтверждена."
            )
            return
        self.status.setText(
            f"Настройки записаны: веб-прокси для {bot_name}, SteamProtocols=4 глобально. "
            "Фактический маршрут Steam CM ещё требует проверки."
        )
        QMessageBox.information(
            self, "Конфигурация ASF сохранена",
            "Веб-прокси бота и глобальный SteamProtocols=4 сохранены и прочитаны обратно. "
            "Это подтверждает конфигурацию, но ещё не доказывает фактический маршрут трафика Steam CM.",
        )

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
