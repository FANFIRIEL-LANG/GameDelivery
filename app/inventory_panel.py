from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QSize, QUrl, QObject, QThread, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QLineEdit, QSpinBox,
)

from games.steam_cards.selection import find_available_sets
from games.steam_cards.inventory_views import (
    build_inventory_view, expand_inventory_instances, filter_inventory_rows, select_inventory_instances,
)

from games.steam_cards.reservations import AssetReservationStore
from games.steam_cards.snapshot import InventorySnapshotStore
from app.asf_inventory_controller import ASFInventoryController
from integrations.asf.client import ASFError
from app.bot_selector import populate_bot_combo, configured_bot_names, connect_account_search



class _InventoryBatchWorker(QObject):
    progress = Signal(int, int, str, str)
    finished = Signal(list)

    def __init__(self, bot_names: list[str], ipc_password: str, database_path):
        super().__init__()
        self.bot_names = list(bot_names)
        self.ipc_password = ipc_password
        self.database_path = database_path

    def run(self):
        results = []
        controller = ASFInventoryController(self.database_path)
        total = len(self.bot_names)
        for index, bot_name in enumerate(self.bot_names, 1):
            self.progress.emit(index, total, bot_name, "Запрашиваю инвентарь через ASF…")
            try:
                result = controller.refresh(bot_name, self.ipc_password)
                results.append({"bot": bot_name, "ok": True,
                    "item_count": int(result.get("item_count", 0)),
                    "updated_at": str(result.get("updated_at", "")), "error": ""})
                self.progress.emit(index, total, bot_name,
                    f"Готово: {result.get('item_count', 0)} предметов")
            except Exception as exc:
                results.append({"bot": bot_name, "ok": False, "item_count": None,
                    "updated_at": "", "error": str(exc)})
                self.progress.emit(index, total, bot_name, f"Ошибка: {exc}")
        self.finished.emit(results)



