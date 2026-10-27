from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.asf_inventory_controller import ASFInventoryController
from integrations.asf.client import ASFClient, ASFError


class ASFManagementWidget(QWidget):
    """Manage ASF bots and import account lists from a local TXT file."""

    inventory_snapshot_updated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
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
        self.txt_path.setPlaceholderText("TXT: одна строка login;password")
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
        self.config_button = QPushButton("Папка config ASF…")
        self.config_button.clicked.connect(self.choose_config)
        config_row.addWidget(self.config_path, 1)
        config_row.addWidget(self.config_button)
        root.addLayout(config_row)

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

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Имя бота", "Состояние"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table, 1)
        self.status = QLabel("ASF не запрашивался.")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._last_inventory = None
        self._last_inventory_bot = None
        self._last_inventory_export_payload = None
        self.refresh_button.clicked.connect(self.refresh_bots)
        self.import_button.clicked.connect(self.import_accounts)
        self.restart_button.clicked.connect(self.restart_asf)
        self.inventory_button.clicked.connect(self.show_inventory)
        self.export_inventory_button.clicked.connect(self.export_inventory)
        self.snapshot_button.clicked.connect(self.refresh_inventory_snapshot)
        self.export_inventory_button.setEnabled(False)

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
                parts = line.split(";")
                if len(parts) != 2:
                    raise ValueError(f"Строка {line_no}: ожидается формат login;password")
                login, password = (part.strip() for part in parts)
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
            self.table.setRowCount(0)
            self._last_inventory = None
            self._last_inventory_bot = None
            self._last_inventory_export_payload = None
            self.export_inventory_button.setEnabled(False)
            self.status.setText(f"Не удалось получить список ASF: {exc}. Список очищен.")
        finally:
            self.refresh_button.setEnabled(True)

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
            self.status.setText("Команда перезапуска отправлена ASF. Подключение может временно пропасть.")
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
