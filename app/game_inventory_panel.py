from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from app.bot_selector import populate_bot_combo
from app.game_inventory_cards import GameItemCard, GameItemImageLoader
from app.inventory_panel import InventoryPanel
from games.catalog import GAME_CATALOG
from games.inventory import GameInventoryStore, refresh_game_inventory
from integrations.asf.client import ASFClient


class GameInventoryPanel(QWidget):
    """Read-only visual inventory for game items across ASF bot accounts."""

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

        controls.addWidget(QLabel("Аккаунт"))
        self.bot_combo = QComboBox()
        populate_bot_combo(self.bot_combo)
        self.bot_combo.insertItem(0, "Все аккаунты", "__all__")
        self.bot_combo.setCurrentIndex(0)
        controls.addWidget(self.bot_combo, 1)

        controls.addWidget(QLabel("IPC-пароль"))
        self.password = QLineEdit(InventoryPanel._load_saved_ipc_password())
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Если задан в ASF")
        controls.addWidget(self.password, 1)

        self.load_button = QPushButton("Загрузить выбранный аккаунт")
        self.load_button.clicked.connect(self.fetch_inventory)
        controls.addWidget(self.load_button)
        self.refresh_button = QPushButton("Обновить список")
        self.refresh_button.clicked.connect(self.load_saved)
        controls.addWidget(self.refresh_button)
        layout.addLayout(controls)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по предмету, типу, аккаунту, asset ID…")
        self.search.textChanged.connect(self.render)
        filters.addWidget(self.search, 3)

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
        self.game_combo.currentIndexChanged.connect(self.load_saved)
        self.bot_combo.currentIndexChanged.connect(self._account_changed)
        self.load_saved()

    def _selected(self):
        return str(self.game_combo.currentData() or ""), str(self.bot_combo.currentData() or "")

    def _account_changed(self):
        self.load_button.setEnabled(self.bot_combo.currentData() != "__all__")
        self.load_saved()

    def fetch_inventory(self):
        game_id, bot_name = self._selected()
        if not game_id or not bot_name or bot_name == "__all__":
            self.status.setText("Выбери конкретный аккаунт для загрузки из ASF.")
            return
        self.load_button.setEnabled(False)
        self.status.setText(f"Запрашиваю {game_id} для {bot_name} через локальный ASF IPC…")
        try:
            client = ASFClient(ipc_password=self.password.text().strip() or None, timeout=45.0)
            result = refresh_game_inventory(client, self.store, game_id, bot_name)
            self.status.setText(
                f"Снимок обновлён: {bot_name} · {result['item_count']} предметов · "
                f"{result['updated_at']}. Обмены не выполнялись."
            )
            self.load_saved()
        except Exception as exc:
            self.status.setText(f"Не удалось обновить инвентарь: {exc}. Старый снимок сохранён.")
        finally:
            self.load_button.setEnabled(self.bot_combo.currentData() != "__all__")

    def load_saved(self):
        game_id, bot_name = self._selected()
        if not game_id:
            self.items = []
            self.render()
            return
        try:
            self.items = (
                self.store.load_all(game_id) if bot_name == "__all__"
                else self.store.load(game_id, bot_name) if bot_name
                else []
            )
            accounts = len({item.bot_name for item in self.items})
            self.status.setText(
                f"Локальные снимки: {game_id} · {accounts} аккаунтов · {len(self.items)} предметов. "
                "Общий список объединяет только ранее загруженные снимки; актуальность нужно проверять."
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
        for item in self.items:
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
        self.image_loader.pending.discard(url)
        if reply.error():
            reply.deleteLater()
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(reply.readAll()):
            self.image_loader.cache[url] = pixmap
            for card in self.cards_by_image.pop(url, []):
                if card is not None:
                    card.set_image(pixmap)
        reply.deleteLater()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.render()