class InventoryPanel(QWidget):
    """Read-only UI for local snapshots and reservations."""

    trade_url_requested = Signal(str)

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
        # Bulk inventory refresh uses the single checked-account list below.
        # Keep the legacy single-account widgets hidden for compatibility with
        # older signal paths while removing the duplicate selector from the UI.
        self.bot_name_input.hide()
        controls.addWidget(QLabel("IPC-пароль"))
        self.ipc_password_input = QLineEdit()
        self.ipc_password_input.setPlaceholderText("Если задан в ASF")
        self.ipc_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.ipc_password_input.setText(self._load_saved_ipc_password())
        controls.addWidget(self.ipc_password_input, 1)
        self.fetch_button = QPushButton("Загрузить из ASF")
        controls.addWidget(self.fetch_button)
        self.fetch_button.hide()
        controls.addWidget(QLabel("Снимки"))
        account_selector = QVBoxLayout()
        self.account_search = QLineEdit()
        self.account_search.setPlaceholderText("Поиск аккаунта…")
        account_selector.addWidget(self.account_search)
        self.bot_list = QListWidget()
        self.bot_list.setMaximumHeight(100)
        connect_account_search(self.account_search, self.bot_list)
        self.bot_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        account_selector.addWidget(self.bot_list)
        controls.addLayout(account_selector, 2)
        bot_actions = QVBoxLayout()
        self.select_all_bots_button = QPushButton("Выбрать всех")
        self.clear_bots_button = QPushButton("Снять выбор ботов")
        self.fetch_selected_button = QPushButton("Проверить выбранных")
        bot_actions.addWidget(self.select_all_bots_button)
        bot_actions.addWidget(self.clear_bots_button)
        bot_actions.addWidget(self.fetch_selected_button)
        controls.addLayout(bot_actions)
        controls.addWidget(QLabel("Режим"))
        self.mode = QComboBox()
        self.mode.addItem("Общий вид", "shared")
        self.mode.addItem("Раздельно по ботам", "separate")
        controls.addWidget(self.mode)
        self.refresh_button = QPushButton("Обновить")
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

        self.summary = QLabel("Снимки не загружены.")
        self.summary.setObjectName("muted")
        layout.addWidget(self.summary)

        filters = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск: игра, предмет, бот или asset ID…")
        filters.addWidget(self.search_input, 3)
        self.item_filter = QComboBox()
        self.item_filter.addItem("Все предметы", "all")
        self.item_filter.addItem("Только карточки", "cards")
        self.item_filter.addItem("Без карточек", "items")
        self.item_filter.addItem("Foil", "foil")
        self.item_filter.addItem("Передаваемые", "tradable")
        self.item_filter.addItem("Непередаваемые", "nontradable")
        filters.addWidget(self.item_filter, 1)
        layout.addLayout(filters)

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
        quantity_controls = QHBoxLayout()
        quantity_controls.addWidget(QLabel("Количество экземпляров"))
        self.quantity_input = QSpinBox()
        self.quantity_input.setRange(0, 0)
        quantity_controls.addWidget(self.quantity_input)
        self.apply_quantity_button = QPushButton("Применить")
        self.clear_selection_button = QPushButton("Снять выбор")
        self.select_visible_button = QPushButton("Выбрать все видимые")
        self.sets_per_bot_spin = QSpinBox()
        self.sets_per_bot_spin.setRange(1, 999)
        self.sets_per_bot_spin.setValue(1)
        self.sets_per_bot_spin.setToolTip("Количество полных наборов отдельно для каждого выбранного бота")
        self.select_sets_button = QPushButton("Выбрать полные наборы")
        self.clear_item_selection_button = QPushButton("Снять выбор предметов")
        quantity_controls.addWidget(self.apply_quantity_button)
        quantity_controls.addWidget(self.clear_selection_button)
        quantity_controls.addWidget(self.select_visible_button)
        quantity_controls.addWidget(self.clear_item_selection_button)
        quantity_controls.addWidget(QLabel("Наборов на бота"))
        quantity_controls.addWidget(self.sets_per_bot_spin)
        quantity_controls.addWidget(self.select_sets_button)
        quantity_controls.addStretch(1)
        layout.addLayout(quantity_controls)
        self.select_visible_button.clicked.connect(self._select_all_visible_items)
        self.select_sets_button.clicked.connect(self._select_complete_sets_for_bots)
        self._selected_asset_keys: set[tuple[str, str]] = set()
        self.item_grid.itemSelectionChanged.connect(self._render_instance_choices)
        self.apply_quantity_button.clicked.connect(self._apply_quantity_selection)
        self.clear_selection_button.clicked.connect(self._clear_asset_selection)
        self.table.hide()

        self.view_mode = QComboBox()
        self.view_mode.addItem("Карточки предметов", "cards")
        self.view_mode.addItem("Таблица подробностей", "table")
        controls.addWidget(self.view_mode)
        self._image_cache: dict[str, QIcon] = {}
        self._image_requests: set[str] = set()
        self._image_attempted: dict[str, set[str]] = {}
        self._image_request_origin: dict[str, str] = {}
        self._image_manager = QNetworkAccessManager(self)
        self._image_manager.finished.connect(self._image_loaded)

        self.refresh_button.clicked.connect(self.refresh)
        self.fetch_button.clicked.connect(self.fetch_from_asf)
        self.select_all_bots_button.clicked.connect(self.select_all_bots)
        self.clear_bots_button.clicked.connect(self.clear_bot_selection)
        self.fetch_selected_button.clicked.connect(self.fetch_selected_from_asf)
        self._batch_thread = None
        self._batch_worker = None
        self._batch_results = []
        self.mode.currentIndexChanged.connect(self.render_inventory)
        self.search_input.textChanged.connect(self._filter_item_tiles)
        self.item_filter.currentIndexChanged.connect(self._filter_item_tiles)
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
            selected_before = self._checked_bot_names()
            # First opening selects every discovered account automatically.
            # Later refreshes preserve the operator's explicit selection.
            select_all_by_default = not selected_before
            self.bot_list.clear()
            all_bots = set(configured_bot_names()) | set(self.inventories)
            for bot in sorted(all_bots, key=str.casefold):
                status = self.statuses.get(bot) or {"updated_at": None, "item_count": 0}
                timestamp = status["updated_at"] or "ещё не загружен"
                label = f"{bot} — {status['item_count']} предметов — {timestamp}"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, bot)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if select_all_by_default or bot in selected_before
                    else Qt.CheckState.Unchecked
                )
                self.bot_list.addItem(item)
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

    def _checked_bot_names(self) -> list[str]:
        return [
            str(self.bot_list.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(self.bot_list.count())
            if self.bot_list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def select_all_bots(self):
        for index in range(self.bot_list.count()):
            self.bot_list.item(index).setCheckState(Qt.CheckState.Checked)
        self.render_inventory()

    def clear_bot_selection(self):
        for index in range(self.bot_list.count()):
            self.bot_list.item(index).setCheckState(Qt.CheckState.Unchecked)
        self.render_inventory()

    def fetch_selected_from_asf(self):
        if self._batch_thread is not None and self._batch_thread.isRunning():
            self.summary.setText("Массовая проверка уже выполняется.")
            return
        bot_names = self._checked_bot_names()
        if not bot_names:
            self.summary.setText("Сначала выбери ботов в списке снимков.")
            return
        for button in (self.fetch_selected_button, self.fetch_button,
                       self.select_all_bots_button, self.clear_bots_button, self.refresh_button):
            button.setEnabled(False)
        self.summary.setText(f"Начинаю проверку инвентарей: 0 из {len(bot_names)}…")
        self._batch_thread = QThread(self)
        self._batch_worker = _InventoryBatchWorker(
            bot_names, self.ipc_password_input.text(), self.database_path
        )
        self._batch_worker.moveToThread(self._batch_thread)
        self._batch_thread.started.connect(self._batch_worker.run)
        self._batch_worker.progress.connect(self._batch_progress)
        self._batch_worker.finished.connect(self._batch_finished)
        self._batch_worker.finished.connect(self._batch_thread.quit)
        self._batch_thread.finished.connect(self._batch_worker.deleteLater)
        self._batch_thread.finished.connect(self._batch_thread.deleteLater)
        self._batch_thread.start()

    def _batch_progress(self, index: int, total: int, bot_name: str, message: str):
        self.summary.setText(f"Проверка инвентарей: {index}/{total} · {bot_name} · {message}")

    def _batch_finished(self, results: list):
        self._batch_results = results
        succeeded = [row for row in results if row["ok"]]
        failed = [row for row in results if not row["ok"]]
        self.refresh()
        checked = {row["bot"] for row in results}
        for index in range(self.bot_list.count()):
            item = self.bot_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) in checked:
                item.setSelected(True)
        self.render_inventory()
        total_items = sum(row["item_count"] for row in succeeded)
        total_sets = sum(
            sum(item.available_sets for item in find_available_sets(self.inventories.get(row["bot"], [])))
            for row in succeeded
        )
        message = (f"Проверка завершена: успешно {len(succeeded)} из {len(results)}; "
                   f"ошибок {len(failed)}. Предметов: {total_items}. "
                   f"Полных наборов отдельно по ботам: {total_sets}. Обмены не отправлялись.")
        if failed:
            message += " Не удалось: " + ", ".join(row["bot"] for row in failed[:12])
        self.summary.setText(message)
        for button in (self.fetch_selected_button, self.fetch_button,
                       self.select_all_bots_button, self.clear_bots_button, self.refresh_button):
            button.setEnabled(True)
        self._batch_worker = None
        self._batch_thread = None

    def _render_instance_choices(self):
        self.instance_list.blockSignals(True)
        self.instance_list.clear()
        selected_cards = self.item_grid.selectedItems()
        available: list[tuple[str, str]] = []
        for card_item in selected_cards:
            row_data = card_item.data(Qt.ItemDataRole.UserRole + 2)
            if row_data is not None:
                available.extend(expand_inventory_instances(row_data))
        self.quantity_input.setRange(0, len(available))
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

    def _apply_quantity_selection(self):
        available = []
        for card_item in self.item_grid.selectedItems():
            row_data = card_item.data(Qt.ItemDataRole.UserRole + 2)
            if row_data is not None:
                available.extend(expand_inventory_instances(row_data))
        available = [
            (str(bot), str(asset)) for bot, asset in available
            if (str(bot), str(asset)) not in self.reservation_rows
        ]
        requested = self.quantity_input.value()
        if requested <= 0:
            self.instance_summary.setText("Укажи количество больше нуля.")
            return
        if requested > len(available):
            self.instance_summary.setText(
                f"Доступно только {len(available)} незарезервированных экземпляров."
            )
            return
        self._selected_asset_keys = set(available[:requested])
        self._render_instance_choices()

    def _clear_item_selection(self):
        self.item_grid.clearSelection()
        self._selected_asset_keys.clear()
        self._render_instance_choices()

    def _clear_asset_selection(self):
        self._selected_asset_keys.clear()
        self._render_instance_choices()

    def _update_instance_summary(self):
        counts = {}
        for bot, asset in self._selected_asset_keys:
            counts[bot] = counts.get(bot, 0) + 1
        distribution = " · ".join(f"{bot}: {count}" for bot, count in sorted(counts.items()))
        detail = f" Распределение: {distribution}." if distribution else ""
        self.instance_summary.setText(
            f"Выбрано экземпляров: {len(self._selected_asset_keys)}.{detail} "
            "Владельцы и asset ID сохраняются отдельно."
        )

    def _toggle_view(self, *_):
        show_table = self.view_mode.currentData() == "table"
        self.table.setVisible(show_table)
        self.item_grid.setVisible(not show_table)

    @staticmethod
    def _steam_image_candidates(url: str) -> list[str]:
        parsed = QUrl(url)
        path = parsed.path()
        if not path:
            return []
        hosts = ("community.cloudflare.steamstatic.com", "community.akamai.steamstatic.com",
                 "steamcommunity-a.akamaihd.net", "community.steamstatic.com")
        return [candidate for host in hosts
                if (candidate := QUrl("https://" + host + path).toString()) != url]

    def _request_item_image(self, url: str, origin: str | None = None):
        if not url or url in self._image_requests or url in self._image_cache:
            return
        origin = origin or url
        attempted = self._image_attempted.setdefault(origin, set())
        if url in attempted:
            return
        attempted.add(url)
        self._image_request_origin[url] = origin
        self._image_requests.add(url)
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"User-Agent", b"Mozilla/5.0 GameDelivery/1.0")
        request.setTransferTimeout(15000)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        self._image_manager.get(request)

    def _image_loaded(self, reply):
        url = reply.request().url().toString()
        origin = self._image_request_origin.pop(url, url)
        self._image_requests.discard(url)
        pixmap = QPixmap()
        if reply.error() == QNetworkReply.NetworkError.NoError:
            pixmap.loadFromData(bytes(reply.readAll()))
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error_text = reply.errorString()
        reply.deleteLater()
        if pixmap.isNull():
            for fallback in self._steam_image_candidates(origin):
                if fallback not in self._image_attempted.setdefault(origin, set()):
                    self._request_item_image(fallback, origin)
                    return
            self._log_image_failure(origin, status, error_text)
            return
        icon = QIcon(pixmap.scaled(QSize(96, 96), Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation))
        self._image_cache[origin] = icon
        self._image_cache[url] = icon
        for index in range(self.item_grid.count()):
            item = self.item_grid.item(index)
            if item.data(Qt.ItemDataRole.UserRole + 1) == origin:
                item.setIcon(icon)

    @staticmethod
    def _log_image_failure(url: str, status, error_text: str):
        try:
            from datetime import datetime, timezone
            import os
            log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GameDelivery" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "image-download.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now(timezone.utc).isoformat()} | HTTP={status} | {error_text} | {url}\\n")
        except OSError:
            pass

    def _select_all_visible_items(self):
        """Select every non-reserved physical asset represented by visible tiles."""
        candidates = set()
        for index in range(self.item_grid.count()):
            tile = self.item_grid.item(index)
            if tile.isHidden():
                continue
            row_data = tile.data(Qt.ItemDataRole.UserRole + 2)
            if row_data is None:
                continue
            for bot_name, asset_id in expand_inventory_instances(row_data):
                key = (str(bot_name), str(asset_id))
                if key not in self.reservation_rows:
                    candidates.add(key)
        self._selected_asset_keys.update(candidates)
        self._render_instance_choices()
        self.instance_summary.setText(
            f"Добавлено доступных экземпляров: {len(candidates)}. "
            f"Всего выбрано: {len(self._selected_asset_keys)}. Резервы исключены."
        )

    def _select_complete_sets_for_bots(self):
        """Select N complete normal trading-card sets independently for each selected bot."""
        requested = self.sets_per_bot_spin.value()
        selected_bots = self._checked_bot_names()
        if not selected_bots:
            self.instance_summary.setText("Сначала выбери одного или нескольких ботов в списке снимков.")
            return

        chosen = set()
        problems = []
        for bot_name in selected_bots:
            available = find_available_sets(self.inventories.get(bot_name, []))
            total_sets = sum(item.available_sets for item in available)
            if total_sets < requested:
                problems.append(f"{bot_name}: доступно {total_sets}")
                continue

            remaining = requested
            bot_chosen = set()
            for game_set in available:
                take = min(game_set.available_sets, remaining)
                if take <= 0:
                    continue
                for asset_id in game_set.asset_ids[:take * game_set.cards_per_set]:
                    key = (bot_name, str(asset_id))
                    if key in self.reservation_rows:
                        problems.append(f"{bot_name}: выбранный набор содержит зарезервированную карточку")
                    bot_chosen.add(key)
                remaining -= take
                if remaining <= 0:
                    break
            chosen.update(bot_chosen)

        # Fail atomically: never apply a partial set selection.
        if problems:
            self.instance_summary.setText(
                f"Ничего не выбрано: не удалось собрать по {requested} полных наборов "
                "для каждого выбранного бота. " + "; ".join(problems[:8])
            )
            return

        self._selected_asset_keys.update(chosen)
        self._render_instance_choices()
        self.instance_summary.setText(
            f"Добавлены полные наборы: {requested} на каждого из {len(selected_bots)} ботов "
            f"({len(chosen)} экземпляров). Наборы между ботами не смешивались."
        )

    def _load_reservations(self):
        import sqlite3
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT bot_name, asset_id, order_id FROM steam_asset_reservations "
                "ORDER BY bot_name, asset_id"
            ).fetchall()
        return {(bot, asset): order for bot, asset, order in rows}

    def _filter_item_tiles(self, *_):
        query = self.search_input.text().strip().casefold()
        kind = self.item_filter.currentData()
        for index in range(self.item_grid.count()):
            item = self.item_grid.item(index)
            row = item.data(Qt.ItemDataRole.UserRole + 2)
            searchable = f"{row.game_name} {row.card_name}".casefold()
            matches = (kind == "all"
                       or (kind == "cards" and row.is_trading_card and not row.is_foil)
                       or (kind == "foil" and row.is_foil)
                       or (kind == "items" and not row.is_trading_card)
                       or (kind == "tradable" and row.is_tradable is True)
                       or (kind == "nontradable" and row.is_tradable is False))
            item.setHidden(not (matches and query in searchable))

    def render_inventory(self, *_):
        if not hasattr(self, "inventories"):
            return
        selected = self._checked_bot_names()
        view = build_inventory_view(
            self.inventories, selected, mode=self.mode.currentData()
        )
        rows = filter_inventory_rows(
            view.rows, self.search_input.text(), item_filter=self.item_filter.currentData()
        )
        self.table.setRowCount(0)
        self.item_grid.clear()
        for row_data in rows:
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
        self._filter_item_tiles()
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
                f"Выбрано ботов: {len(selected)} · показано: {len(rows)} из {len(view.rows)} · "
                + " | ".join(updated)
            )
