from __future__ import annotations

import os
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, QSize, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QInputDialog, QListWidget, QListWidgetItem, QPushButton, QScrollArea,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.bot_selector import (
    BotSelectionModel, checked_bot_names, configured_bot_names,
    connect_account_search, populate_bot_list,
)
from app.game_inventory_cards import CS2InventoryGroupCard, GameItemCard, GameItemImageLoader
from app.inventory_panel import InventoryPanel
from games.catalog import GAME_CATALOG
from games.inventory import (
    GameInventoryStore, InventoryItem, parse_game_inventory,
    refresh_game_inventory, filter_game_inventory_items,
)
from games.cs2.inventory_view import (
    CS2InventoryInstance, adapt_cs2_inventory_response, build_cs2_inventory_projection,
)
from games.cs2.price_store import PriceCacheStore
from games.cs2.pricing import (
    PRICE_THRESHOLD_USD, PriceState, cached_price_from_openskin,
    calculate_valuation, requestable_positions, unsupported_positions,
)
from games.inventory_display import build_inventory_groups, count_physical_metrics
from app.inventory_display import (
    InventoryMetricsLabel, metric_table_cells, thumbnail_table_item,
    update_table_thumbnails,
)
from integrations.asf.client import ASFClient, ASFError, TEST_ASF_IPC_URL_ENV
from integrations.frankfurter.client import FrankfurterClient, FrankfurterError
from integrations.openskin.client import OpenSkinClient, OpenSkinError
from integrations.openskin.proxy import OpenSkinProxySettingsStore


class _BotListRefreshSignals(QObject):
    finished = Signal(list, str)


class _BotListRefreshTask(QRunnable):
    """Read only the bot names currently loaded in the explicitly selected ASF."""

    def __init__(self, client):
        super().__init__()
        self.client = client
        self.signals = _BotListRefreshSignals()

    def run(self):
        try:
            self.signals.finished.emit(sorted(self.client.get_bots(), key=str.casefold), "")
        except Exception as exc:
            self.signals.finished.emit([], _safe_test_asf_error(exc))


class _PriceRefreshSignals(QObject):
    finished = Signal(str)


class _PriceRefreshTask(QRunnable):
    """Serial network refresh on Qt's worker pool; no UI objects are touched."""

    def __init__(self, database_path, positions):
        super().__init__()
        self.database_path = str(database_path)
        self.positions = tuple(positions)
        self.signals = _PriceRefreshSignals()

    def run(self):
        messages = []
        try:
            store = PriceCacheStore(self.database_path)
            due = store.due_positions(self.positions)
            if due:
                try:
                    proxy = OpenSkinProxySettingsStore().load_enabled_proxy()
                    prices = OpenSkinClient(proxy=proxy).fetch_prices(due)
                    for price in prices:
                        store.save_quote(cached_price_from_openskin(price))
                except OpenSkinError as exc:
                    for position in due:
                        store.mark_error(
                            position, exc.http_status,
                            retry_after_seconds=exc.retry_after_seconds,
                        )
                    messages.append(
                        f"OpenSkin HTTP {exc.http_status}" if exc.http_status else "OpenSkin недоступен"
                    )
            cached_quotes = (store.get(position) for position in self.positions)
            needs_conversion = any(
                quote is not None and quote.currency == "USD"
                and quote.steam_ask is not None
                and quote.steam_ask > PRICE_THRESHOLD_USD
                for quote in cached_quotes
            )
            if needs_conversion and store.fx_rate_due():
                try:
                    rate = FrankfurterClient().fetch_usd_rub()
                    store.save_fx_rate(rate.rate, rate.source_date, now=rate.fetched_at)
                except FrankfurterError as exc:
                    store.mark_fx_error(exc.http_status)
                    messages.append(
                        f"Frankfurter HTTP {exc.http_status}" if exc.http_status
                        else "Курс USD/RUB недоступен"
                    )
            rate = store.get_fx_rate()
            if rate is not None:
                for position in self.positions:
                    quote = store.get(position)
                    if (quote is None or quote.currency != "USD" or quote.steam_ask is None
                            or quote.steam_ask <= PRICE_THRESHOLD_USD):
                        continue
                    rub = (quote.steam_ask * rate.rate).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    store.save_conversion(
                        position, rub, rate.rate, rate.source_date, rate.fetched_at
                    )
        except Exception:
            messages.append("Не удалось обновить кэш цен")
        self.signals.finished.emit(" · ".join(messages))


def _safe_test_asf_error(exc: Exception) -> str:
    """Expose only fixed ASFClient diagnostics in test mode, never arbitrary payloads."""
    message = str(exc)
    if isinstance(exc, ASFError):
        fixed_messages = {
            "ASF IPC отклонил авторизацию; проверь IPCPassword",
            "ASF IPC временно заблокировал запросы или запретил доступ",
            "ASF IPC недоступен на локальном компьютере",
            "ASF вернул ответ не в формате JSON",
        }
        if message in fixed_messages:
            return message
        prefix = "ASF IPC вернул HTTP "
        code = message.removeprefix(prefix)
        if message.startswith(prefix) and len(code) == 3 and code.isdecimal():
            return message
    return "ошибка тестового ASF IPC/обработки; подробности скрыты"


