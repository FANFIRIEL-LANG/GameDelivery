from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.asf_inventory_controller import ASFInventoryController
from integrations.asf.client import ASFClient, ASFError


class ASFManagementWidget(QWidget):
    """Manage ASF bots and import account lists from a local TXT file."""

    inventory_snapshot_updated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = QSettings("FANFIRIEL-LANG", "GameDelivery")
        self._asf_process = None
        self._asf_log_handle = None
        self._asf_ipc_attempts = 0
        self._pending_start_bot_names: list[str] = []
        root = QVBoxLayout(self)
        intro = QLabel(
            "Массовый импорт аккаунтов: TXT с логином и паролем + папка maFiles + "
            "папка config ASF. Сначала создаются отключённые конфигурации для проверки; "
            "после перезапуска ASF проверь список ботов. Секреты не выводятся в журнал."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        form = QFormLayout()
        self.ipc_password = QLineEdit()
        self.ipc_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.ipc_password.setPlaceholderText("IPCPassword из локальной конфигурации ASF")
        form.addRow("Пароль ASF IPC", self.ipc_password)
        root.addLayout(form)

        import_row = QHBoxLayout()
        self.txt_path = QLineEdit()
        self.txt_path.setReadOnly(True)
        self.txt_path.setPlaceholderText("TXT: login:password, login;password, login|password или TAB")
        self.txt_button = QPushButton("Выбрать TXT…")
        self.txt_button.clicked.connect(self.choose_txt)
        import_row.addWidget(self.txt_path, 1)
        import_row.addWidget(self.txt_button)
        root.addLayout(import_row)

        mafile_row = QHBoxLayout()
        self.mafiles_path = QLineEdit()
        self.mafiles_path.setReadOnly(True)
        self.mafiles_path.setPlaceholderText("Папка со всеми maFiles")
        self.mafiles_button = QPushButton("Папка maFiles…")
        self.mafiles_button.clicked.connect(self.choose_mafiles)
        mafile_row.addWidget(self.mafiles_path, 1)
        mafile_row.addWidget(self.mafiles_button)
        root.addLayout(mafile_row)

        config_row = QHBoxLayout()
        self.config_path = QLineEdit()
        self.config_path.setReadOnly(True)
        self.config_path.setPlaceholderText("Папка config установленного ASF")
        self.config_path.setText(str(self._settings.value("asf/config_path", "")))
        self.config_button = QPushButton("Папка config ASF…")
        self.config_button.clicked.connect(self.choose_config)
        config_row.addWidget(self.config_path, 1)
        config_row.addWidget(self.config_button)
        root.addLayout(config_row)

        asf_process_row = QHBoxLayout()
        self.asf_executable_path = QLineEdit()
        self.asf_executable_path.setReadOnly(True)
        self.asf_executable_path.setPlaceholderText("ArchiSteamFarm.exe — путь сохраняется в GameDelivery")
        self.asf_executable_path.setText(str(self._settings.value("asf/executable_path", "")))
        self.asf_executable_button = QPushButton("Файл ASF.exe…")
        self.start_asf_button = QPushButton("Запустить ASF")
        self.stop_asf_button = QPushButton("Остановить ASF")
        self.auto_start_asf_checkbox = QCheckBox("Запускать ASF вместе с GameDelivery")
        self.auto_start_asf_checkbox.setChecked(bool(self._settings.value("asf/auto_start", True, type=bool)))
        asf_process_row.addWidget(self.asf_executable_path, 1)
        asf_process_row.addWidget(self.asf_executable_button)
        asf_process_row.addWidget(self.start_asf_button)
        asf_process_row.addWidget(self.stop_asf_button)
        root.addLayout(asf_process_row)
        root.addWidget(self.auto_start_asf_checkbox)

        buttons = QHBoxLayout()
        self.import_button = QPushButton("Проверить TXT и импортировать аккаунты")
        self.refresh_button = QPushButton("Обновить список ботов")
        self.restart_button = QPushButton("Перезапустить ASF…")
        self.inventory_button = QPushButton("Показать инвентарь выбранного бота")
        self.export_inventory_button = QPushButton("Экспорт инвентаря JSON")
        self.snapshot_button = QPushButton("Обновить снимок в GameDelivery")
        for button in (
            self.import_button, self.refresh_button, self.restart_button,
            self.inventory_button, self.export_inventory_button, self.snapshot_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch()
        root.addLayout(buttons)

        launch_buttons = QHBoxLayout()
        self.start_selected_button = QPushButton("Запустить выбранных")
        self.start_all_button = QPushButton("Запустить всех")
        self.select_all_button = QPushButton("Выбрать всех")
        launch_buttons.addWidget(self.start_selected_button)
        launch_buttons.addWidget(self.start_all_button)
        launch_buttons.addWidget(self.select_all_button)
        self.enable_selected_button = QPushButton("Включить выбранных")
        self.enable_all_button = QPushButton("Включить всех в config ASF")
        launch_buttons.addWidget(self.enable_selected_button)
        launch_buttons.addWidget(self.enable_all_button)
        launch_buttons.addStretch()
        root.addLayout(launch_buttons)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Имя бота", "Состояние"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table, 1)
        self.status = QLabel("ASF не запрашивался.")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._last_inventory = None
        self._last_inventory_bot = None
        self._last_inventory_export_payload = None
        self.refresh_button.clicked.connect(self.refresh_bots)
        self.asf_executable_button.clicked.connect(self.choose_asf_executable)
        self.start_asf_button.clicked.connect(self.start_asf)
        self.stop_asf_button.clicked.connect(self.stop_asf)
        self.auto_start_asf_checkbox.toggled.connect(lambda enabled: self._settings.setValue("asf/auto_start", enabled))
        self.import_button.clicked.connect(self.import_accounts)
        self.restart_button.clicked.connect(self.restart_asf)
        self.inventory_button.clicked.connect(self.show_inventory)
        self.export_inventory_button.clicked.connect(self.export_inventory)
        self.snapshot_button.clicked.connect(self.refresh_inventory_snapshot)
        self.start_selected_button.clicked.connect(self.start_selected_bots)
        self.start_all_button.clicked.connect(self.start_all_bots)
        self.select_all_button.clicked.connect(self.table.selectAll)
        self.enable_selected_button.clicked.connect(self.enable_selected_bots)
        self.enable_all_button.clicked.connect(self.enable_all_configured_bots)
        self.export_inventory_button.setEnabled(False)
        if not self.asf_executable_path.text().strip() and self.config_path.text().strip():
            self._autodetect_asf_executable(Path(self.config_path.text().strip()))
        if self.auto_start_asf_checkbox.isChecked() and self.asf_executable_path.text().strip():
            QTimer.singleShot(700, self.start_asf)
        elif self.config_path.text().strip():
            QTimer.singleShot(250, self.refresh_bots)

    def choose_txt(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выбрать список аккаунтов", "", "Text (*.txt);;All files (*)")
        if path:
            self.txt_path.setText(path)

    def choose_mafiles(self):
        path = QFileDialog.getExistingDirectory(self, "Выбрать папку maFiles")
        if path:
            self.mafiles_path.setText(path)

    def choose_config(self):
        path = QFileDialog.getExistingDirectory(self, "Выбрать папку config ASF")
        if path:
            self.config_path.setText(path)
            self._settings.setValue("asf/config_path", path)
            self._autodetect_asf_executable(Path(path))
            self.refresh_bots()

    def choose_asf_executable(self):
        start_dir = str(Path(self.config_path.text()).parent) if self.config_path.text().strip() else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбрать исполняемый файл ASF", start_dir,
            "ASF executable (*.exe);;All files (*)",
        )
        if path:
            self.asf_executable_path.setText(path)
            self._settings.setValue("asf/executable_path", path)

    def _autodetect_asf_executable(self, config_dir: Path):
        current = self.asf_executable_path.text().strip()
        if current and Path(current).is_file():
            return
        for name in ("ArchiSteamFarm.exe", "ASF.exe"):
            candidate = config_dir.parent / name
            if candidate.is_file():
                self.asf_executable_path.setText(str(candidate))
                self._settings.setValue("asf/executable_path", str(candidate))
                return

    @staticmethod
    def _ipc_port_is_open() -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 1242), timeout=0.35):
                return True
        except OSError:
            return False

    def start_asf(self):
        try:
            self._client().get_status()
        except (ASFError, ValueError, OSError):
            if self._ipc_port_is_open():
                self._pending_start_bot_names.clear()
                self.status.setText(
                    "На порту ASF IPC уже есть процесс, но запрос не прошёл. "
                    "Проверь IPCPassword и настройки ASF; второй экземпляр не запускаю."
                )
                return
        else:
            self.status.setText("ASF уже запущен; подключение IPC подтверждено.")
            if self._pending_start_bot_names:
                pending = self._pending_start_bot_names[:]
                self._pending_start_bot_names.clear()
                self._start_bots(pending)
            else:
                self.refresh_bots()
            return

        executable = Path(self.asf_executable_path.text().strip()) if self.asf_executable_path.text().strip() else None
        if executable is None or not executable.is_file():
            config_dir = Path(self.config_path.text().strip()) if self.config_path.text().strip() else None
            if config_dir and config_dir.is_dir():
                self._autodetect_asf_executable(config_dir)
                candidate = self.asf_executable_path.text().strip()
                executable = Path(candidate) if candidate else None
        if executable is None or not executable.is_file():
            self.status.setText(
                "Не найден ArchiSteamFarm.exe. Выбери файл ASF один раз кнопкой «Файл ASF.exe…»."
            )
            return
        if self._asf_process is not None and self._asf_process.poll() is None:
            self._asf_ipc_attempts = 0
            self.status.setText("ASF уже запущен GameDelivery; ожидаю готовности IPC…")
            QTimer.singleShot(500, self._poll_asf_ipc)
            return
        try:
            log_dir = Path.home() / ".gamedelivery"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_handle = (log_dir / "asf-supervisor.log").open("ab")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                process = subprocess.Popen(
                    [str(executable), "--process-required"],
                    cwd=str(executable.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                    close_fds=True,
                )
            except Exception:
                log_handle.close()
                raise
        except (OSError, ValueError) as exc:
            self.status.setText(f"Не удалось запустить ASF: {exc}")
            return
        self._asf_process = process
        self._asf_log_handle = log_handle
        self._settings.setValue("asf/executable_path", str(executable))
        self._asf_ipc_attempts = 0
        self.status.setText("GameDelivery запустил ASF и ожидает готовности IPC…")
        QTimer.singleShot(500, self._poll_asf_ipc)

    def _poll_asf_ipc(self):
        try:
            self._client().get_status()
        except (ASFError, ValueError, OSError):
            self._asf_ipc_attempts += 1
            if self._asf_ipc_attempts < 30:
                QTimer.singleShot(500, self._poll_asf_ipc)
                return
            self.status.setText(
                "ASF запущен, но IPC не стал доступен за 15 секунд. "
                "Проверь, что IPC включён в ASF.json и IPCPassword указан верно."
            )
            return
        self._asf_ipc_attempts = 0
        self.status.setText("ASF готов: локальный IPC доступен.")
        if self._pending_start_bot_names:
            pending = self._pending_start_bot_names[:]
            self._pending_start_bot_names.clear()
            self._start_bots(pending)
        else:
            self.refresh_bots()

    def stop_asf(self):
        answer = QMessageBox.question(
            self, "Остановить ASF",
            "Остановить весь экземпляр ASF? Все работающие боты будут отключены.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText("Остановка ASF отменена.")
            return
        try:
            self._client().exit()
            self._asf_ipc_attempts = 0
            self.status.setText("Команда остановки отправлена ASF.")
            QTimer.singleShot(1200, self._verify_asf_stopped)
            return
        except (ASFError, ValueError, OSError) as exc:
            if self._asf_process is not None and self._asf_process.poll() is None:
                try:
                    self._asf_process.terminate()
                    self._asf_process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    self._asf_process.kill()
                    self._asf_process.wait(timeout=2)
                except OSError as terminate_error:
                    self.status.setText(f"Не удалось остановить ASF: {terminate_error}")
                    return
                self._close_asf_log()
                message = "Процесс ASF, запущенный GameDelivery, остановлен."
            else:
                message = (
                    f"Не удалось остановить ASF через IPC ({exc}). "
                    "Внешний процесс не завершаю принудительно."
                )
            self.refresh_bots()
            self.status.setText(message)

    def _verify_asf_stopped(self):
        if not self._ipc_port_is_open():
            self._close_asf_log()
            self.refresh_bots()
            self.status.setText("ASF остановлен; порт IPC больше не отвечает.")
            return
        self._asf_ipc_attempts += 1
        if self._asf_ipc_attempts < 10:
            QTimer.singleShot(500, self._verify_asf_stopped)
        else:
            self.status.setText("Команда остановки отправлена, но IPC всё ещё отвечает. Проверь состояние ASF.")

    def _close_asf_log(self):
        if self._asf_log_handle is not None:
            try:
                self._asf_log_handle.close()
            except OSError:
                pass
            self._asf_log_handle = None

    @staticmethod
    def _safe_bot_name(login: str) -> str:
        name = login.strip()
        if (
            not name or name.casefold() == "asf" or name.startswith(".")
            or any(ch in name for ch in '/\\\\<>:"|?*')
            or any(ord(ch) < 32 for ch in name) or name.endswith(".")
        ):
            raise ValueError(f"Логин нельзя безопасно использовать как имя бота ASF: {login!r}")
        return name

    def import_accounts(self):
        txt = Path(self.txt_path.text().strip()) if self.txt_path.text().strip() else None
        ma_dir = Path(self.mafiles_path.text().strip()) if self.mafiles_path.text().strip() else None
        config_dir = Path(self.config_path.text().strip()) if self.config_path.text().strip() else None
        if not txt or not txt.is_file():
            QMessageBox.warning(self, "Импорт аккаунтов", "Выбери существующий TXT-файл.")
            return
        if not ma_dir or not ma_dir.is_dir():
            QMessageBox.warning(self, "Импорт аккаунтов", "Выбери существующую папку maFiles.")
            return
        if not config_dir or not config_dir.is_dir():
            QMessageBox.warning(self, "Импорт аккаунтов", "Выбери существующую папку config установленного ASF.")
            return

        try:
            lines = txt.read_text(encoding="utf-8-sig").splitlines()
            accounts = []
            seen = set()
            for line_no, raw in enumerate(lines, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                separator = next((sep for sep in (";", "|", "\t", ":") if sep in line), None)
                if separator is None:
                    raise ValueError(
                        f"Строка {line_no}: используй login:password, login;password, login|password или TAB"
                    )
                login, password = (part.strip() for part in line.split(separator, 1))
                if not login or not password:
                    raise ValueError(f"Строка {line_no}: логин и пароль обязательны")
                bot_name = self._safe_bot_name(login)
                key = bot_name.casefold()
                if key in seen:
                    raise ValueError(f"Строка {line_no}: повторяется логин {login}")
                seen.add(key)
                candidates = [ma_dir / f"{login}.maFile", ma_dir / f"{bot_name}.maFile"]
                ma_path = next((p for p in candidates if p.is_file()), None)
                if ma_path is None:
                    raise ValueError(
                        f"Не найден maFile для {login}. Ожидается файл {login}.maFile в выбранной папке."
                    )
                try:
                    ma_data = json.loads(ma_path.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    raise ValueError(f"maFile для {login} не читается как JSON.") from None
                if not isinstance(ma_data, dict):
                    raise ValueError(f"maFile для {login} имеет неверный формат.")
                target_json = config_dir / f"{bot_name}.json"
                target_ma = config_dir / f"{bot_name}.maFile"
                if target_json.exists() or target_ma.exists():
                    raise ValueError(
                        f"Для {bot_name} уже есть JSON или maFile в config ASF. "
                        "Импорт остановлен без перезаписи."
                    )
                accounts.append((bot_name, login, password, ma_path, target_json, target_ma, ma_data))

            if not accounts:
                raise ValueError("В TXT нет аккаунтов для импорта.")
            answer = QMessageBox.question(
                self, "Подтвердить импорт",
                f"Найдено аккаунтов: {len(accounts)}. Будут созданы отключённые конфигурации "
                "Enabled=false и скопированы соответствующие maFiles в config ASF. "
                "Пароли будут записаны в JSON конфигурации ASF. Исходные TXT и maFiles не удаляются. "
                "Продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

            created = []
            try:
                for bot_name, login, password, ma_path, target_json, target_ma, ma_data in accounts:
                    payload = {"Enabled": False, "SteamLogin": login, "SteamPassword": password}
                    target_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    try:
                        shutil.copy2(ma_path, target_ma)
                    except Exception:
                        target_json.unlink(missing_ok=True)
                        raise
                    created.extend([target_json, target_ma])
            except Exception:
                for path in reversed(created):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise

            self.status.setText(
                f"Подготовлено аккаунтов: {len(accounts)}. Конфигурации созданы с Enabled=false. "
                "Нажми «Перезапустить ASF…», затем «Обновить список ботов» для проверки загрузки."
            )
            QMessageBox.information(
                self, "Импорт подготовлен",
                f"Подготовлено аккаунтов: {len(accounts)}.\n\n"
                "Конфигурации отключены (Enabled=false), чтобы сначала проверить загрузку. "
                "TXT и исходные maFiles не изменены. Перезапусти ASF через кнопку и обнови список ботов."
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Импорт не выполнен", str(exc))

    def _client(self) -> ASFClient:
        return ASFClient(ipc_password=self.ipc_password.text() or None)

    def _configured_bots_from_disk(self) -> dict[str, str]:
        """Read bot names and Enabled flags from config JSON without exposing secrets."""
        config_text = self.config_path.text().strip()
        config_dir = Path(config_text) if config_text else None
        if config_dir is None or not config_dir.is_dir():
            return {}
        configured = {}
        for path in config_dir.glob("*.json"):
            if path.name.casefold() == "asf.json":
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            enabled = payload.get("Enabled", True)
            state = "Отключён (config)" if enabled is False else "В конфигурации ASF"
            configured[path.stem] = state
        return configured

    def refresh_bots(self):
        self.refresh_button.setEnabled(False)
        configured = self._configured_bots_from_disk()
        ipc_error = None
        try:
            bots = self._client().get_bots()
        except (ASFError, ValueError, OSError) as exc:
            bots = {}
            ipc_error = exc
        try:
            merged = {
                name: {"_local_state": state}
                for name, state in configured.items()
            }
            for name, details in bots.items():
                canonical = next(
                    (known for known in merged if known.casefold() == str(name).casefold()),
                    str(name),
                )
                merged[canonical] = details
            if merged:
                self._show_bot_rows(merged)
            if ipc_error:
                if configured:
                    self.status.setText(
                        f"IPC ASF недоступен или не вернул список ({ipc_error}). "
                        f"Показаны конфигурации из папки config: {len(configured)}; статусы активности неизвестны."
                    )
                else:
                    self.table.setRowCount(0)
                    self._last_inventory = None
                    self._last_inventory_bot = None
                    self._last_inventory_export_payload = None
                    self.export_inventory_button.setEnabled(False)
                    self.status.setText(f"Не удалось получить список ASF: {ipc_error}. Список очищен.")
            elif bots:
                self.status.setText(
                    f"Получено ботов: {len(bots)} из ASF; всего конфигураций: {len(merged)}. "
                    "Секреты в таблицу не выводятся."
                )
            elif configured:
                self.status.setText(
                    f"Активных ботов в ASF: 0. Найдено конфигураций: {len(configured)}. "
                    "Показаны из папки config; для применения изменений перезапусти ASF."
                )
            else:
                self.table.setRowCount(0)
                self.status.setText("ASF не вернул ботов, а конфигурации ботов в выбранной папке не найдены.")
        finally:
            self.refresh_button.setEnabled(True)

    def _show_bot_rows(self, bots):
        self.table.setRowCount(0)
        for name, details in sorted(bots.items(), key=lambda pair: pair[0].casefold()):
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(name)))
            if isinstance(details, dict) and "_local_state" in details:
                state = details["_local_state"]
            elif isinstance(details, bool):
                state = "Запущен" if details else "Отключён"
            elif isinstance(details, dict):
                connected = details.get("IsConnected")
                if isinstance(connected, bool):
                    state = "Подключён" if connected else "Не подключён"
                else:
                    running = details.get("KeepRunning", details.get("IsRunning"))
                    state = ("Запущен" if running else "Отключён") if isinstance(running, bool) else "Загружен"
            else:
                state = "Загружен"
            self.table.setItem(row, 1, QTableWidgetItem(state))

    def _selected_bot_names(self) -> list[str]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        names = []
        for row in rows:
            item = self.table.item(row, 0)
            if item is not None and item.text().strip():
                names.append(item.text().strip())
        return names

    def start_selected_bots(self):
        bot_names = self._selected_bot_names()
        if not bot_names:
            self.status.setText("Выбери одного или нескольких ботов в таблице.")
            return
        answer = QMessageBox.question(
            self, "Запуск выбранных ботов",
            f"Отправить ASF команду запуска для ботов: {len(bot_names)}? "
            "Для входа могут потребоваться Steam Guard и подтверждения.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText("Запуск выбранных ботов отменён.")
            return
        self._start_or_launch_asf(bot_names)

    def start_all_bots(self):
        configured = self._configured_bots_from_disk()
        ipc_error = None
        try:
            bot_map = self._client().get_bots()
        except (ASFError, ValueError, OSError) as exc:
            bot_map = {}
            ipc_error = exc
        names = {name.casefold(): name for name in configured}
        for name in bot_map:
            names.setdefault(str(name).casefold(), str(name))
        bot_names = sorted(names.values(), key=str.casefold)
        if not bot_names:
            if ipc_error:
                self.status.setText(
                    f"Не удалось получить список ASF: {ipc_error}. "
                    "Проверь папку config ASF — без списка аккаунтов запуск невозможен."
                )
            else:
                self.status.setText("В конфигурации ASF не найдено ботов для запуска.")
            return
        answer = QMessageBox.question(
            self, "Запуск всех ботов",
            f"Отправить ASF команду запуска для всех известных ботов ({len(bot_names)})? "
            "При недоступном IPC GameDelivery сначала попробует запустить ASF.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText("Запуск всех ботов отменён.")
            return
        if ipc_error:
            self._pending_start_bot_names = bot_names
            self.start_asf()
        else:
            self._start_bots(bot_names)

    def _start_or_launch_asf(self, bot_names: list[str]):
        try:
            self._client().get_status()
        except (ASFError, ValueError, OSError):
            self._pending_start_bot_names = list(bot_names)
            self.start_asf()
            return
        self._start_bots(bot_names)

    def _set_bots_enabled(self, bot_names: list[str]):
        config_text = self.config_path.text().strip()
        config_dir = Path(config_text) if config_text else None
        if config_dir is None or not config_dir.is_dir():
            QMessageBox.warning(self, "Включение ботов", "Сначала укажи папку config установленного ASF.")
            return
        targets = []
        try:
            for bot_name in bot_names:
                path = config_dir / f"{bot_name}.json"
                if not path.is_file():
                    raise ValueError(f"Не найден файл конфигурации для бота {bot_name}.")
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(payload, dict):
                    raise ValueError(f"Конфигурация бота {bot_name} имеет неверный формат.")
                targets.append((bot_name, path, payload))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            QMessageBox.warning(self, "Включение ботов остановлено", str(exc))
            return
        if not targets:
            self.status.setText("Нет ботов для включения.")
            return
        answer = QMessageBox.question(
            self, "Включить ботов",
            f"Установить Enabled=true в конфигурациях {len(targets)} ботов? После этого потребуется перезапустить ASF.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText("Включение ботов отменено.")
            return
        originals = {path: path.read_bytes() for _, path, _ in targets}
        written = []
        try:
            for bot_name, path, payload in targets:
                payload["Enabled"] = True
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                written.append(path)
        except (OSError, TypeError, ValueError):
            for path in reversed(written):
                try:
                    path.write_bytes(originals[path])
                except OSError:
                    pass
            QMessageBox.warning(self, "Включение ботов", "Не удалось изменить все конфигурации; выполнен откат, где это удалось.")
            return
        self.status.setText(f"Enabled=true записан для {len(targets)} ботов. Перезапусти ASF и обнови список.")
        QMessageBox.information(
            self, "Боты включены в конфигурации",
            f"Обновлено конфигураций: {len(targets)}.\nЭто разрешает ASF запускать ботов при загрузке; фактический вход проверь после перезапуска.",
        )

    def enable_selected_bots(self):
        bot_names = self._selected_bot_names()
        if not bot_names:
            self.status.setText("Выбери одного или нескольких ботов в таблице.")
            return
        self._set_bots_enabled(bot_names)

    def enable_all_configured_bots(self):
        config_text = self.config_path.text().strip()
        config_dir = Path(config_text) if config_text else None
        if config_dir is None or not config_dir.is_dir():
            QMessageBox.warning(self, "Включение всех ботов", "Сначала укажи папку config установленного ASF.")
            return
        bot_names = sorted(
            (
                p.stem for p in config_dir.glob("*.json")
                if p.is_file() and p.name.casefold() != "asf.json"
            ),
            key=str.casefold,
        )
        self._set_bots_enabled(bot_names)

    def _start_bots(self, bot_names: list[str]):
        self.start_selected_button.setEnabled(False)
        self.start_all_button.setEnabled(False)
        started = []
        failed = []
        client = self._client()
        try:
            for bot_name in bot_names:
                try:
                    client.start_bot(bot_name)
                    started.append(bot_name)
                except (ASFError, ValueError, OSError):
                    failed.append(bot_name)
            self.refresh_bots()
            if failed:
                self.status.setText(
                    f"Команды запуска отправлены: {len(started)} из {len(bot_names)}. "
                    f"Не удалось отправить для: {', '.join(failed[:8])}"
                    + ("…" if len(failed) > 8 else "")
                    + ". Перечитай состояние после входа ASF."
                )
            else:
                self.status.setText(
                    f"Команды запуска отправлены для {len(started)} ботов. "
                    "Фактическое подключение проверь по состояниям и журналу ASF."
                )
        finally:
            self.start_selected_button.setEnabled(True)
            self.start_all_button.setEnabled(True)

    def restart_asf(self):
        answer = QMessageBox.question(
            self, "Перезапуск ASF",
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
            self._asf_ipc_attempts = 0
            self.status.setText("Команда перезапуска отправлена ASF. GameDelivery автоматически восстановит IPC.")
            QTimer.singleShot(1000, self._poll_asf_ipc)
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
        self._last_inventory = None
        self._last_inventory_bot = None
        self.export_inventory_button.setEnabled(False)
        try:
            payload = self._client().get_bot_inventory(bot_name)
            self._last_inventory = payload
            self._last_inventory_bot = bot_name
            safe_payload = self._sanitize_inventory(payload)
            self._last_inventory_export_payload = safe_payload
            self.export_inventory_button.setEnabled(True)
            dialog = QMessageBox(self)
            dialog.setWindowTitle(f"Инвентарь ASF — {bot_name}")
            dialog.setText(f"Ответ ASF для бота {bot_name}")
            dialog.setDetailedText(json.dumps(safe_payload, ensure_ascii=False, indent=2))
            dialog.setIcon(QMessageBox.Icon.Information)
            dialog.exec()
            self.status.setText(f"Инвентарь получен через ASF для бота {bot_name}.")
        except (ASFError, ValueError, OSError) as exc:
            self.status.setText(f"Не удалось получить инвентарь {bot_name}: {exc}")
        finally:
            self.inventory_button.setEnabled(True)

    @classmethod
    def _sanitize_inventory(cls, value):
        secret_markers = ("password", "token", "secret", "apikey", "api_key", "authorization", "cookie")
        if isinstance(value, dict):
            return {key: cls._sanitize_inventory(item) for key, item in value.items()
                    if not any(marker in str(key).casefold() for marker in secret_markers)}
        if isinstance(value, list):
            return [cls._sanitize_inventory(item) for item in value]
        return value

    def export_inventory(self):
        if self._last_inventory_export_payload is None or not self._last_inventory_bot:
            self.status.setText("Сначала загрузи инвентарь выбранного бота.")
            return
        default_name = f"{self._last_inventory_bot}-inventory.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт инвентаря ASF", str(Path.home() / default_name), "JSON (*.json)"
        )
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self._last_inventory_export_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "Экспорт инвентаря", f"Не удалось сохранить JSON: {exc}")
            return
        self.status.setText(f"Инвентарь бота {self._last_inventory_bot} сохранён в JSON.")

    def refresh_inventory_snapshot(self):
        row = self.table.currentRow()
        if row < 0 or self.table.item(row, 0) is None:
            self.status.setText("Сначала выбери бота в списке.")
            return
        bot_name = self.table.item(row, 0).text()
        self.snapshot_button.setEnabled(False)
        try:
            controller = ASFInventoryController(Path.home() / ".gamedelivery" / "gamedelivery.sqlite3")
            result = controller.refresh(bot_name, self.ipc_password.text() or None)
        except (ASFError, ValueError, RuntimeError, OSError) as exc:
            self.status.setText(f"Снимок инвентаря не обновлён для {bot_name}: {exc}")
            return
        finally:
            self.snapshot_button.setEnabled(True)
        count = result.get("item_count", result.get("count", "неизвестно")) if isinstance(result, dict) else "неизвестно"
        self.status.setText(
            f"Снимок инвентаря ASF обновлён для {bot_name}. Элементов: {count}. "
            "Снимок сохранён раздельно для этого бота."
        )
        self.inventory_snapshot_updated.emit(bot_name)
