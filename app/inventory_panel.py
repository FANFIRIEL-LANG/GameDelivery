from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QLineEdit,
)

from games.steam_cards.inventory_views import build_inventory_view
from games.steam_cards.reservations import AssetReservationStore
from games.steam_cards.snapshot import InventorySnapshotStore
from app.asf_inventory_controller import ASFInventoryController
from integrations.asf.client import ASFError
from app.bot_selector import populate_bot_combo


class InventoryPanel(QWidget):
    """Read-only UI for local snapshots and reservations; never fetches or sends."""

    def __init__(self, parent=None):
        super().__init__(parent)
        app_data = Path.home() / ".gamedelivery"
        self.database_path = app_data / "gamedelivery.sqlite3"
        self.snapshots = InventorySnapshotStore(self.database_path)
        self.reservations = AssetReservationStore(self.database_path)
        self.asf_controller = ASFInventoryController(self.database_path)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Локальные снимки ASF/инвентаря и резервы. Автообновления нет; "
            "снимок не считается актуальным только потому, что он существует."
        )
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        layout.addWidget(intro)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Бот ASF"))
        self.bot_name_input = QComboBox()
        self.bot_name_input.setMinimumWidth(180)
        controls.addWidget(self.bot_name_input, 1)
        controls.addWidget(QLabel("IPC-пароль"))
        self.ipc_password_input = QLineEdit()
        self.ipc_password_input.setPlaceholderText("Если задан в ASF")
        self.ipc_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        controls.addWidget(self.ipc_password_input, 1)
        self.fetch_button = QPushButton("Загрузить из ASF")
        controls.addWidget(self.fetch_button)
        controls.addWidget(QLabel("Снимки"))
        self.bot_list = QListWidget()
        self.bot_list.setMaximumHeight(100)
        self.bot_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        controls.addWidget(self.bot_list, 2)
        controls.addWidget(QLabel("Режим"))
        self.mode = QComboBox()
        self.mode.addItem("Общий вид", "shared")
        self.mode.addItem("Раздельно по ботам", "separate")
        controls.addWidget(self.mode)
        self.refresh_button = QPushButton("Обновить")
        controls.addWidget(self.refresh_button)
        layout.addLayout(controls)

        self.summary = QLabel("Снимки не загружены.")
        self.summary.setObjectName("muted")
        layout.addWidget(self.summary)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Игра", "Карточка", "Тип", "Передача", "Количество", "Владелец / asset ID", "Резерв"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        self.refresh_button.clicked.connect(self.refresh)
        self.fetch_button.clicked.connect(self.fetch_from_asf)
        self.mode.currentIndexChanged.connect(self.render_inventory)
        self.bot_list.itemSelectionChanged.connect(self.render_inventory)
        self.refresh()

    def select_bot_snapshot(self, bot_name: str):
        """Refresh local inventory and select one bot after an ASF snapshot update."""
        self.refresh()
        for index in range(self.bot_list.count()):
            item = self.bot_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == bot_name:
                self.bot_list.clearSelection()
                item.setSelected(True)
                self.summary.setText(
                    f"Выбран снимок {bot_name}. Данные разделены по владельцам; "
                    "общий режим не меняет фактического владельца предметов."
                )
                return

    def fetch_from_asf(self):
        bot_name = str(self.bot_name_input.currentData() or "").strip()
        if not bot_name:
            self.summary.setText("Выбери бота из списка ASF. Если список пуст, сначала укажи папку config во вкладке «ASF-боты».")
            return
        self.fetch_button.setEnabled(False)
        self.summary.setText(f"Запрашиваю инвентарь {bot_name} через локальный ASF IPC…")
        try:
            result = self.asf_controller.refresh(
                bot_name, self.ipc_password_input.text()
            )
            self.refresh()
            for index in range(self.bot_list.count()):
                item = self.bot_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == bot_name:
                    item.setSelected(True)
            self.summary.setText(
                f"Снимок обновлён: {bot_name} · {result['item_count']} предметов · "
                f"{result['updated_at']}. Обмены не отправлялись."
            )
        except Exception as exc:
            self.summary.setText(
                f"Не удалось обновить инвентарь ASF: {exc}. Старый снимок сохранён."
            )
        finally:
            self.fetch_button.setEnabled(True)

    def refresh(self):
        try:
            self.inventories = self.snapshots.load_all()
            populate_bot_combo(self.bot_name_input, self.inventories.keys())
            self.statuses = {
                bot: self.snapshots.status(bot) for bot in self.inventories
            }
            self.reservation_rows = self._load_reservations()
            selected_before = {
                item.data(Qt.ItemDataRole.UserRole) for item in self.bot_list.selectedItems()
            }
            self.bot_list.clear()
            for bot in sorted(self.inventories, key=str.casefold):
                status = self.statuses[bot]
                timestamp = status["updated_at"] or "нет даты"
                label = f"{bot} — {status['item_count']} предметов — {timestamp}"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, bot)
                self.bot_list.addItem(item)
                if bot in selected_before:
                    item.setSelected(True)
            self.summary.setText(
                f"База: {self.database_path} · снимков: {len(self.inventories)}. "
                "Если список пуст, реальные данные ещё не импортированы в эту базу."
            )
            self.render_inventory()
        except Exception as exc:
            # Fail closed: never leave the previous in-memory snapshot looking current.
            self.inventories = {}
            self.statuses = {}
            self.reservation_rows = {}
            self.bot_list.clear()
            populate_bot_combo(self.bot_name_input)
            self.table.setRowCount(0)
            self.summary.setText(
                f"Не удалось прочитать локальную базу: {exc}. "
                "Отображение очищено; данные не подтверждены как актуальные."
            )

    def _load_reservations(self):
        import sqlite3
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT bot_name, asset_id, order_id FROM steam_asset_reservations "
                "ORDER BY bot_name, asset_id"
            ).fetchall()
        return {(bot, asset): order for bot, asset, order in rows}

    def render_inventory(self, *_):
        if not hasattr(self, "inventories"):
            return
        selected = [
            item.data(Qt.ItemDataRole.UserRole)
            for item in self.bot_list.selectedItems()
        ]
        view = build_inventory_view(
            self.inventories, selected, mode=self.mode.currentData()
        )
        self.table.setRowCount(0)
        for row_data in view.rows:
            owners = []
            reservations = []
            for bot, asset_ids in row_data.assets_by_bot:
                for asset_id in asset_ids:
                    owners.append(f"{bot}: {asset_id}")
                    order = self.reservation_rows.get((bot, asset_id))
                    if order:
                        reservations.append(f"{bot}: {order}")
            row = self.table.rowCount()
            self.table.insertRow(row)
            kind = "Обычная карточка" if row_data.is_trading_card and not row_data.is_foil else (
                "Foil" if row_data.is_foil else "Другой предмет"
            )
            transferability = (
                "Да" if row_data.is_tradable is True
                else "Нет" if row_data.is_tradable is False
                else "Не проверена"
            )
            values = [
                row_data.game_name,
                row_data.card_name,
                kind,
                transferability,
                str(row_data.quantity),
                "\n".join(owners),
                "\n".join(reservations) if reservations else "Нет",
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
        if not selected:
            self.summary.setText(
                "Выбери один или несколько ботов. Без выбора инвентарь намеренно пуст."
            )
        else:
            updated = [
                f"{bot}: {self.statuses.get(bot, {}).get('updated_at') or 'нет снимка'}"
                for bot in selected
            ]
            self.summary.setText(
                f"Выбрано ботов: {len(selected)} · строк: {len(view.rows)} · "
                + " | ".join(updated)
            )
