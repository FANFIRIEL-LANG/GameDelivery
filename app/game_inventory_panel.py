from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.bot_selector import populate_bot_combo
from app.inventory_panel import InventoryPanel
from games.catalog import GAME_CATALOG
from games.inventory import GameInventoryStore, refresh_game_inventory
from integrations.asf.client import ASFClient


class GameInventoryPanel(QWidget):
    """Read-only viewer for non-card game inventory snapshots fetched via local ASF."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.database_path = Path.home() / ".gamedelivery" / "gamedelivery.sqlite3"
        self.store = GameInventoryStore(self.database_path)
        self.items = []

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Игровые предметы загружаются вручную через локальный ASF IPC. "
            "Этот экран только читает инвентарь: резервы и обмены здесь отключены."
        )
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        layout.addWidget(intro)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Игра"))
        self.game_combo = QComboBox()
        for game in GAME_CATALOG:
            if game.game_id != "steam_cards":
                self.game_combo.addItem(game.name, game.game_id)
        controls.addWidget(self.game_combo, 1)

        controls.addWidget(QLabel("Бот ASF"))
        self.bot_combo = QComboBox()
        populate_bot_combo(self.bot_combo)
        controls.addWidget(self.bot_combo, 1)

        controls.addWidget(QLabel("IPC-пароль"))
        self.password = QLineEdit(InventoryPanel._load_saved_ipc_password())
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Если задан в ASF")
        controls.addWidget(self.password, 1)

        self.load_button = QPushButton("Загрузить из ASF")
        self.load_button.clicked.connect(self.fetch_inventory)
        controls.addWidget(self.load_button)
        self.refresh_button = QPushButton("Показать сохранённое")
        self.refresh_button.clicked.connect(self.load_saved)
        controls.addWidget(self.refresh_button)
        layout.addLayout(controls)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по названию, типу, asset ID, качеству…")
        self.search.textChanged.connect(self.render)
        filters.addWidget(self.search, 3)
        self.transfer_filter = QComboBox()
        self.transfer_filter.addItem("Любая передаваемость", "all")
        self.transfer_filter.addItem("Передаваемые", "yes")
        self.transfer_filter.addItem("Непередаваемые", "no")
        self.transfer_filter.addItem("Не проверена", "unknown")
        self.transfer_filter.currentIndexChanged.connect(self.render)
        filters.addWidget(self.transfer_filter, 1)
        layout.addLayout(filters)

        self.status = QLabel("Выбери игру и бота ASF.")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "Предмет", "Тип", "Качество", "Редкость", "Износ",
            "Передаваемый", "Торгуемый", "Бот", "Asset ID",
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.load_saved()

    def _selected(self):
        return str(self.game_combo.currentData() or ""), str(self.bot_combo.currentData() or "")

    def fetch_inventory(self):
        game_id, bot_name = self._selected()
        if not game_id or not bot_name:
            self.status.setText("Выбери игру и существующего бота ASF.")
            return
        self.load_button.setEnabled(False)
        self.status.setText(f"Запрашиваю {game_id} для {bot_name} через локальный ASF IPC…")
        try:
            client = ASFClient(ipc_password=self.password.text().strip() or None, timeout=45.0)
            result = refresh_game_inventory(client, self.store, game_id, bot_name)
            self.items = self.store.load(game_id, bot_name)
            self.render()
            self.status.setText(
                f"Снимок обновлён: {bot_name} · {result['item_count']} предметов · "
                f"{result['updated_at']}. Никаких обменов не выполнялось."
            )
        except Exception as exc:
            self.status.setText(f"Не удалось обновить инвентарь: {exc}. Старый снимок сохранён.")
        finally:
            self.load_button.setEnabled(True)

    def load_saved(self):
        game_id, bot_name = self._selected()
        if not game_id or not bot_name:
            self.items = []
            self.render()
            return
        try:
            self.items = self.store.load(game_id, bot_name)
            self.status.setText(
                f"Локальный снимок: {game_id} · {bot_name} · {len(self.items)} предметов. "
                "Наличие снимка не означает, что он актуален."
            )
            self.render()
        except Exception as exc:
            self.items = []
            self.render()
            self.status.setText(f"Не удалось прочитать снимок: {exc}")

    def render(self, *_):
        if not hasattr(self, "table"):
            return
        query = self.search.text().strip().casefold()
        transfer = self.transfer_filter.currentData()
        filtered = []
        for item in self.items:
            if transfer == "yes" and item.is_tradable is not True:
                continue
            if transfer == "no" and item.is_tradable is not False:
                continue
            if transfer == "unknown" and item.is_tradable is not None:
                continue
            haystack = " ".join((
                item.name, item.item_type, item.asset_id, item.quality,
                item.rarity, item.exterior, item.market_hash_name, item.bot_name,
            )).casefold()
            if query and query not in haystack:
                continue
            filtered.append(item)
        self.table.setRowCount(len(filtered))
        for row, item in enumerate(filtered):
            values = [
                item.name, item.item_type, item.quality, item.rarity, item.exterior,
                "Да" if item.is_tradable is True else "Нет" if item.is_tradable is False else "Не проверено",
                "Да" if item.is_marketable is True else "Нет" if item.is_marketable is False else "Не проверено",
                item.bot_name, item.asset_id,
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 8:
                    cell.setData(Qt.ItemDataRole.UserRole, item.asset_id)
                self.table.setItem(row, column, cell)
        self.table.resizeColumnsToContents()
