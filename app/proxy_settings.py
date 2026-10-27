from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QInputDialog, QTabWidget,
)

from integrations.asf.client import ASFClient, ASFError
from integrations.asf.proxy_config import SteamProxy, parse_proxy_list_text
from integrations.asf.proxy_store import ProxyStore


def _database_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    folder = base / "GameDelivery"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "gamedelivery.sqlite3"


class ProxySettingsWidget(QWidget):
    """Manage proxy pool, per-bot assignments, and application through local ASF IPC."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = ProxyStore(_database_path())
        self._build_ui()
        self.refresh()
        self.refresh_pool()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        profiles_page = QWidget()
        root = QVBoxLayout(profiles_page)
        intro = QLabel(
            "Здесь хранятся назначения «бот → прокси». Кнопки применения записывают только "
            "WebProxy/WebProxyUsername/WebProxyPassword через локальный ASF IPC. Пароли прокси "
            "хранятся в системном хранилище. Steam CM этим не подтверждается."
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
        self.save_button = QPushButton("Сохранить профиль")
        self.save_button.clicked.connect(self.save_profile)
        clear = QPushButton("Очистить поля")
        clear.clicked.connect(self.clear_fields)
        self.apply_button = QPushButton("Применить выбранное к ASF")
        self.apply_button.clicked.connect(self.apply_selected)
        self.apply_all_button = QPushButton("Применить все назначения к ASF")
        self.apply_all_button.clicked.connect(self.apply_all_profiles)
        delete = QPushButton("Удалить назначение")
        delete.clicked.connect(self.delete_selected)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.apply_all_button)
        buttons.addWidget(clear)
        buttons.addStretch()
        buttons.addWidget(delete)
        root.addLayout(buttons)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["ASF-бот", "Тип", "Сервер", "Порт", "Авторизация", "Режим"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self.load_selected)
        root.addWidget(self.table, 1)
        self.status = QLabel("Пароли не отображаются в таблице и не сохраняются в обычных настройках.")
        self.status.setWordWrap(True)
        self.status.setObjectName("muted")
        root.addWidget(self.status)
        self.tabs.addTab(profiles_page, "Назначения ASF")

        pool_page = QWidget()
        pool_root = QVBoxLayout(pool_page)
        pool_intro = QLabel(
            "Импорт TXT: одна прокси в строке. Форматы: socks5://host:1080 или "
            "socks5://host:1080;логин;пароль. Пустые строки и строки с # игнорируются. "
            "Пароли хранятся только в системном хранилище."
        )
        pool_intro.setWordWrap(True)
        pool_intro.setObjectName("muted")
        pool_root.addWidget(pool_intro)

        file_row = QHBoxLayout()
        self.proxy_txt_path = QLineEdit()
        self.proxy_txt_path.setReadOnly(True)
        self.proxy_txt_path.setPlaceholderText("TXT со списком прокси")
        choose_file = QPushButton("Выбрать TXT…")
        choose_file.clicked.connect(self.choose_proxy_txt)
        import_file = QPushButton("Импортировать список")
        import_file.clicked.connect(self.import_proxy_txt)
        file_row.addWidget(self.proxy_txt_path, 1)
        file_row.addWidget(choose_file)
        file_row.addWidget(import_file)
        pool_root.addLayout(file_row)

        assignment_row = QHBoxLayout()
        self.pool_bot_name = QLineEdit()
        self.pool_bot_name.setPlaceholderText("Имя бота для ручного назначения")
        self.assign_pool_button = QPushButton("Назначить выбранный прокси боту")
        self.assign_pool_button.clicked.connect(self.assign_selected_pool_proxy)
        self.auto_distribute_button = QPushButton("Автораспределить по всем ботам")
        self.auto_distribute_button.clicked.connect(self.auto_distribute_pool)
        self.delete_pool_button = QPushButton("Удалить прокси из списка")
        self.delete_pool_button.clicked.connect(self.delete_selected_pool_proxy)
        assignment_row.addWidget(self.pool_bot_name, 1)
        assignment_row.addWidget(self.assign_pool_button)
        assignment_row.addWidget(self.auto_distribute_button)
        assignment_row.addWidget(self.delete_pool_button)
        pool_root.addLayout(assignment_row)

        self.pool_table = QTableWidget(0, 5)
        self.pool_table.setHorizontalHeaderLabels(["Прокси ID", "Тип", "Сервер", "Порт", "Авторизация"])
        self.pool_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.pool_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.pool_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.pool_table.horizontalHeader().setStretchLastSection(True)
        self.pool_table.itemSelectionChanged.connect(self.load_selected_pool_proxy)
        pool_root.addWidget(self.pool_table, 1)
        self.pool_status = QLabel("Список прокси пока не загружен.")
        self.pool_status.setWordWrap(True)
        self.pool_status.setObjectName("muted")
        pool_root.addWidget(self.pool_status)
        self.tabs.addTab(pool_page, "Список прокси")

    def choose_proxy_txt(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выбрать список прокси", "", "Text (*.txt);;All files (*)")
        if path:
            self.proxy_txt_path.setText(path)

    def import_proxy_txt(self):
        path_text = self.proxy_txt_path.text().strip()
        if not path_text or not Path(path_text).is_file():
            QMessageBox.warning(self, "Импорт прокси", "Выбери существующий TXT-файл.")
            return
        try:
            proxies = parse_proxy_list_text(Path(path_text).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Импорт прокси не выполнен", str(exc))
            return
        answer = QMessageBox.question(
            self, "Импорт прокси",
            f"Проверено прокси: {len(proxies)}. Они будут добавлены в пул; существующий список не удаляется. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            ids = self.store.add_pool_proxies(proxies)
        except (RuntimeError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "Импорт прокси не выполнен", str(exc))
            return
        self.refresh_pool()
        self.pool_status.setText(f"Добавлено прокси: {len(ids)}. Пароли не выводятся в интерфейс.")

    def refresh_pool(self):
        try:
            proxies = self.store.list_pool()
        except (RuntimeError, OSError) as exc:
            self.pool_status.setText(f"Не удалось загрузить список прокси: {exc}")
            return
        self.pool_table.setRowCount(0)
        for index, proxy in enumerate(proxies, 1):
            row = self.pool_table.rowCount()
            self.pool_table.insertRow(row)
            values = [str(index), str(proxy["scheme"]), str(proxy["host"]), str(proxy["port"]),
                      "Да" if proxy["username"] else "Нет"]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, str(proxy["proxy_id"]))
                self.pool_table.setItem(row, column, item)
        self.pool_status.setText(f"Прокси в пуле: {len(proxies)}. Пароли не отображаются.")

    def load_selected_pool_proxy(self):
        row = self.pool_table.currentRow()
        if row >= 0 and self.pool_table.item(row, 0) is not None:
            self.pool_status.setText(
                f"Выбран прокси №{self.pool_table.item(row, 0).text()}. Укажи имя бота для ручного назначения."
            )

    def _selected_pool_id(self):
        row = self.pool_table.currentRow()
        if row < 0 or self.pool_table.item(row, 0) is None:
            return None
        return str(self.pool_table.item(row, 0).data(Qt.ItemDataRole.UserRole) or "")

    def _pool_proxy_object(self, proxy_id: str) -> SteamProxy:
        profile = next((p for p in self.store.list_pool() if str(p["proxy_id"]) == proxy_id), None)
        if profile is None:
            raise KeyError("Прокси не найден в пуле")
        username = str(profile["username"]) if profile["username"] else None
        password = self.store.get_pool_password(proxy_id) if username else None
        return SteamProxy(str(profile["scheme"]), str(profile["host"]), int(profile["port"]), username, password)

    def assign_selected_pool_proxy(self):
        proxy_id = self._selected_pool_id()
        bot_name = self.pool_bot_name.text().strip()
        if not proxy_id:
            QMessageBox.information(self, "Назначение прокси", "Сначала выбери прокси в списке.")
            return
        if not bot_name:
            QMessageBox.information(self, "Назначение прокси", "Введи имя бота ASF для назначения.")
            return
        existing = next((p for p in self.store.list_profiles() if str(p["bot_name"]).casefold() == bot_name.casefold()), None)
        if existing:
            answer = QMessageBox.question(
                self, "Заменить назначение", f"У бота {bot_name} уже сохранён прокси. Заменить назначение?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            self.store.save(bot_name, self._pool_proxy_object(proxy_id))
        except (ValueError, RuntimeError, OSError, KeyError) as exc:
            QMessageBox.warning(self, "Не удалось назначить прокси", str(exc))
            return
        self.refresh()
        self.status.setText(f"Прокси назначен локально для {bot_name}. Применение к ASF — отдельной кнопкой.")

    def auto_distribute_pool(self):
        pool = self.store.list_pool()
        if not pool:
            QMessageBox.information(self, "Автораспределение", "Сначала импортируй список прокси.")
            return
        ipc_password, accepted = QInputDialog.getText(
            self, "Авторизация ASF IPC", "Введи IPCPassword из локальной конфигурации ASF:", QLineEdit.EchoMode.Password
        )
        if not accepted:
            return
        try:
            bots = ASFClient(ipc_password=ipc_password or None).get_bots()
        except (ASFError, ValueError, OSError) as exc:
            QMessageBox.warning(self, "Автораспределение", f"Не удалось получить список ASF: {exc}")
            return
        bot_names = sorted((str(name) for name in bots), key=str.casefold)
        if not bot_names:
            QMessageBox.information(self, "Автораспределение", "ASF не вернул список ботов.")
            return
        existing = {str(p["bot_name"]).casefold() for p in self.store.list_profiles()}
        overwrite_count = sum(name.casefold() in existing for name in bot_names)
        answer = QMessageBox.question(
            self, "Автораспределение прокси",
            f"Распределить прокси по кругу между {len(bot_names)} ботами? "
            f"Существующих назначений, которые будут перезаписаны: {overwrite_count}. "
            "Пока сохраняются только локальные назначения; для применения к ASF нажми отдельную кнопку.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        saved, failed = 0, []
        for index, bot_name in enumerate(bot_names):
            proxy = pool[index % len(pool)]
            try:
                self.store.save(bot_name, self._pool_proxy_object(str(proxy["proxy_id"])))
                saved += 1
            except (ValueError, RuntimeError, OSError, KeyError):
                failed.append(bot_name)
        self.refresh()
        if failed:
            self.pool_status.setText(
                f"Локально назначено: {saved} из {len(bot_names)}. Ошибки: "
                + ", ".join(failed[:8]) + ("…" if len(failed) > 8 else "")
            )
        else:
            self.pool_status.setText(f"Автораспределено по кругу на {saved} ботов. Теперь примени назначения к ASF.")

    def delete_selected_pool_proxy(self):
        proxy_id = self._selected_pool_id()
        if not proxy_id:
            QMessageBox.information(self, "Список прокси", "Выбери прокси в таблице.")
            return
        answer = QMessageBox.question(
            self, "Удаление прокси",
            "Удалить выбранный прокси из пула? Уже сохранённые назначения ботов останутся.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.delete_pool_proxy(proxy_id)
        except (RuntimeError, OSError, KeyError) as exc:
            QMessageBox.warning(self, "Удаление прокси", str(exc))
            return
        self.refresh_pool()
        self.pool_status.setText("Прокси удалён из пула. Назначения ботов не изменились.")

    def refresh(self):
        self.table.setRowCount(0)
        for profile in self.store.list_profiles():
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [profile["bot_name"], profile["scheme"], profile["host"], str(profile["port"]),
                      "Да" if profile["username"] else "Нет", "Сохранён"]
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
            self.store.save(bot_name, SteamProxy(self.scheme.currentText(), self.host.text(), self.port.value(), username, password))
        except (ValueError, RuntimeError, OSError) as exc:
            QMessageBox.warning(self, "Не удалось сохранить", str(exc))
            return
        self.password.clear()
        self.refresh()
        self.status.setText("Профиль сохранён локально. Применение к ASF — отдельной кнопкой.")

    def _ask_ipc_password(self):
        password, accepted = QInputDialog.getText(
            self, "Авторизация ASF IPC", "Введи IPCPassword из локальной конфигурации ASF:",
            QLineEdit.EchoMode.Password,
        )
        return (password or None, True) if accepted else (None, False)

    def apply_all_profiles(self):
        profiles = self.store.list_profiles()
        if not profiles:
            QMessageBox.information(self, "ASF", "Нет сохранённых назначений прокси.")
            return
        ipc_password, accepted = self._ask_ipc_password()
        if not accepted:
            return
        answer = QMessageBox.question(
            self, "Применить все назначения",
            f"Записать веб-прокси в ASF для {len(profiles)} ботов? Будут изменены только "
            "WebProxy, WebProxyUsername и WebProxyPassword. Глобальный SteamProtocols не меняется.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        client, applied, failed = ASFClient(ipc_password=ipc_password), [], []
        for profile in profiles:
            bot_name = str(profile["bot_name"])
            try:
                username = str(profile["username"]) if profile["username"] else None
                password = self.store.get_password(bot_name) if username else None
                proxy = SteamProxy(str(profile["scheme"]), str(profile["host"]), int(profile["port"]), username, password)
                client.apply_proxy_settings(bot_name, proxy.asf_bot_settings())
                applied.append(bot_name)
            except (ValueError, RuntimeError, OSError, ASFError, KeyError):
                failed.append(bot_name)
        if failed:
            self.status.setText(
                f"Применено к ASF: {len(applied)} из {len(profiles)}. Ошибки: "
                + ", ".join(failed[:8]) + ("…" if len(failed) > 8 else "")
            )
        else:
            self.status.setText(
                f"Веб-прокси применены и проверены ASF для {len(applied)} ботов. "
                "Steam CM и фактическая маршрутизация отдельно не подтверждены."
            )

    def apply_selected(self):
        row = self.table.currentRow()
        if row < 0 or self.table.item(row, 0) is None:
            QMessageBox.information(self, "ASF", "Сначала выбери профиль прокси в таблице.")
            return
        bot_name = str(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) or self.table.item(row, 0).text())
        profile = next((p for p in self.store.list_profiles() if p["bot_name"] == bot_name), None)
        if not profile:
            QMessageBox.warning(self, "ASF", "Профиль прокси не найден.")
            return
        ipc_password, accepted = self._ask_ipc_password()
        if not accepted:
            return
        try:
            username = str(profile["username"]) if profile["username"] else None
            password = self.store.get_password(bot_name) if username else None
            proxy = SteamProxy(str(profile["scheme"]), str(profile["host"]), int(profile["port"]), username, password)
            client = ASFClient(ipc_password=ipc_password)
            client.apply_proxy_settings(bot_name, proxy.asf_bot_settings())
        except (ValueError, RuntimeError, OSError, ASFError, KeyError) as exc:
            QMessageBox.warning(self, "Не удалось применить прокси", str(exc))
            return
        self.status.setText(f"Веб-прокси сохранён и проверен ASF для {bot_name}. Steam CM отдельно не подтверждён.")

    def load_selected(self):
        row = self.table.currentRow()
        if row < 0 or self.table.item(row, 0) is None:
            return
        profile_name = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) or self.table.item(row, 0).text()
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
        if row < 0 or self.table.item(row, 0) is None:
            QMessageBox.information(self, "Прокси", "Сначала выбери назначение в таблице.")
            return
        bot_name = str(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) or self.table.item(row, 0).text())
        answer = QMessageBox.question(
            self, "Удалить назначение", f"Удалить локальное назначение для {bot_name}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
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
        self.status.setText("Назначение удалено локально. Настройки работающего ASF не изменялись.")

    def clear_fields(self):
        self.bot_name.clear()
        self.scheme.setCurrentText("http")
        self.host.clear()
        self.port.setValue(1080)
        self.username.clear()
        self.password.clear()
        self.table.clearSelection()