class GameInventoryPanel(QWidget):
    """Read-only visual inventory for game items across ASF bot accounts."""

    trade_url_requested = Signal(str)

    def __init__(self, parent=None, bot_selection_model: BotSelectionModel | None = None):
        super().__init__(parent)
        self.test_mode = bool(os.environ.get(TEST_ASF_IPC_URL_ENV, "").strip())
        self.bot_selection_model = bot_selection_model or BotSelectionModel(self)
        if not self.test_mode and not self.bot_selection_model.account_names:
            self.bot_selection_model.set_accounts(configured_bot_names())
        self.database_path = Path.home() / ".gamedelivery" / "gamedelivery.sqlite3"
        self.store = GameInventoryStore(self.database_path)
        self.items = []
        self.cs2_instances_by_bot: dict[str, tuple[CS2InventoryInstance, ...]] = {}
        self.cs2_all_instances: tuple[CS2InventoryInstance, ...] = ()
        self.cs2_snapshot_meta: dict[str, int] = {}
        self.cs2_all_valuation = None
        self.cs2_projection = None
        self.price_cache_store = None
        self.price_quotes = {}
        self.usd_rub = None
        self.fx_rate_source_date = None
        self.fx_rate_fetched_at = None
        self.fx_rate_is_stale = False
        self.cs2_valuation = None
        self._price_refresh_running = False
        self._price_worker = None
        self._price_refresh_message = ""
        self._bot_list_refresh_running = False
        self._bot_list_refresh_task = None
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
        self.bot_list.setMaximumHeight(170)
        self.account_search = QLineEdit()
        self.account_search.setPlaceholderText("Поиск аккаунта…")
        connect_account_search(self.account_search, self.bot_list)
        account_search_column = QVBoxLayout()
        account_search_column.addWidget(self.account_search)
        account_search_column.addWidget(self.bot_list)
        self.bot_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        controls.addLayout(account_search_column, 2)
        account_actions = QVBoxLayout()
        self.refresh_bot_list_button = QPushButton("Обновить список ботов")
        account_actions.addWidget(self.refresh_bot_list_button)
        self.select_all_bots_button = QPushButton("Выбрать всех")
        self.clear_bots_button = QPushButton("Снять выбор")
        account_actions.addWidget(self.select_all_bots_button)
        account_actions.addWidget(self.clear_bots_button)
        controls.addLayout(account_actions)

        self.password_label = QLabel("IPC-пароль")
        controls.addWidget(self.password_label)
        test_endpoint = os.environ.get(TEST_ASF_IPC_URL_ENV, "").strip()
        self.password = QLineEdit(
            "" if test_endpoint else InventoryPanel._load_saved_ipc_password()
        )
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText(
            "Пароль будет запрошен отдельно" if test_endpoint else "Если задан в ASF"
        )
        self.password.setEnabled(not test_endpoint)
        self.password_label.setVisible(not self.test_mode)
        self.password.setVisible(not self.test_mode)
        controls.addWidget(self.password, 1)

        self.load_button = QPushButton("Обновить выбранные аккаунты")
        self.load_button.clicked.connect(self.fetch_inventory)
        controls.addWidget(self.load_button)
        self.refresh_button = QPushButton("Обновить список")
        self.refresh_button.clicked.connect(self.load_saved)
        controls.addWidget(self.refresh_button)
        self.price_refresh_button = QPushButton("Обновить цены")
        self.price_refresh_button.clicked.connect(
            lambda: self._start_price_refresh(manual=True)
        )
        controls.addWidget(self.price_refresh_button)
        self.price_missing_button = QPushButton("Запросить цены без снимка")
        self.price_missing_button.setToolTip(
            "Запросить только рыночные позиции, для которых ещё нет записи в кэше цен"
        )
        self.price_missing_button.clicked.connect(self._start_missing_price_refresh)
        controls.addWidget(self.price_missing_button)
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
        self.grouping_mode = QComboBox()
        self.grouping_mode.addItem("Общий вид", "shared")
        self.grouping_mode.addItem("Раздельно по ботам", "separate")
        filters.addWidget(self.grouping_mode, 1)
        self.view_mode = QComboBox()
        self.view_mode.addItem("Карточки предметов", "cards")
        self.view_mode.addItem("Таблица подробностей", "table")
        filters.addWidget(self.view_mode, 1)
        layout.addLayout(filters)

        self.status = QLabel("Выбери игру. Для общего списка сначала загрузи снимки нужных аккаунтов.")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        self.price_summary = QLabel("Цены CS2 появятся после загрузки снимка инвентаря.")
        self.price_summary.setObjectName("inventorySummary")
        self.price_summary.setWordWrap(True)
        self.price_summary.setMinimumHeight(56)
        self.price_summary.setStyleSheet(
            "QLabel#inventorySummary { color: #f3f6fb; background: #202a38; "
            "border: 1px solid #43536a; border-radius: 10px; "
            "padding: 8px 12px; font-size: 15px; font-weight: 700; }"
        )
        layout.addWidget(self.price_summary)

        self.table = QTableWidget(0, 12)
        self.table.setHorizontalHeaderLabels([
            "Игра", "Предмет", "Тип / качество", "Можно продать", "Сумма ASF amount",
            "Всего", "Непередаваемые", "Аккаунтов", "Передаваемые",
            "Статус неизвестен", "Ограничение", "Владельцы / asset ID",
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setIconSize(QSize(48, 48))
        self.table.hide()
        layout.addWidget(self.table, 1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.cards_host = QWidget()
        self.cards_layout = QGridLayout(self.cards_host)
        self.cards_layout.setContentsMargins(8, 8, 8, 8)
        self.cards_layout.setSpacing(10)
        self.scroll.setWidget(self.cards_host)
        layout.addWidget(self.scroll, 1)

        self.image_loader = GameItemImageLoader(self, self._image_loaded)
        self._price_refresh_timer = QTimer(self)
        self._price_refresh_timer.setInterval(10 * 60 * 1000)
        self._price_refresh_timer.timeout.connect(self._start_price_refresh)
        self._price_refresh_timer.start()
        self.game_combo.currentIndexChanged.connect(self._game_changed)
        self.grouping_mode.currentIndexChanged.connect(self.render)
        self.view_mode.currentIndexChanged.connect(self.render)
        self.select_all_bots_button.clicked.connect(self.select_all_bots)
        self.clear_bots_button.clicked.connect(self.clear_bot_selection)
        self.refresh_bot_list_button.clicked.connect(self.bot_selection_model.refresh_requested.emit)
        self._populate_accounts()
        self.bot_list.itemChanged.connect(self._bot_list_changed)
        self.bot_selection_model.accounts_changed.connect(self._render_bot_selection)
        self.bot_selection_model.selection_changed.connect(self._bot_selection_updated)
        is_cs2 = self.game_combo.currentData() == "cs2"
        self.price_summary.setVisible(is_cs2)
        self.price_refresh_button.setVisible(is_cs2)
        self.price_missing_button.setVisible(is_cs2)
        self.grouping_mode.setVisible(not is_cs2)
        self.load_saved()

    def _game_changed(self, *_):
        is_cs2 = self.game_combo.currentData() == "cs2"
        self.item_filter.setVisible(is_cs2)
        self.price_summary.setVisible(is_cs2)
        self.price_refresh_button.setVisible(is_cs2)
        self.price_missing_button.setVisible(is_cs2)
        self.grouping_mode.setVisible(not is_cs2)
        separate_index = self.grouping_mode.findData("separate")
        if separate_index >= 0:
            self.grouping_mode.setItemText(
                separate_index, "Раздельно по Asset" if is_cs2 else "Раздельно по ботам"
            )
        if not is_cs2:
            self.item_filter.setCurrentIndex(0)
        self.load_saved()

    def _populate_accounts(self):
        if not self.test_mode:
            self.bot_selection_model.set_accounts(
                {*self.bot_selection_model.account_names, *configured_bot_names()}
            )
        self._render_bot_selection()

    def _selected_bot_names(self) -> list[str]:
        return self.bot_selection_model.selected_names

    def _render_bot_selection(self, *_):
        populate_bot_list(
            self.bot_list, self.bot_selection_model.account_names,
            self.bot_selection_model.selected_names,
        )

    def _bot_list_changed(self, *_):
        self.bot_selection_model.set_selected(checked_bot_names(self.bot_list))

    def _bot_selection_updated(self, *_):
        self._render_bot_selection()
        if hasattr(self, "game_combo"):
            self.load_saved()

    def select_all_bots(self):
        self.bot_selection_model.select_all()
        self.load_saved()

    def clear_bot_selection(self):
        self.bot_selection_model.clear_selection()
        self.load_saved()

    def refresh_bot_list(self):
        if self._bot_list_refresh_running:
            return
        if not self.test_mode:
            names = set(configured_bot_names())
            try:
                game_id = str(self.game_combo.currentData() or "")
                names.update(item.bot_name for item in self.store.load_all(game_id))
            except Exception:
                pass
            self.bot_selection_model.set_accounts(names)
            self._render_bot_selection()
            self.status.setText(f"Список ботов обновлён: {len(names)} аккаунтов ASF.")
            return
        client = self._create_asf_client()
        if client is None:
            return
        self._bot_list_refresh_running = True
        self.refresh_bot_list_button.setEnabled(False)
        task = _BotListRefreshTask(client)
        task.signals.finished.connect(self._test_bot_list_refreshed)
        self._bot_list_refresh_task = task
        QThreadPool.globalInstance().start(task)

    def _test_bot_list_refreshed(self, names: list[str], error: str):
        self._bot_list_refresh_running = False
        self._bot_list_refresh_task = None
        self.refresh_bot_list_button.setEnabled(True)
        if error:
            self.status.setText(f"Не удалось прочитать список из тестового ASF: {error}")
            return
        self.bot_selection_model.set_accounts(names)
        self._render_bot_selection()
        self.status.setText(f"Список тестового ASF обновлён: {len(names)} аккаунтов.")

    def _create_asf_client(self):
        endpoint = os.environ.get(TEST_ASF_IPC_URL_ENV, "").strip()
        if not endpoint:
            return ASFClient(ipc_password=self.password.text().strip() or None, timeout=45.0)

        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
            valid_endpoint = (
                parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and port is not None and 0 < port <= 65535 and port != 1242
                and parsed.username is None and parsed.password is None
                and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
            )
        except ValueError:
            valid_endpoint = False
        if not valid_endpoint:
            self.status.setText(
                "Тестовый ASF endpoint отклонён: укажи HTTP loopback-порт, отличный от 1242."
            )
            return None

        password, accepted = QInputDialog.getText(
            self, "Тестовый ASF IPC", "IPCPassword тестового ASF:",
            QLineEdit.EchoMode.Password,
        )
        if not accepted or not password:
            self.status.setText("Ввод пароля отменён; ASF-запрос не выполнялся.")
            return None
        return ASFClient(
            base_url=endpoint, ipc_password=password, timeout=45.0,
            allow_alternate_loopback_port=True,
        )

    def fetch_inventory(self):
        game_id = str(self.game_combo.currentData() or "")
        bot_names = self._selected_bot_names()
        if getattr(self, "test_mode", bool(os.environ.get(TEST_ASF_IPC_URL_ENV, "").strip())) \
                and game_id != "cs2":
            self.status.setText("В режиме изолированного ASF разрешена только проверка инвентаря CS2.")
            return
        if not game_id or not bot_names:
            self.status.setText(
                "Укажи имя бота изолированного ASF."
                if getattr(self, "test_mode", False)
                else "Выбери аккаунты для загрузки из ASF."
            )
            return
        self.load_button.setEnabled(False)
        succeeded, failed = 0, []
        try:
            client = self._create_asf_client()
            if client is None:
                return
            for index, bot_name in enumerate(bot_names, 1):
                self.status.setText(f"Загрузка {game_id}: {index}/{len(bot_names)} · {bot_name}…")
                try:
                    if game_id == "cs2":
                        payload = client.get_bot_inventory(
                            bot_name, app_id=730, context_id=2, language="english"
                        )
                        items = parse_game_inventory(payload, "cs2", bot_name)
                        self.store.replace("cs2", bot_name, items)
                        succeeded += 1
                        try:
                            adapted = adapt_cs2_inventory_response(
                                payload, bot_name, appid=730, context_id=2
                            )
                            parsed_by_key = {
                                (item.game_id, item.bot_name, item.asset_id): item
                                for item in items
                            }
                            self.cs2_instances_by_bot[bot_name] = tuple(
                                replace(
                                    instance,
                                    icon_url=(parsed_by_key.get(instance.physical_key).icon_url
                                              if parsed_by_key.get(instance.physical_key) else instance.icon_url),
                                    icon_url_large=(parsed_by_key.get(instance.physical_key).icon_url_large
                                                    if parsed_by_key.get(instance.physical_key) else instance.icon_url_large),
                                )
                                for instance in adapted
                            )
                        except Exception as exc:
                            self.cs2_instances_by_bot.pop(bot_name, None)
                            detail = ("CS2-проекция недоступна; подробности скрыты"
                                      if os.environ.get(TEST_ASF_IPC_URL_ENV, "").strip()
                                      else f"CS2-проекция недоступна: {exc}")
                            failed.append(f"{bot_name}: снимок обновлён, {detail}")
                        continue
                    refresh_game_inventory(client, self.store, game_id, bot_name)
                    succeeded += 1
                except Exception as exc:
                    detail = (_safe_test_asf_error(exc)
                              if os.environ.get(TEST_ASF_IPC_URL_ENV, "").strip()
                              else str(exc))
                    failed.append(f"{bot_name}: {detail}")
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
            self.cs2_all_instances = ()
            self.cs2_snapshot_meta = {}
            self.cs2_all_valuation = None
            self.cs2_projection = None
            self.render()
            return
        try:
            all_items = self.store.load_all(game_id)
            selected = set(self._selected_bot_names())
            self.items = [item for item in all_items if item.bot_name in selected]
            self.cs2_projection = None
            if game_id == "cs2":
                instances_by_key = {
                    instance.physical_key: instance
                    for values in self.cs2_instances_by_bot.values()
                    for instance in values
                }
                all_instances = [
                    instances_by_key.get(
                        (item.game_id, item.bot_name, item.asset_id),
                        CS2InventoryInstance(
                            item.game_id, 730, 2, item.bot_name, item.asset_id,
                            item.class_id, item.instance_id, None, item.name,
                            item.item_type, item.market_hash_name, item.is_tradable,
                            item.is_marketable, item.icon_url, item.icon_url_large,
                            item.tags, quality=item.quality, exterior=item.exterior,
                            rarity=item.rarity,
                        ),
                    )
                    for item in all_items
                ]
                self.cs2_all_instances = tuple(all_instances)
                self.cs2_snapshot_meta = self.store.load_snapshot_meta("cs2")
                selected_instances = [
                    item for item in all_instances if item.bot_name in selected
                ]
                self.cs2_projection = build_cs2_inventory_projection(
                    selected_instances, list(selected)
                )
                try:
                    self._load_price_cache()
                except Exception:
                    self.price_quotes = {}
                    self.usd_rub = None
                    self.fx_rate_is_stale = False
                    self.fx_rate_source_date = None
                    self.fx_rate_fetched_at = None
                    self.cs2_valuation = None
                    self._price_refresh_message = "Кэш цен недоступен; оценка в ₽ не рассчитана."
            else:
                self.cs2_valuation = None
            accounts = len({item.bot_name for item in self.items})
            self.status.setText(
                f"Локальные снимки: {game_id} · {accounts} аккаунтов · {len(self.items)} предметов. "
                "Общий список объединяет только выбранные аккаунты; актуальность нужно проверять."
            )
            self._refresh_filter_values()
            self.render()
            if game_id == "cs2" and self.cs2_all_instances:
                self._start_price_refresh()
        except Exception as exc:
            self.items = []
            self.cs2_all_instances = ()
            self.cs2_snapshot_meta = {}
            self.cs2_all_valuation = None
            self.cs2_valuation = None
            self.cs2_projection = None
            self.render()
            self.status.setText(f"Не удалось прочитать снимок: {exc}")

    def _load_price_cache(self):
        instances = tuple(getattr(self, "cs2_all_instances", ()))
        self.price_quotes = {}
        self.usd_rub = None
        self.fx_rate_source_date = None
        self.fx_rate_fetched_at = None
        self.fx_rate_is_stale = False
        if self.price_cache_store is None:
            self.price_cache_store = PriceCacheStore(self.database_path)
        positions = requestable_positions(instances)
        unsupported = unsupported_positions(instances)
        for position in unsupported:
            self.price_cache_store.mark_unsupported(position)
        self.price_quotes = {
            position: quote
            for position in positions
            if (quote := self.price_cache_store.get(position)) is not None
        }
        rate = self.price_cache_store.get_fx_rate()
        if rate is not None:
            self.usd_rub = rate.rate
            self.fx_rate_source_date = rate.source_date
            self.fx_rate_fetched_at = rate.fetched_at
            self.fx_rate_is_stale = rate.is_stale

        try:
            all_bot_names = set(self.bot_selection_model.account_names)
        except (AttributeError, TypeError):
            all_bot_names = set()
        all_bot_names.update(item.bot_name for item in instances)
        selected_bot_names = set(self._selected_bot_names())
        self.cs2_all_valuation = calculate_valuation(
            instances, self.price_quotes, self.usd_rub,
            selected_bots=all_bot_names, rate_is_stale=self.fx_rate_is_stale,
        )
        self.cs2_valuation = calculate_valuation(
            instances, self.price_quotes, self.usd_rub,
            selected_bots=selected_bot_names, rate_is_stale=self.fx_rate_is_stale,
        )
        self._update_price_summary()

    @staticmethod
    def _valuation_total_text(valuation):
        if valuation is None:
            return "ожидает расчёта"
        total = valuation.selected_total_rub
        if total is None:
            if valuation.unknown_price_instances or valuation.awaiting_rate_instances:
                return "цена пока неизвестна"
            if valuation.below_threshold_instances:
                return "нет предметов выше $0.05"
            return "нет данных"
        if total == 0 and valuation.priced_instances == 0 and valuation.awaiting_rate_instances:
            return "ожидает курс USD/RUB"
        suffix = " · частичная оценка" if valuation.is_partial else ""
        return f"₽{total:.2f}{suffix}"

    @staticmethod
    def _russian_count(value: int, forms: tuple[str, str, str]) -> str:
        value = abs(int(value))
        tail = value % 100
        if 11 <= tail <= 14:
            form = forms[2]
        elif value % 10 == 1:
            form = forms[0]
        elif 2 <= value % 10 <= 4:
            form = forms[1]
        else:
            form = forms[2]
        return f"{value} {form}"

    def _update_price_summary(self, message: str = ""):
        if not hasattr(self, "price_summary"):
            return
        selected_names = set(self._selected_bot_names())
        bot_model = getattr(self, "bot_selection_model", None)
        all_names = set(getattr(bot_model, "account_names", ()))
        all_instances = tuple(getattr(self, "cs2_all_instances", ()))
        all_names.update(item.bot_name for item in all_instances)
        selected_instances = [
            item for item in all_instances if item.bot_name in selected_names
        ]
        if getattr(self, "cs2_valuation", None) is None:
            self.cs2_valuation = calculate_valuation(
                all_instances, getattr(self, "price_quotes", {}),
                getattr(self, "usd_rub", None), selected_bots=selected_names,
                rate_is_stale=getattr(self, "fx_rate_is_stale", False),
            )
        if getattr(self, "cs2_all_valuation", None) is None:
            self.cs2_all_valuation = calculate_valuation(
                all_instances, getattr(self, "price_quotes", {}),
                getattr(self, "usd_rub", None), selected_bots=all_names,
                rate_is_stale=getattr(self, "fx_rate_is_stale", False),
            )
        selected_total = self._valuation_total_text(self.cs2_valuation)
        all_total = self._valuation_total_text(self.cs2_all_valuation)
        selected_line = (
            f"ВЫБРАНО: {len(selected_names)} из {self._russian_count(len(all_names), ('бота', 'ботов', 'ботов'))} · "
            f"{self._russian_count(len(selected_instances), ('предмет', 'предмета', 'предметов'))} · {selected_total}"
        )
        all_line = (
            f"ВСЕ БОТЫ: {self._russian_count(len(all_names), ('бот', 'бота', 'ботов'))} · "
            f"{self._russian_count(len(all_instances), ('предмет', 'предмета', 'предметов'))} · {all_total}"
        )
        self.price_summary.setText(f"{selected_line}\n{all_line}")

    def _start_missing_price_refresh(self):
        if (self._price_refresh_running
                or self.game_combo.currentData() != "cs2"
                or not getattr(self, "cs2_all_instances", ())):
            return
        if self.price_cache_store is None:
            try:
                self.price_cache_store = PriceCacheStore(self.database_path)
            except Exception:
                self._price_refresh_message = "Кэш цен недоступен; сетевой запрос не выполнен."
                self.status.setText(self._price_refresh_message)
                return
        positions = requestable_positions(self.cs2_all_instances)
        missing = tuple(
            position for position in positions
            if self.price_cache_store.get(position) is None
        )
        if not missing:
            self._price_refresh_message = "Нет доступных позиций без сохранённого снимка цены."
            self.status.setText(self._price_refresh_message)
            return
        self._start_price_refresh(manual=True, positions=missing)

    def _start_price_refresh(self, manual: bool = False, positions=None):
        if (self._price_refresh_running
                or self.game_combo.currentData() != "cs2"
                or not getattr(self, "cs2_all_instances", ())):
            return
        if self.price_cache_store is None:
            try:
                self.price_cache_store = PriceCacheStore(self.database_path)
            except Exception:
                self.price_summary.setText("Кэш цен недоступен; сетевой запрос не выполнен.")
                return
        positions = tuple(positions) if positions is not None else requestable_positions(self.cs2_all_instances)
        due = self.price_cache_store.due_positions(positions)
        eligible_cached = any(
            (quote := self.price_cache_store.get(position)) is not None
            and quote.currency == "USD" and quote.steam_ask is not None
            and quote.steam_ask > PRICE_THRESHOLD_USD
            for position in positions
        )
        rate_due = eligible_cached and self.price_cache_store.fx_rate_due()
        if not due and not rate_due:
            if manual:
                self._price_refresh_message = (
                    "Кэш актуален; повторный запрос ограничен частотой обновления Steam Ask."
                )
                self.status.setText(self._price_refresh_message)
            return
        self._price_refresh_running = True
        self.price_refresh_button.setEnabled(False)
        if hasattr(self, "price_missing_button"):
            self.price_missing_button.setEnabled(False)
        worker = _PriceRefreshTask(self.database_path, positions)
        worker.signals.finished.connect(self._price_refresh_finished)
        self._price_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _price_refresh_finished(self, message: str):
        self._price_refresh_running = False
        self._price_worker = None
        self.price_refresh_button.setEnabled(True)
        if hasattr(self, "price_missing_button"):
            self.price_missing_button.setEnabled(True)
        self._price_refresh_message = message
        try:
            self._load_price_cache()
            self.render()
            self.status.setText(message or "Обновление цен завершено.")
        except Exception:
            self.price_summary.setText("Цены сохранены, но не удалось обновить представление.")

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
        if self.game_combo.currentData() == "cs2":
            self._render_cs2_groups()
            return

        query = self.search.text().strip().casefold()
        transfer = self.transfer_filter.currentData()
        rarity = self.rarity_filter.currentData()
        quality = self.quality_filter.currentData()
        groups = build_inventory_groups(
            self.items, mode=self.grouping_mode.currentData()
        )
        filtered = []
        for group in groups:
            item = group.items[0]
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
            haystack = " ".join(
                value for member in group.items for value in (
                    member.name, member.item_type, member.asset_id, member.quality,
                    member.rarity, member.exterior, member.market_hash_name,
                    member.bot_name, *member.tags,
                )
            ).casefold()
            if query and query not in haystack:
                continue
            filtered.append(group)

        if self.view_mode.currentData() == "table":
            self._render_game_table([
                (group.items[0], group.items, "Неизвестно", "Неизвестно")
                for group in filtered
            ])
        else:
            self._render_cards([
                (group.items[0], group.items, "game") for group in filtered
            ])
        self.status.setText(
            f"Показано групп: {len(filtered)} из {len(groups)} · "
            f"физических экземпляров: {sum(len(group.items) for group in filtered)}."
        )

    @staticmethod
    def _cs2_restriction_text(group):
        from games.cs2.inventory_view import RestrictionStatus, RestrictionType

        names = {
            RestrictionType.NONE: "Нет",
            RestrictionType.MARKET_PURCHASE_COOLDOWN: "Период после покупки на маркете",
            RestrictionType.TRADE_PROTECTION: "Защита обмена",
            RestrictionType.PERMANENTLY_NON_TRADABLE: "Постоянно непередаваемый",
            RestrictionType.UNKNOWN: "Неизвестно",
        }
        statuses = {
            RestrictionStatus.AVAILABLE: "Доступен",
            RestrictionStatus.RESTRICTED: "Ограничен",
            RestrictionStatus.UNKNOWN: "Неизвестно",
        }
        counts = {}
        for item in group.instances:
            label = f"{names[item.restriction_type]} · {statuses[item.restriction_status]}"
            counts[label] = counts.get(label, 0) + 1
        return "; ".join(f"{label}: {count}" for label, count in sorted(counts.items()))

    @classmethod
    def _cs2_marketable_text(cls, group):
        counts = {True: 0, False: 0, None: 0}
        for item in group.instances:
            counts[item.is_marketable] += 1
        return " · ".join(
            f"{cls._marketable_text(value)}: {count}"
            for value, count in ((True, counts[True]), (False, counts[False]),
                                 (None, counts[None])) if count
        )

    @staticmethod
    def _cs2_restriction_details(group):
        from games.cs2.inventory_view import RestrictionStatus, RestrictionType

        type_names = {
            RestrictionType.NONE: "Нет",
            RestrictionType.MARKET_PURCHASE_COOLDOWN: "Период после покупки на маркете",
            RestrictionType.TRADE_PROTECTION: "Защита обмена",
            RestrictionType.PERMANENTLY_NON_TRADABLE: "Постоянно непередаваемый",
            RestrictionType.UNKNOWN: "Неизвестно",
        }
        status_names = {
            RestrictionStatus.AVAILABLE: "Доступен",
            RestrictionStatus.RESTRICTED: "Ограничен",
            RestrictionStatus.UNKNOWN: "Неизвестно",
        }
        rows = []
        for item in group.instances:
            tradable_after = (
                item.tradable_after.isoformat()
                if item.tradable_after is not None else "Неизвестно"
            )
            rows.append(
                f"{item.bot_name} · asset {item.asset_id}: "
                f"{type_names[item.restriction_type]} · {status_names[item.restriction_status]}; "
                f"Передаваемый={GameInventoryPanel._marketable_text(item.is_tradable)}, "
                f"marketable={GameInventoryPanel._marketable_text(item.is_marketable)}, "
                f"market_tradable_restriction={item.market_tradable_restriction if item.market_tradable_restriction is not None else 'Неизвестно'}, "
                f"market_marketable_restriction={item.market_marketable_restriction if item.market_marketable_restriction is not None else 'Неизвестно'}, "
                f"tradable_after={tradable_after}"
            )
        return "\n".join(rows)

    def _render_cs2_groups(self):
        query = self.search.text().strip().casefold()
        transfer = self.transfer_filter.currentData()
        rarity = self.rarity_filter.currentData()
        quality = self.quality_filter.currentData()
        projection = self.cs2_projection or build_cs2_inventory_projection(())
        selected_bots = sorted({item.bot_name for item in projection.instances}, key=str.casefold)
        self.cs2_valuation = calculate_valuation(
            projection.instances, getattr(self, "price_quotes", {}),
            getattr(self, "usd_rub", None), selected_bots=selected_bots,
            rate_is_stale=getattr(self, "fx_rate_is_stale", False),
        )
        self._update_price_summary()
        allowed_keys = {
            (item.game_id, item.bot_name, item.asset_id)
            for item in filter_game_inventory_items(
                self.items, containers_only=self.item_filter.currentData() == "containers"
            )
        }
        items_by_key = {
            (item.game_id, item.bot_name, item.asset_id): item for item in self.items
        }
        # CS2 always groups identical market items across selected bots.
        # The projection splits a market item only by transferability.
        source_groups = projection.groups

        filtered = []
        rarity_by_key = {}
        for group in source_groups:
            members = [item for item in group.instances if item.physical_key in allowed_keys]
            if transfer == "yes":
                members = [item for item in members if item.is_tradable is True]
            elif transfer == "no":
                members = [item for item in members if item.is_tradable is False]
            elif transfer == "unknown":
                members = [item for item in members if item.is_tradable is None]
            if not members:
                continue
            sample = members[0]
            legacy_items = [
                items_by_key[item.physical_key] for item in members
                if item.physical_key in items_by_key
            ]
            group_rarities = {item.rarity for item in members if item.rarity}
            if not group_rarities:
                group_rarities = {item.rarity for item in legacy_items if item.rarity}
            group_rarity = next(iter(group_rarities)) if len(group_rarities) == 1 else ""
            rarity_by_key.update((item.physical_key, group_rarity) for item in members)
            if rarity and not (
                any(item.rarity == rarity for item in members)
                or any(item.rarity == rarity for item in legacy_items)
            ):
                continue
            if quality and not (
                any((item.exterior or item.quality) == quality for item in members)
                or any((item.exterior or item.quality) == quality for item in legacy_items)
            ):
                continue
            haystack = " ".join((
                sample.name, sample.item_type, sample.market_hash_name, *sample.tags,
                *(item.bot_name for item in members), *(item.asset_id for item in members),
            )).casefold()
            if query and query not in haystack:
                continue
            base_group = build_cs2_inventory_projection(members).groups[0]
            if self.view_mode.currentData() == "table":
                filtered.append(base_group)
            else:
                # Card view splits one market item only by transferability.
                for transfer_state in (True, False, None):
                    state_members = [
                        member for member in members
                        if member.is_tradable is transfer_state
                    ]
                    if state_members:
                        filtered.append(
                            build_cs2_inventory_projection(state_members).groups[0]
                        )

        self._cs2_rarity_by_physical_key = rarity_by_key

        if self.view_mode.currentData() == "table":
            rows = []
            for group in filtered:
                item = items_by_key.get(group.instances[0].physical_key)
                amount = (
                    str(group.total_amount) if group.total_amount is not None else
                    f"Неизвестно (известно: {group.known_amount_sum})"
                )
                rows.append((group, (item,) if item else (), amount,
                             self._cs2_restriction_text(group),
                             self._cs2_marketable_text(group),
                             self._cs2_restriction_details(group)))
            self._render_cs2_table(rows)
        else:
            self._render_cards([(group, group.instances, "cs2") for group in filtered])

        # The prominent summary above already owns bot/item counts and valuation.
        self.status.setText("")

    @staticmethod
    def _owners_text(instances):
        by_bot = {}
        for item in instances:
            by_bot.setdefault(item.bot_name, []).append(str(item.asset_id))
        return "\n".join(
            f"{bot}: " + ", ".join(assets)
            for bot, assets in sorted(by_bot.items(), key=lambda pair: pair[0].casefold())
        )

    @staticmethod
    def _marketable_text(value):
        return "Да" if value is True else "Нет" if value is False else "Неизвестно"

    def _render_game_table(self, rows):
        entries = []
        for sample, instances, amount, restriction in rows:
            entries.append((
                self.game_combo.currentText(), sample.name,
                " · ".join(value for value in (
                    sample.item_type, sample.exterior or sample.quality,
                    sample.rarity,
                ) if value) or "Неизвестно",
                self._marketable_text(sample.is_marketable), amount, restriction,
                instances, next((item.icon_url_large or item.icon_url
                                 for item in instances
                                 if item.icon_url_large or item.icon_url), ""),
                restriction,
            ))
        self._render_table_entries(entries)

    def _render_cs2_table(self, rows):
        entries = []
        for row_data in rows:
            group, legacy, amount, restriction, marketable, restriction_details = row_data[:6]
            item = legacy[0] if legacy else None
            first = group.instances[0]
            details = " · ".join(value for value in (
                first.item_type, item.exterior or item.quality if item else "",
                item.rarity if item else "",
            ) if value) or "Неизвестно"
            entries.append((
                "Counter-Strike 2", first.name or "Название неизвестно", details,
                marketable, amount, restriction,
                group.instances, next((item.icon_url_large or item.icon_url
                                       for item in group.instances
                                       if item.icon_url_large or item.icon_url), ""),
                restriction_details,
            ))
        self._render_table_entries(entries)

    def _render_table_entries(self, entries):
        self._clear_cards()
        self.scroll.hide()
        self.table.show()
        self.table.setRowCount(0)
        self.table.setColumnCount(12)
        self.table.setHorizontalHeaderLabels([
            "Игра", "Предмет", "Тип / качество", "Можно продать", "Сумма ASF amount",
            "Всего", "Непередаваемые", "Аккаунтов", "Передаваемые",
            "Статус неизвестен", "Ограничение", "Владельцы / asset ID",
        ])
        for values in entries:
            (game, name, details, marketable, amount, restriction,
             instances, image_url) = values[:8]
            restriction_details = values[8] if len(values) > 8 else restriction
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setRowHeight(row, 56)
            self.table.setItem(row, 0, QTableWidgetItem(game))
            cached = self.image_loader.cache.get(image_url)
            name_item = thumbnail_table_item(image_url, QIcon(cached) if cached else None)
            name_item.setText(name)
            name_item.setData(Qt.ItemDataRole.UserRole + 1, tuple(
                (item.game_id, item.bot_name, item.asset_id) for item in instances
            ))
            self.table.setItem(row, 1, name_item)
            self.table.setItem(row, 2, QTableWidgetItem(details))
            self.table.setItem(row, 3, QTableWidgetItem(marketable))
            self.table.setItem(row, 4, QTableWidgetItem(amount))
            metrics = count_physical_metrics(
                (item.bot_name, item.is_tradable) for item in instances
            )
            for column, item in enumerate(metric_table_cells(metrics), 5):
                self.table.setItem(row, column, item)
            restriction_item = QTableWidgetItem(restriction)
            restriction_item.setToolTip(restriction_details)
            self.table.setItem(row, 10, restriction_item)
            self.table.setItem(row, 11, QTableWidgetItem(self._owners_text(instances)))
            if image_url and cached is None:
                self.image_loader.request(image_url)
        self.table.resizeColumnsToContents()

    def _render_cards(self, entries):
        self._clear_cards()
        self.table.hide()
        self.scroll.show()
        columns = max(1, self.width() // 205)
        for index, (item, instances, kind) in enumerate(entries):
            if kind == "cs2":
                first = item.instances[0]
                rarity = getattr(self, "_cs2_rarity_by_physical_key", {}).get(
                    first.physical_key, ""
                )
                card = CS2InventoryGroupCard(
                    item, self.cards_host, rarity=rarity, show_price=False
                )
            else:
                card = GameItemCard(item, self.cards_host, instances=instances)
            self.cards_layout.addWidget(card, index // columns, index % columns)
            image_url = next((instance.icon_url_large or instance.icon_url
                              for instance in instances
                              if instance.icon_url_large or instance.icon_url), "")
            if image_url:
                if image_url in self.image_loader.cache:
                    card.set_image(self.image_loader.cache[image_url])
                else:
                    self.cards_by_image.setdefault(image_url, []).append(card)
                    self.image_loader.request(image_url)

    def _image_loaded(self, reply):
        url = reply.request().url().toString()
        origin = self.image_loader.request_origin.get(url, url)
        self.image_loader.handle_reply(reply)
        pixmap = self.image_loader.cache.get(origin)
        if pixmap is not None:
            update_table_thumbnails(self.table, column=1, url=origin, icon=QIcon(pixmap))
            for card in self.cards_by_image.pop(origin, []):
                if card is not None:
                    card.set_image(pixmap)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.render()
