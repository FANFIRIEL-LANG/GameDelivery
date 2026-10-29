from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import (
    QFileDialog, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QInputDialog, QTabWidget,
)

from integrations.asf.client import ASFClient, ASFError
from integrations.asf.proxy_config import SteamProxy, parse_proxy_list_text
from integrations.asf.proxy_store import ProxyStore
from app.bot_selector import populate_bot_combo


def _database_path() -> Path:
    """Use the same per-user database file as orders, inventory, and reservations."""
    folder = Path.home() / ".gamedelivery"
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
        self.bot_name = QComboBox()
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
        form.addRow("Бот ASF", self.bot_name)
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
        self.enable_cm_button = QPushButton("Включить прокси Steam CM…")
        self.enable_cm_button.clicked.connect(self.enable_steam_cm_proxy)
        delete = QPushButton("Удалить назначение")
        delete.clicked.connect(self.delete_selected)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(clear)
        buttons.addStretch()
        root.addLayout(buttons)

        bulk_buttons = QHBoxLayout()
        bulk_buttons.addWidget(self.apply_all_button)
        bulk_buttons.addWidget(self.enable_cm_button)
        bulk_buttons.addStretch()
        bulk_buttons.addWidget(delete)
        root.addLayout(bulk_buttons)

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
            "Импорт TXT: одна прокси в строке. Форматы: socks5://host:1080; URI;логин;пароль "
            "(разделители ; | , или TAB); host:port или host:port:логин:пароль. "
            "Без схемы используется HTTP; для SOCKS указывай схему явно. IPv6 пиши как [2001:db8::1]:1080. "
            "Пустые строки и строки с # игнорируются. Пароли хранятся только в системном хранилище."
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
        self.pool_bot_name = QComboBox()
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
        self.pool_table.setHorizontalHeaderLabels(["№", "Тип", "Сервер", "Порт", "Авторизация"])
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
        names = [str(p["bot_name"]) for p in self.store.list_profiles()]
        populate_bot_combo(self.pool_bot_name, names)

    def load_selected_pool_proxy(self):
        row = self.pool_table.currentRow()
        if row >= 0 and self.pool_table.item(row, 0) is not None:
            self.pool_status.setText(
                f"Выбран прокси №{self.pool_table.item(row, 0).text()}. Выбери бота из списка для назначения."
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
        bot_name = str(self.pool_bot_name.currentData() or "").strip()
        if not proxy_id:
            QMessageBox.information(self, "Назначение прокси", "Сначала выбери прокси в списке.")
            return
        if not bot_name:
            QMessageBox.information(self, "Назначение прокси", "Выбери бота ASF из списка.")
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

    def _asf_config_directory(self) -> Path:
        settings = QSettings("FANFIRIEL-LANG", "GameDelivery")
        configured = str(settings.value("asf/config_path", "") or "").strip()
        if not configured:
            raise RuntimeError("Сначала укажи папку config ASF во вкладке «ASF-боты».")
        folder = Path(configured).expanduser()
        if not folder.is_dir():
            raise RuntimeError("Указанная папка config ASF не существует.")
        return folder

    @staticmethod
    def _write_proxy_to_bot_config(config_path: Path, proxy: SteamProxy) -> None:
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError(f"Не удалось прочитать конфигурацию {config_path.name}.") from None
        if not isinstance(payload, dict):
            raise RuntimeError(f"Конфигурация {config_path.name} имеет неверный формат.")
        backup_path = config_path.with_suffix(config_path.suffix + ".gamedelivery.bak")
        if not backup_path.exists():
            shutil.copy2(config_path, backup_path)
        payload.update(proxy.asf_bot_settings())
        temporary_path = config_path.with_suffix(config_path.suffix + ".gamedelivery.tmp")
        try:
            temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary_path, config_path)
            written = json.loads(config_path.read_text(encoding="utf-8-sig"))
            expected = proxy.asf_bot_settings()
            if any(written.get(key) != value for key, value in expected.items()):
                raise RuntimeError(
                    f"ASF не сохранил все поля прокси в {config_path.name}; бот не должен запускаться."
                )
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def auto_distribute_pool(self):
        pool = self.store.list_pool()
        if not pool:
            QMessageBox.information(self, "Автораспределение", "Сначала импортируй список прокси.")
            return
        try:
            config_dir = self._asf_config_directory()
        except (RuntimeError, OSError) as exc:
            QMessageBox.warning(self, "Автораспределение", str(exc))
            return
        ignored = {"asf", "global", "example", "examplebot"}
        bot_files = sorted(
            (path for path in config_dir.glob("*.json") if path.stem.casefold() not in ignored),
            key=lambda path: path.stem.casefold(),
        )
        if not bot_files:
            QMessageBox.information(
                self, "Автораспределение",
                "В папке config ASF не найдены конфигурации ботов. Ничего не изменено."
            )
            return
        existing = {str(p["bot_name"]).casefold() for p in self.store.list_profiles()}
        overwrite_count = sum(path.stem.casefold() in existing for path in bot_files)
        answer = QMessageBox.question(
            self, "Автораспределение прокси",
            f"Распределить {len(pool)} прокси по кругу между {len(bot_files)} конфигурациями ASF, "
            f"включая выключенные боты? Существующих назначений: {overwrite_count}. "
            "Прокси будут записаны в JSON-конфигурации до запуска ботов. Для каждого файла "
            "создаётся резервная копия .gamedelivery.bak. Сами боты запускаться не будут.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        applied, failed = [], []
        for index, config_path in enumerate(bot_files):
            proxy_record = pool[index % len(pool)]
            bot_name = config_path.stem
            try:
                proxy = self._pool_proxy_object(str(proxy_record["proxy_id"]))
                self.store.save(bot_name, proxy)
                self._write_proxy_to_bot_config(config_path, proxy)
                applied.append(bot_name)
            except (ValueError, RuntimeError, OSError, KeyError):
                failed.append(bot_name)
        self.refresh()
        self.refresh_pool()
        if failed:
            self.pool_status.setText(
                f"Прокси применены до запуска к {len(applied)} из {len(bot_files)} ботов. "
                "Ботов не запускай, пока не исправишь ошибки для: "
                + ", ".join(failed[:8]) + ("…" if len(failed) > 8 else "")
            )
            QMessageBox.warning(
                self, "Автораспределение выполнено не полностью",
                self.pool_status.text()
            )
        else:
            self.pool_status.setText(
                f"Готово: прокси записаны в конфигурации {len(applied)} ботов до запуска. "
                "Боты не запускались. Резервные копии созданы."
            )

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
        profiles = self.store.list_profiles()
        populate_bot_combo(self.bot_name, [str(p["bot_name"]) for p in profiles])
        populate_bot_combo(self.pool_bot_name, [str(p["bot_name"]) for p in profiles])
        for profile in profiles:
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
        bot_name = str(self.bot_name.currentData() or "").strip()
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

    def _saved_ipc_password(self) -> str | None:
        # Reuse the same Windows credential entry as the ASF management tab.
        try:
            import keyring
            backend = keyring.get_keyring()
            if getattr(backend, "priority", 0) >= 1:
                saved = keyring.get_password("GameDelivery.ASFIPC", "localhost:1242")
                if saved:
                    return saved
        except Exception:
            pass

        # ASF installations commonly keep IPCPassword in their local ASF.json.
        settings = QSettings("FANFIRIEL-LANG", "GameDelivery")
        config_text = str(settings.value("asf/config_path", "") or "").strip()
        if config_text:
            config_file = Path(config_text) / "ASF.json"
            try:
                import json
                payload = json.loads(config_file.read_text(encoding="utf-8-sig"))
                if isinstance(payload, dict):
                    for key, value in payload.items():
                        if str(key).casefold() == "ipcpassword" and isinstance(value, str) and value:
                            return value
            except (OSError, UnicodeDecodeError, ValueError):
                pass
        return None

    def _ask_ipc_password(self):
        saved = self._saved_ipc_password()
        if saved:
            return saved, True
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

    def enable_steam_cm_proxy(self):
        ipc_password, accepted = self._ask_ipc_password()
        if not accepted:
            return
        self._confirm_steam_cm_proxy(ASFClient(ipc_password=ipc_password))

    def _confirm_steam_cm_proxy(self, client: ASFClient, bot_name: str | None = None):
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
            if bot_name:
                self.status.setText(
                    f"Веб-прокси применён к {bot_name}. Глобальная настройка не менялась; "
                    "прокси Steam CM не подтверждён."
                )
            else:
                self.status.setText("SteamProtocols не менялся. Прокси Steam CM не включён.")
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
            self.status.setText("Глобальная настройка ASF не менялась.")
            return
        try:
            result = client.apply_global_steam_protocols(confirmed_instance_wide_change=True)
        except (ValueError, RuntimeError, OSError, ASFError) as exc:
            QMessageBox.warning(
                self, "Не удалось изменить глобальную настройку ASF",
                "Веб-прокси бота сохранён, но SteamProtocols=4 не подтверждён.\n" + str(exc),
            )
            self.status.setText("Глобальная настройка SteamProtocols=4 не подтверждена.")
            return
        self.status.setText(
            "SteamProtocols=4 сохранён и прочитан обратно. Это глобальная настройка для всех ботов; "
            "фактический маршрут Steam CM ещё нужно проверить."
        )
        QMessageBox.information(
            self, "Конфигурация ASF сохранена",
            "Глобальный SteamProtocols=4 сохранён и прочитан обратно. "
            "Это подтверждает конфигурацию, но не доказывает фактический маршрут трафика Steam CM.",
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
        self.status.setText(f"Веб-прокси сохранён и проверен ASF для {bot_name}.")
        self._confirm_steam_cm_proxy(client, bot_name)

    def load_selected(self):
        row = self.table.currentRow()
        if row < 0 or self.table.item(row, 0) is None:
            return
        profile_name = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) or self.table.item(row, 0).text()
        profile = next((p for p in self.store.list_profiles() if p["bot_name"] == profile_name), None)
        if not profile:
            return
        index = self.bot_name.findData(str(profile["bot_name"]))
        if index >= 0:
            self.bot_name.setCurrentIndex(index)
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
        self.bot_name.setCurrentIndex(0)
        self.scheme.setCurrentText("http")
        self.host.clear()
        self.port.setValue(1080)
        self.username.clear()
        self.password.clear()
        self.table.clearSelection()
