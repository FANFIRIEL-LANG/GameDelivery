from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from app.bot_selector import configured_bot_names
from app.game_inventory_cards import GameItemCard, GameItemImageLoader
from app.inventory_panel import InventoryPanel
from games.catalog import GAME_CATALOG
from games.inventory import (\n    GameInventoryStore, refresh_game_inventory, filter_game_inventory_items,\n)
from integrations.asf.client import ASFClient


class GameInventoryPanel(QWidget):
    """Read-only visual inventory for game items across ASF bot accounts."""

    trade_url_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.database_path = Path.home() / ".gamedelivery" / "gamedelivery.sqlite3"
        self.store = GameInventoryStore(self.database_path)
        self.items = []
        self.cards_by_image: dict[str, list[GameItemCard]] = {}

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Карточки предметов из локальных снимков ASF. Наведи курсор на предмет, "
            "чтобы увидеть аккаунт-владельца, качество, StatTrak и характеристики. "
            "Раздел работает только на чтение: резервы и обмены отключены."
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

        controls.addWidget(QLabel("Аккаунты"))
        self.bot_list = QListWidget()
        self.bot_list.setMaximumHeight(92)
        self.bot_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        controls.addWidget(self.bot_list, 2)
        account_actions = QVBoxLayout()
        self.select_all_bots_button = QPushButton("Выбрать всех")
        self.clear_bots_button = QPushButton("Снять выбор")
        account_actions.addWidget(self.select_all_bots_button)
        account_actions.addWidget(self.clear_bots_button)
        controls.addLayout(account_actions)

        controls.addWidget(QLabel("IPC-пароль"))
        self.password = QLineEdit(InventoryPanel._load_saved_ipc_password())
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Если задан в ASF")
        controls.addWidget(self.password, 1)

        self.load_button = QPushButton("Обновить выбранные аккаунты")
        self.load_button.clicked.connect(self.fetch_inventory)
        controls.addWidget(self.load_button)
        self.refresh_button = QPushButton("Обновить список")
        self.refresh_button.clicked.connect(self.load_saved)
        controls.addWidget(self.refresh_button)
        layout.addLayout(controls)

        trade_row = QHBoxLayout()
        self.trade_url = QLineEdit()
        self.trade_url.setPlaceholderText("HTTPS Steam Trade URL получателя")
        self.send_to_order_button = QPushButton("Отправить через заказы…")
        self.send_to_order_button.clicked.connect(
            lambda: self.trade_url_requested.emit(self.trade_url.text().strip())
        )
        trade_row.addWidget(self.trade_url, 1)
        trade_row.addWidget(self.send_to_order_button)
        layout.addLayout(trade_row)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по предмету, типу, аккаунту, asset ID…")
        self.search.textChanged.connect(self.render)
        filters.addWidget(self.search, 3)

        self.item_filter = QComboBox()
        self.item_filter.addItem("Все предметы", "all")
        self.item_filter.addItem("Только контейнеры", "containers")
        self.item_filter.currentIndexChanged.connect(self.render)
        filters.addWidget(self.item_filter, 1)

        self.rarity_filter = QComboBox()
        self.rarity_filter.addItem("Любая редкость", "")
        self.rarity_filter.currentIndexChanged.connect(self.render)
        filters.addWidget(self.rarity_filter, 1)

        self.quality_filter = QComboBox()
        self.quality_filter.addItem("Любое качество", "")
        self.quality_filter.currentIndexChanged.connect(self.render)
        filters.addWidget(self.quality_filter, 1)

        self.transfer_filter = QComboBox()
        self.transfer_filter.addItem("Любая передаваемость", "all")
        self.transfer_filter.addItem("Передаваемые", "yes")
        self.transfer_filter.addItem("Непередаваемые", "no")
        self.transfer_filter.addItem("Не проверена", "unknown")
        self.transfer_filter.currentIndexChanged.connect(self.render)
        filters.addWidget(self.transfer_filter, 1)
        layout.addLayout(filters)

        self.status = QLabel("Выбери игру. Для общего списка сначала загрузи снимки нужных аккаунтов.")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.cards_host = QWidget()
        self.cards_layout = QGridLayout(self.cards_host)
        self.cards_layout.setContentsMargins(8, 8, 8, 8)
        self.cards_layout.setSpacing(10)
        self.scroll.setWidget(self.cards_host)
        layout.addWidget(self.scroll, 1)

        self.image_loader = GameItemImageLoader(self, self._image_loaded)
        self.game_combo.currentIndexChanged.connect(self._game_changed)
        self.select_all_bots_button.clicked.connect(self.select_all_bots)
        self.clear_bots_button.clicked.connect(self.clear_bot_selection)
        self._populate_accounts()
        self.load_saved()

    def _game_changed(self, *_):
        is_cs2 = self.game_combo.currentData() == "cs2"
        self.item_filter.setVisible(is_cs2)
        if not is_cs2:
            self.item_filter.setCurrentIndex(0)
        self.load_saved()

    def _populate_accounts(self):
        names = sorted(set(configured_bot_names()), key=str.casefold)
        self.bot_list.clear()
        for name in names:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.bot_list.addItem(item)

    def _selected_bot_names(self) -> list[str]:
        return [
            str(self.bot_list.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(self.bot_list.count())
            if self.bot_list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def select_all_bots(self):
        for i in range(self.bot_list.count()):
            self.bot_list.item(i).setCheckState(Qt.CheckState.Checked)
        self.load_saved()

    def clear_bot_selection(self):
        for i in range(self.bot_list.count()):
            self.bot_list.item(i).setCheckState(Qt.CheckState.Unchecked)
        self.load_saved()

    def fetch_inventory(self):
        game_id = str(self.game_combo.currentData() or "")
        bot_names = self._selected_bot_names()
        if not game_id or not bot_names:
            self.status.setText("Выбери аккаунты для загрузки из ASF.")
            return
        self.load_button.setEnabled(False)
        succeeded, failed = 0, []
        try:
            client = ASFClient(ipc_password=self.password.text().strip() or None, timeout=45.0)
            for index, bot_name in enumerate(bot_names, 1):
                self.status.setText(f"Загрузка {game_id}: {index}/{len(bot_names)} · {bot_name}…")
                try:
                    refresh_game_inventory(client, self.store, game_id, bot_name)
                    succeeded += 1
                except Exception as exc:
                    failed.append(f"{bot_name}: {exc}")
            self.load_saved()
            message = f"Обновлено аккаунтов: {succeeded} из {len(bot_names)}."
            if failed:
                message += " Ошибки: " + " | ".join(failed[:4])
            self.status.setText(message)
        finally:
            self.load_button.setEnabled(True)

    def load_saved(self):
        game_id = str(self.game_combo.currentData() or "")
        if not game_id:
            self.items = []
            self.render()
            return
        try:
            all_items = self.store.load_all(game_id)
            selected = set(self._selected_bot_names())
            self.items = [item for item in all_items if item.bot_name in selected]
            accounts = len({item.bot_name for item in self.items})
            self.status.setText(
                f"Локальные снимки: {game_id} · {accounts} аккаунтов · {len(self.items)} предметов. "
                "Общий список объединяет только выбранные аккаунты; актуальность нужно проверять."
            )
            self._refresh_filter_values()
            self.render()
        except Exception as exc:
            self.items = []
            self.render()
            self.status.setText(f"Не удалось прочитать снимок: {exc}")

    def _refresh_filter_values(self):
        current_rarity = self.rarity_filter.currentData()
        current_quality = self.quality_filter.currentData()
        self.rarity_filter.blockSignals(True)
        self.quality_filter.blockSignals(True)
        self.rarity_filter.clear()
        self.quality_filter.clear()
        self.rarity_filter.addItem("Любая редкость", "")
        self.quality_filter.addItem("Любое качество", "")
        for value in sorted({item.rarity for item in self.items if item.rarity}, key=str.casefold):
            self.rarity_filter.addItem(value, value)
        for value in sorted({item.exterior or item.quality for item in self.items if item.exterior or item.quality}, key=str.casefold):
            self.quality_filter.addItem(value, value)
        self.rarity_filter.setCurrentIndex(max(0, self.rarity_filter.findData(current_rarity)))
        self.quality_filter.setCurrentIndex(max(0, self.quality_filter.findData(current_quality)))
        self.rarity_filter.blockSignals(False)
        self.quality_filter.blockSignals(False)

    def _clear_cards(self):
        while self.cards_layout.count():
            child = self.cards_layout.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()
        self.cards_by_image.clear()

    def render(self, *_):
        if not hasattr(self, "cards_layout"):
            return
        query = self.search.text().strip().casefold()
        transfer = self.transfer_filter.currentData()
        rarity = self.rarity_filter.currentData()
        quality = self.quality_filter.currentData()
        filtered = []
        source_items = filter_game_inventory_items(
            self.items,
            containers_only=(
                self.game_combo.currentData() == "cs2"
                and self.item_filter.currentData() == "containers"
            ),
        )
        for item in source_items:
            if transfer == "yes" and item.is_tradable is not True:
                continue
            if transfer == "no" and item.is_tradable is not False:
                continue
            if transfer == "unknown" and item.is_tradable is not None:
                continue
            if rarity and item.rarity != rarity:
                continue
            if quality and (item.exterior or item.quality) != quality:
                continue
            haystack = " ".join((
                item.name, item.item_type, item.asset_id, item.quality, item.rarity,
                item.exterior, item.market_hash_name, item.bot_name, *item.tags,
            )).casefold()
            if query and query not in haystack:
                continue
            filtered.append(item)

        self._clear_cards()
        columns = max(1, self.width() // 205)
        for index, item in enumerate(filtered):
            card = GameItemCard(item, self.cards_host)
            self.cards_layout.addWidget(card, index // columns, index % columns)
            image_url = item.icon_url_large or item.icon_url
            if image_url:
                if image_url in self.image_loader.cache:
                    card.set_image(self.image_loader.cache[image_url])
                else:
                    self.cards_by_image.setdefault(image_url, []).append(card)
                    self.image_loader.request(image_url)
        self.status.setText(
            f"Показано {len(filtered)} из {len(self.items)} предметов · "
            f"владельцы видны при наведении на карточку."
        )

    def _image_loaded(self, reply):
        url = reply.request().url().toString()
        origin = self.image_loader.request_origin.get(url, url)
        self.image_loader.handle_reply(reply)
        pixmap = self.image_loader.cache.get(origin)
        if pixmap is not None:
            for card in self.cards_by_image.pop(origin, []):
                if card is not None:
                    card.set_image(pixmap)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.render()
