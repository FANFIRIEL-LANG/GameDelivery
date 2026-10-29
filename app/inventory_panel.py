from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QSize, QUrl
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QLineEdit,
)

from games.steam_cards.inventory_views import build_inventory_view, expand_inventory_instances
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
        self.ipc_password_input.setText(self._load_saved_ipc_password())
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

        self.item_grid = QListWidget()
        self.item_grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.item_grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.item_grid.setMovement(QListWidget.Movement.Static)
        self.item_grid.setWrapping(True)
        self.item_grid.setWordWrap(True)
        self.item_grid.setIconSize(QSize(76, 76))
        self.item_grid.setGridSize(QSize(156, 132))
        self.item_grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.item_grid.setSpacing(8)
        layout.addWidget(self.item_grid, 1)
        self.instance_summary = QLabel("Выбери карточку, чтобы выбрать конкретные экземпляры.")
        self.instance_summary.setObjectName("muted")
        layout.addWidget(self.instance_summary)
        self.instance_list = QListWidget()
        self.instance_list.setMaximumHeight(150)
        self.instance_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.instance_list.itemChanged.connect(self._instance_check_changed)
        layout.addWidget(self.instance_list)
        self._selected_asset_keys: set[tuple[str, str]] = set()
        self.item_grid.itemSelectionChanged.connect(self._render_instance_choices)
        self.table.hide()

        self.view_mode = QComboBox()
        self.view_mode.addItem("Карточки предметов", "cards")
        self.view_mode.addItem("Таблица подробностей", "table")
        controls.addWidget(self.view_mode)
        self._image_cache: dict[str, QIcon] = {}
        self._image_requests: set[str] = set()
        self._image_manager = QNetworkAccessManager(self)
        self._image_manager.finished.connect(self._image_loaded)

        self.refresh_button.clicked.connect(self.refresh)
        self.fetch_button.clicked.connect(self.fetch_from_asf)
        self.mode.currentIndexChanged.connect(self.render_inventory)
        self.bot_list.itemSelectionChanged.connect(self.render_inventory)
        self.view_mode.currentIndexChanged.connect(self._toggle_view)
        self.refresh()

    @staticmethod
    def _load_saved_ipc_password() -> str:
        """Reuse the ASF IPC credential saved by ASF management or read ASF.json."""
        try:
            import keyring
            backend = keyring.get_keyring()
            if getattr(backend, "priority", 0) >= 1:
                saved = keyring.get_password("GameDelivery.ASFIPC", "localhost:1242")
                if saved:
                    return saved
        except Exception:
            # The credential store is optional; fall back to the selected ASF config.
            pass

        settings = QSettings("FANFIRIEL-LANG", "GameDelivery")
        config_text = str(settings.value("asf/config_path", "") or "").strip()
        if not config_text:
            return ""
        try:
            payload = json.loads((Path(config_text) / "ASF.json").read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if isinstance(payload, dict):
            for key, value in payload.items():
                if str(key).casefold() == "ipcpassword" and isinstance(value, str):
                    return value
        return ""

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

    def _render_instance_choices(self):
        self.instance_list.blockSignals(True)
        self.instance_list.clear()
        selected_cards = self.item_grid.selectedItems()
        available: list[tuple[str, str]] = []
        for card_item in selected_cards:
            row_data = card_item.data(Qt.ItemDataRole.UserRole + 2)
            if row_data is not None:
                available.extend(expand_inventory_instances(row_data))
        for bot_name, asset_id in available:
            key = (bot_name, asset_id)
            choice = QListWidgetItem(f"{bot_name} — {asset_id}")
            choice.setFlags(choice.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            choice.setData(Qt.ItemDataRole.UserRole, key)
            choice.setCheckState(
                Qt.CheckState.Checked if key in self._selected_asset_keys else Qt.CheckState.Unchecked
            )
            self.instance_list.addItem(choice)
        self.instance_list.blockSignals(False)
        self._update_instance_summary()

    def _instance_check_changed(self, item):
        key = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(key, tuple) or len(key) != 2:
            return
        normalized = (str(key[0]), str(key[1]))
        if item.checkState() == Qt.CheckState.Checked:
            self._selected_asset_keys.add(normalized)
        else:
            self._selected_asset_keys.discard(normalized)
        self._update_instance_summary()

    def _update_instance_summary(self):
        self.instance_summary.setText(
            f"Выбрано экземпляров: {len(self._selected_asset_keys)}. "
            "Каждый экземпляр сохраняет своего бота и asset ID."
        )

    def _toggle_view(self, *_):
        show_table = self.view_mode.currentData() == "table"
        self.table.setVisible(show_table)
        self.item_grid.setVisible(not show_table)

    def _request_item_image(self, url: str):
        if url in self._image_requests:
            return
        self._image_requests.add(url)
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(QNetworkRequest.Attribute.FollowRedirectsAttribute, True)
        self._image_manager.get(request)

    def _image_loaded(self, reply):
        url = reply.request().url().toString()
        self._image_requests.discard(url)
        pixmap = QPixmap()
        if reply.error() == QNetworkReply.NetworkError.NoError:
            pixmap.loadFromData(bytes(reply.readAll()))
        reply.deleteLater()
        if pixmap.isNull():
            return
        icon = QIcon(pixmap.scaled(
            QSize(76, 76), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        self._image_cache[url] = icon
        for index in range(self.item_grid.count()):
            item = self.item_grid.item(index)
            if item.data(Qt.ItemDataRole.UserRole + 1) == url:
                item.setIcon(icon)

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
        self.item_grid.clear()
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

            owner_lines = [
                f"{bot}: {len(asset_ids)} шт. · " + ", ".join(asset_ids)
                for bot, asset_ids in row_data.assets_by_bot
            ]
            quality = f" · {row_data.item_quality}" if row_data.item_quality else ""
            label = f"{row_data.card_name}{quality}\n×{row_data.quantity}"
            tile = QListWidgetItem(label)
            tile.setData(Qt.ItemDataRole.UserRole, row_data.assets_by_bot)
            tile.setData(Qt.ItemDataRole.UserRole + 1, row_data.icon_url_large or row_data.icon_url)
            tile.setData(Qt.ItemDataRole.UserRole + 2, row_data)
            tile.setToolTip(
                f"{row_data.card_name}{quality}\nИгра: {row_data.game_name}\n"
                f"Всего: {row_data.quantity}\nВладельцы:\n" + "\n".join(owner_lines)
            )
            icon_url = row_data.icon_url_large or row_data.icon_url
            if icon_url and icon_url in self._image_cache:
                tile.setIcon(self._image_cache[icon_url])
            else:
                tile.setIcon(QIcon())
                if icon_url:
                    self._request_item_image(icon_url)
            self.item_grid.addItem(tile)
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
