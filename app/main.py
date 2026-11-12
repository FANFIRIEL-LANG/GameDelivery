import os
import sys
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from PySide6.QtCore import Qt, QUrl, QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QFrame,
    QLineEdit, QComboBox, QListWidget, QListWidgetItem, QMessageBox, QCompleter,
)
from games.steam_cards.snapshot import InventorySnapshotStore
from games.steam_cards.reservations import AssetReservationStore
from app.inventory_panel import InventoryPanel
from app.game_inventory_panel import GameInventoryPanel
from app.bot_selector import BotSelectionModel, configured_bot_names
from app.proxy_settings import ProxySettingsWidget
from app.asf_management import ASFManagementWidget
from app.platforms_panel import MarketplacePanel
from integrations.asf.client import ASFClient, ASFError, TEST_ASF_IPC_URL_ENV
from integrations.openskin.client import configure_openskin_diagnostics
from integrations.asf.trade_credentials import SteamTradeCredentialStore
from app.diagnostics import configure_logging, get_log_directory, get_logger, log_exception


logger = get_logger("ui")


class _AsfCallSignals(QObject):
    finished = Signal(object, object, object)


class _AsfCallWorker(QRunnable):
    """Run one blocking ASF IPC call away from the Qt GUI thread."""

    def __init__(self, operation, callback):
        super().__init__()
        self.operation = operation
        self.callback = callback
        self.signals = _AsfCallSignals()

    def run(self):
        try:
            result = self.operation()
        except Exception as exc:
            self.signals.finished.emit(self, None, exc)
        else:
            self.signals.finished.emit(self, result, None)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        logger.info("MainWindow initialization started")
        self.setWindowTitle("GameDelivery — локальная панель")
        try:
            self._trade_credentials = SteamTradeCredentialStore()
            self._ensure_delivery_order_schema()
            self._recover_interrupted_delivery_orders()
            logger.info("Local delivery storage initialized")
        except (OSError, sqlite3.Error, ValueError) as exc:
            log_exception(logger, "local storage initialization", exc)
            message = (
                "GameDelivery не может безопасно открыть локальные заказы или системное хранилище "
                "секретов. Исходные данные не сбрасывались. Проверь доступ к базе и хранилищу "
                "учётных данных; перед восстановлением сделай резервную копию.\n\n"
                f"Подробности: {type(exc).__name__}"
            )
            QMessageBox.critical(self, "Ошибка локальной базы данных", message)
            self.startup_error_label = QLabel(message)
            self.startup_error_label.setWordWrap(True)
            self.startup_error_label.setMargin(24)
            self.startup_error_label.setObjectName("databaseStartupError")
            self.setCentralWidget(self.startup_error_label)
            self.resize(720, 260)
            self.setMinimumSize(560, 220)
            return
        logger.info("MainWindow initialized successfully")
        self.resize(1120, 720)
        self.setMinimumSize(900, 600)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(14)
        header = QHBoxLayout()
        title = QLabel("GameDelivery")
        title.setObjectName("title")
        subtitle = QLabel("Автоматизация доставки игровых предметов")
        subtitle.setObjectName("muted")
        status = QLabel("Playerok: не подключён")
        self.platform_status = status
        status.setObjectName("status")
        header.addWidget(title)
        header.addSpacing(12)
        header.addWidget(subtitle)
        header.addStretch()
        header.addWidget(status)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.bot_selection_model = BotSelectionModel(self)
        if not os.environ.get(TEST_ASF_IPC_URL_ENV, "").strip():
            self.bot_selection_model.set_accounts(configured_bot_names())
        self.tabs.addTab(self._orders_tab(), "Заказы")
        self.marketplace_panel = MarketplacePanel()
        self.marketplace_panel.connection_status_changed.connect(self.platform_status.setText)
        self.marketplace_panel.order_import_requested.connect(self._import_marketplace_order)
        self.tabs.addTab(self.marketplace_panel, "Площадки")
        self.inventory_panel = self._inventory_tab()
        self.tabs.addTab(self.inventory_panel, "Инвентарь")
        self.game_inventory_panel = GameInventoryPanel(
            bot_selection_model=self.bot_selection_model
        )
        self.bot_selection_model.refresh_requested.connect(
            self.game_inventory_panel.refresh_bot_list
        )
        self.tabs.addTab(self.game_inventory_panel, "Игровые предметы")
        self.inventory_panel.trade_url_requested.connect(self._open_order_with_trade_url)
        # Game inventory snapshots are not Steam Trading Card snapshots.
        # Keep CS2/game items out of the card-only order/send handler until C.6 delivery exists.
        self.game_inventory_panel.trade_url_requested.connect(self._explain_game_delivery_unavailable)
        self.tabs.addTab(self._games_tab(), "Модули игр")
        self.tabs.addTab(self._logs_tab(), "Журнал")
        self.tabs.addTab(self._settings_tab(), "Настройки")
        self.asf_management = ASFManagementWidget()
        self.asf_management.inventory_snapshot_updated.connect(self.inventory_panel.select_bot_snapshot)
        self.asf_management.log_line.connect(self._append_log_line)
        self.tabs.addTab(self.asf_management, "ASF-боты")
        layout.addWidget(self.tabs)
        self.setCentralWidget(root)
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #171b22; color: #e8edf5; font-size: 13px; }
            QLabel#title { font-size: 25px; font-weight: 700; color: #f3f6fb; }
            QLabel#muted { color: #9aa6b7; }
            QLabel#status { color: #f2c879; padding: 8px 12px; background: #29271f; border-radius: 8px; }
            QTabWidget::pane { border: 1px solid #303846; border-radius: 10px; padding: 8px; }
            QTabBar::tab { background: #222833; padding: 10px 16px; margin-right: 4px; border-radius: 6px; }
            QTabBar::tab:selected { background: #304a3d; color: #a8f0c6; }
            QPushButton { background: #2e654b; border: 0; border-radius: 7px; padding: 9px 13px; font-weight: 600; }
            QPushButton:hover { background: #397b5b; }
            QTableWidget, QTextEdit { background: #1e242d; border: 1px solid #303846; border-radius: 7px; gridline-color: #303846; }
            QLineEdit, QComboBox, QSpinBox, QListWidget { background: #1e242d; border: 1px solid #303846; border-radius: 6px; padding: 6px; }
            QHeaderView::section { background: #252d38; color: #cbd5e1; border: 0; padding: 8px; }
            QFrame#card { background: #202731; border: 1px solid #303846; border-radius: 10px; }
        """)

    def _open_order_with_trade_url(self, trade_url: str, selected_assets=None):
        value = str(trade_url or "").strip()
        if not value:
            QMessageBox.warning(self, "Trade URL", "Укажи Steam Trade URL получателя.")
            return
        try:
            value = self._validate_trade_url(value)
        except ValueError as exc:
            QMessageBox.warning(self, "Некорректный Trade URL", str(exc))
            return

        normalized_assets = sorted({
            (str(bot).strip(), str(asset).strip())
            for bot, asset in (selected_assets or [])
            if str(bot).strip() and str(asset).strip()
        })
        if normalized_assets:
            bots = {bot.casefold() for bot, _asset in normalized_assets}
            if len(bots) != 1:
                QMessageBox.warning(
                    self, "Выбраны предметы нескольких ботов",
                    "Текущая подготовка одного Steam-предложения поддерживает один ASF-бот. "
                    "Выбери предметы одного бота; активы остальных ботов отправляются отдельными предложениями.",
                )
                return

        self.order_recipient.setText(value)
        if normalized_assets:
            bot_name = normalized_assets[0][0]
            self._load_order_bots()
            bot_index = self.order_bot.findData(bot_name)
            if bot_index < 0:
                QMessageBox.warning(
                    self, "Не найден ASF-бот",
                    f"Бот {bot_name} отсутствует в списке снимков. Выбранные активы не перенесены.",
                )
                return
            previous_index = self.order_bot.currentIndex()
            self.order_bot.setCurrentIndex(bot_index)
            if previous_index == bot_index:
                self._load_order_assets()
            selected_set = set(normalized_assets)
            matched = set()
            self.order_assets.clearSelection()
            for row in range(self.order_assets.count()):
                item = self.order_assets.item(row)
                key = item.data(Qt.ItemDataRole.UserRole)
                if key in selected_set and item.flags() & Qt.ItemFlag.ItemIsSelectable:
                    item.setSelected(True)
                    matched.add(key)
            if matched != selected_set:
                self.order_assets.clearSelection()
                missing = len(selected_set - matched)
                QMessageBox.warning(
                    self, "Не все выбранные активы доступны",
                    f"{missing} выбранных активов не найдены в свежем снимке или уже зарезервированы. "
                    "Ничего не перенесено в состав заказа; обнови инвентарь и проверь выбор.",
                )
                return

        self.tabs.setCurrentIndex(0)
        self.statusBar().showMessage(
            f"Trade URL перенесён в «Заказы». Выбрано активов: {len(normalized_assets)}. "
            "Проверь состав; резерв и отправка выполняются отдельными подтверждёнными действиями."
        )

    def _explain_game_delivery_unavailable(self, trade_url: str, selected_assets=None):
        """Do not route CS2/game assets into the Steam-card-only order flow."""
        count = len(selected_assets or [])
        QMessageBox.information(
            self,
            "Доставка игровых предметов ещё не подключена",
            f"Выбрано игровых предметов: {count}.\n\n"
            "Инвентарь игровых предметов уже загружается и отображается, "
            "но текущая подготовка и отправка через ASF поддерживает только обычные "
            "Steam Trading Cards (AppID 753 / context 6).\n\n"
            "Предметы CS2 (AppID 730 / context 2) нельзя передавать через карточный "
            "обработчик. Они не будут добавлены в заказ и не будут зарезервированы. "
            "Для них нужен отдельный проверенный обработчик этапа C.6."
        )

    def _orders_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "Playerok-заказы можно импортировать с вкладки «Площадки». Выбери импортированный заказ, "
            "введи trade URL покупателя и отметь активы ASF. Если заказ не выбран, создастся ручной черновик. "
            "Резервирование локальное; отправка доступна отдельной кнопкой после проверки состава через ASF и явного подтверждения."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QHBoxLayout()
        self.order_recipient = QLineEdit()
        self.order_recipient.setPlaceholderText("HTTPS trade URL получателя")
        self.order_bot = QComboBox()
        self.order_bot.setMinimumWidth(160)
        self.order_bot.setEditable(True)
        self.order_bot.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        order_bot_completer = QCompleter(self.order_bot.model(), self.order_bot)
        order_bot_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        order_bot_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        order_bot_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.order_bot.setCompleter(order_bot_completer)
        self.order_bot.currentIndexChanged.connect(self._load_order_assets)
        form.addWidget(self.order_recipient, 2)
        form.addWidget(self.order_bot, 1)
        self.order_refresh = QPushButton("Обновить активы")
        self.order_refresh.clicked.connect(self._load_order_bots)
        form.addWidget(self.order_refresh)
        layout.addLayout(form)

        self.order_assets = QListWidget()
        self.order_assets.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        layout.addWidget(QLabel("Доступные активы (выдели конкретные карточки для резерва):"))
        layout.addWidget(self.order_assets, 2)

        actions = QHBoxLayout()
        self.prepare_order_button = QPushButton("Проверить и зарезервировать")
        self.prepare_order_button.clicked.connect(self._prepare_delivery_order)
        self.preview_offer_button = QPushButton("Проверить черновик через ASF")
        self.preview_offer_button.clicked.connect(self._preview_selected_order_with_asf)
        self.send_offer_button = QPushButton("Отправить предложение Steam…")
        self.send_offer_button.setEnabled(False)
        self.send_offer_button.clicked.connect(self._start_send_selected_order_async)
        actions.addWidget(self.prepare_order_button)
        actions.addWidget(self.preview_offer_button)
        actions.addWidget(self.send_offer_button)
        actions.addStretch()
        layout.addLayout(actions)

        AssetReservationStore(self._database_path)
        self._ensure_delivery_order_schema()
        self._recover_interrupted_delivery_orders()
        self.orders = QTableWidget(0, 5)
        self.orders.setHorizontalHeaderLabels(["ID заказа", "Источник", "Товар / активы", "Статус", "Обновлено"])
        self.orders.horizontalHeader().setStretchLastSection(True)
        self.orders.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.orders.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.orders.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.orders.itemSelectionChanged.connect(self._update_order_action_buttons)
        self.orders.cellDoubleClicked.connect(self._show_order_details)
        layout.addWidget(self.orders, 2)
        order_actions = QHBoxLayout()
        self.order_details_button = QPushButton("Состав заказа")
        self.order_details_button.clicked.connect(self._show_selected_order_details)
        self.cancel_order_button = QPushButton("Отменить заказ")
        self.cancel_order_button.clicked.connect(self._cancel_selected_order)
        self.reconcile_order_button = QPushButton("Снять резерв после отмены обмена…")
        self.reconcile_order_button.clicked.connect(self._reconcile_cancelled_order_reservation)
        self.delete_order_button = QPushButton("Удалить заказ")
        self.delete_order_button.clicked.connect(self._delete_selected_order)
        order_actions.addWidget(self.order_details_button)
        order_actions.addWidget(self.cancel_order_button)
        order_actions.addWidget(self.reconcile_order_button)
        order_actions.addWidget(self.delete_order_button)
        order_actions.addStretch()
        layout.addLayout(order_actions)
        self._load_order_bots()
        self._load_saved_orders()
        return page

    @property
    def _database_path(self):
        return Path.home() / ".gamedelivery" / "gamedelivery.sqlite3"

    def _ensure_delivery_order_schema(self):
        """Create or migrate local orders while preserving existing rows."""
        with sqlite3.connect(self._database_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS delivery_orders (
                    order_id TEXT PRIMARY KEY,
                    recipient_trade_url TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    asset_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    source_platform TEXT NOT NULL DEFAULT 'manual',
                    source_order_id TEXT,
                    source_title TEXT NOT NULL DEFAULT '',
                    source_buyer TEXT NOT NULL DEFAULT '',
                    source_status TEXT NOT NULL DEFAULT '',
                    source_amount TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    trade_offer_id TEXT NOT NULL DEFAULT '',
                    trade_confirmation_status TEXT NOT NULL DEFAULT ''
                )
            """)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(delivery_orders)").fetchall()
            }
            migrations = {
                "source_platform": "TEXT NOT NULL DEFAULT 'manual'",
                "source_order_id": "TEXT",
                "source_title": "TEXT NOT NULL DEFAULT ''",
                "source_buyer": "TEXT NOT NULL DEFAULT ''",
                "source_status": "TEXT NOT NULL DEFAULT ''",
                "source_amount": "TEXT NOT NULL DEFAULT ''",
                "source_url": "TEXT NOT NULL DEFAULT ''",
                "trade_offer_id": "TEXT NOT NULL DEFAULT ''",
                "trade_confirmation_status": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE delivery_orders ADD COLUMN {name} {definition}")
            db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_external_order
                ON delivery_orders (source_platform, source_order_id)
                WHERE source_order_id IS NOT NULL AND source_order_id <> ''
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS delivery_order_assets (
                    order_id TEXT NOT NULL,
                    bot_name TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    PRIMARY KEY (order_id, bot_name, asset_id),
                    FOREIGN KEY (order_id) REFERENCES delivery_orders(order_id)
                )
            """)

        # Migrate existing plaintext Steam tokens to the OS credential vault.
        # Keep the original DB value until the vault confirms the token was saved.
        with sqlite3.connect(self._database_path) as db:
            db.execute("PRAGMA secure_delete = ON")
            legacy_urls = db.execute(
                "SELECT order_id, recipient_trade_url FROM delivery_orders "
                "WHERE recipient_trade_url <> ''"
            ).fetchall()
            for order_id, stored_url in legacy_urls:
                safe_url = self._trade_credentials.migrate_legacy_url(order_id, stored_url)
                if safe_url != stored_url:
                    db.execute(
                        "UPDATE delivery_orders SET recipient_trade_url = ? "
                        "WHERE order_id = ? AND recipient_trade_url = ?",
                        (safe_url, order_id, stored_url),
                    )

    def _recover_interrupted_delivery_orders(self) -> int:
        """Fail closed for interrupted sends and remove their saved Steam trade tokens."""
        with sqlite3.connect(self._database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            interrupted_order_ids = [
                row[0]
                for row in db.execute(
                    "SELECT order_id FROM delivery_orders WHERE status = ?",
                    ("sending — запрос ASF",),
                ).fetchall()
            ]
            cursor = db.execute(
                "UPDATE delivery_orders "
                "SET status = ?, trade_confirmation_status = 'unknown' "
                "WHERE status = ?",
                (
                    "неизвестен результат отправки — проверь исходящие Steam",
                    "sending — запрос ASF",
                ),
            )
            recovered_count = cursor.rowcount

        # The order is now locked against retry in SQLite. Remove the credential only
        # after that state is committed, so a failed DB write cannot lose the token.
        for order_id in interrupted_order_ids:
            self._delete_trade_credential_best_effort(order_id)
        return recovered_count


    def _import_marketplace_order(self, marketplace_order):
        platform = str(marketplace_order.get("platform") or "").strip().lower()
        external_id = str(marketplace_order.get("order_id") or "").strip()
        title = str(marketplace_order.get("title") or "Заказ с торговой площадки").strip()
        buyer = str(marketplace_order.get("buyer") or "").strip()
        source_status = str(marketplace_order.get("status") or "").strip()
        source_amount = str(marketplace_order.get("amount") or "").strip()
        source_url = str(marketplace_order.get("url") or "").strip()
        if platform != "playerok" or not external_id:
            self.statusBar().showMessage("Не удалось импортировать заказ: нет поддерживаемого источника или ID.")
            return
        local_order_id = f"GD-{uuid4().hex[:12].upper()}"
        imported_now = False
        try:
            self._ensure_delivery_order_schema()
            with sqlite3.connect(self._database_path) as db:
                existing = db.execute(
                    "SELECT order_id FROM delivery_orders WHERE source_platform = ? AND source_order_id = ?",
                    (platform, external_id),
                ).fetchone()
                if existing:
                    local_order_id = existing[0]
                    db.execute(
                        "UPDATE delivery_orders SET source_status = ?, source_amount = ?, source_url = ? "
                        "WHERE order_id = ?",
                        (source_status, source_amount, source_url, local_order_id),
                    )
                else:
                    db.execute("""
                        INSERT INTO delivery_orders
                            (order_id, recipient_trade_url, status, asset_count,
                             source_platform, source_order_id, source_title, source_buyer,
                             source_status, source_amount, source_url)
                        VALUES (?, '', 'imported — ожидание подготовки', 0, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        local_order_id, platform, external_id, title, buyer,
                        source_status, source_amount, source_url,
                    ))
                    imported_now = True
            self._load_saved_orders()
            self.tabs.setCurrentIndex(0)
            for row in range(self.orders.rowCount()):
                if self.orders.item(row, 0).text() == local_order_id:
                    self.orders.selectRow(row)
                    break
            if imported_now:
                self.statusBar().showMessage(
                    f"Сделка Playerok {external_id} добавлена как {local_order_id}. "
                    "Укажи trade URL покупателя и выбери активы для подготовки."
                )
            else:
                self.statusBar().showMessage(
                    f"Сделка Playerok {external_id} уже есть как {local_order_id}; "
                    "данные площадки обновлены."
                )
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.statusBar().showMessage(f"Не удалось импортировать заказ Playerok: {exc}")

    def _load_order_bots(self, *_):
        current = self.order_bot.currentData() if hasattr(self, "order_bot") else None
        self.order_bot.clear()
        try:
            for bot in sorted(InventorySnapshotStore(self._database_path).load_all(), key=str.casefold):
                status = InventorySnapshotStore(self._database_path).status(bot)
                label = f"{bot} · {status['item_count']} · {status['updated_at'] or 'без даты'}"
                self.order_bot.addItem(label, bot)
            if current:
                index = self.order_bot.findData(current)
                if index >= 0:
                    self.order_bot.setCurrentIndex(index)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.order_bot.addItem("Ошибка чтения снимков", "")
            self.statusBar().showMessage(f"Не удалось загрузить снимки: {exc}")
        self._load_order_assets()

    def _load_order_assets(self, *_):
        self.order_assets.clear()
        bot = self.order_bot.currentData()
        if not bot:
            return
        try:
            store = InventorySnapshotStore(self._database_path)
            status = store.status(bot)
            if status["stale"] or not status["updated_at"]:
                self.order_assets.addItem("Нет подтверждённого снимка — сначала обнови инвентарь через ASF.")
                return
            with sqlite3.connect(self._database_path) as db:
                rows = db.execute(
                    "SELECT asset_id, game_name, card_name, is_trading_card, is_foil, is_tradable "
                    "FROM steam_inventory_snapshot WHERE bot_name = ? ORDER BY game_name, card_name, asset_id",
                    (bot,),
                ).fetchall()
                existing = db.execute(
                    "SELECT asset_id FROM steam_asset_reservations WHERE bot_name = ?", (bot,)
                ).fetchall()
            reserved_ids = {row[0] for row in existing}
            for asset_id, game, name, is_card, is_foil, is_tradable in rows:
                if is_card != 1 or is_foil == 1:
                    continue
                kind = "Карточка"
                label = f"{game} — {name} [{kind}] · asset {asset_id}"
                if is_tradable != 1:
                    state = (
                        "ПЕРЕДАЧА ЗАПРЕЩЕНА"
                        if is_tradable == 0
                        else "ПЕРЕДАВАЕМОСТЬ НЕ ПРОВЕРЕНА"
                    )
                    label += f" · {state}"
                    item = QListWidgetItem(label)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                elif asset_id in reserved_ids:
                    label += " · УЖЕ ЗАРЕЗЕРВИРОВАН"
                    item = QListWidgetItem(label)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                else:
                    item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, (bot, asset_id))
                self.order_assets.addItem(item)
            if self.order_assets.count() == 0:
                self.order_assets.addItem("В снимке нет обычных Steam Trading Cards.")
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.order_assets.clear()
            self.order_assets.addItem(f"Ошибка чтения инвентаря: {exc}")

    @staticmethod
    def _validate_trade_url(value):
        url = value.strip()
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        partner = params.get("partner", [""])[0]
        token = params.get("token", [""])[0]
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"steamcommunity.com", "www.steamcommunity.com"}
            or parsed.path.rstrip("/") != "/tradeoffer/new"
            or not partner.isdigit()
            or not token
            or len(token) > 128
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in token)
        ):
            raise ValueError("Укажи корректную HTTPS-ссылку Steam tradeoffer/new с partner и token.")
        return url

    def _prepare_delivery_order(self):
        try:
            recipient = self._validate_trade_url(self.order_recipient.text())
            selected = [
                item.data(Qt.ItemDataRole.UserRole)
                for item in self.order_assets.selectedItems()
                if item.data(Qt.ItemDataRole.UserRole) is not None
            ]
            if not selected:
                raise ValueError("Выбери хотя бы один незарезервированный актив.")

            # Validate snapshot freshness and every selected asset again at action time.
            snapshot_store = InventorySnapshotStore(self._database_path)
            for bot_name in {item[0] for item in selected}:
                snapshot_status = snapshot_store.status(bot_name)
                if snapshot_status["stale"] or not snapshot_status["updated_at"]:
                    raise ValueError(
                        f"Снимок инвентаря бота {bot_name} устарел или не подтверждён. "
                        "Обнови инвентарь через ASF перед резервированием."
                    )
            with sqlite3.connect(self._database_path) as db:
                for bot_name, asset_id in selected:
                    row = db.execute(
                        "SELECT is_trading_card, is_foil, is_tradable "
                        "FROM steam_inventory_snapshot WHERE bot_name = ? AND asset_id = ?",
                        (bot_name, asset_id),
                    ).fetchone()
                    if row is None or row[0] != 1 or row[1] == 1:
                        raise ValueError(
                            f"Актив {bot_name}/{asset_id} не является обычной Steam Trading Card."
                        )
                    if row[2] != 1:
                        raise ValueError(
                            f"Актив {bot_name}/{asset_id} нельзя резервировать: "
                            "передаваемость запрещена или не подтверждена. Обнови снимок ASF."
                        )

            self._ensure_delivery_order_schema()
            existing_order_id = None
            selected_row = self.orders.currentRow()
            if selected_row >= 0 and self.orders.item(selected_row, 0):
                candidate_id = self.orders.item(selected_row, 0).text().strip()
                with sqlite3.connect(self._database_path) as db:
                    candidate = db.execute(
                        "SELECT source_platform, status FROM delivery_orders WHERE order_id = ?",
                        (candidate_id,),
                    ).fetchone()
                if candidate and candidate[0] == "playerok":
                    if candidate[1] != "imported — ожидание подготовки":
                        raise ValueError(
                            "Эта сделка Playerok уже не ожидает подготовки. "
                            "Нельзя создавать отдельный ручной заказ из выбранной сделки."
                        )
                    existing_order_id = candidate_id

            order_id = existing_order_id or f"GD-{uuid4().hex[:12].upper()}"
            reservations = AssetReservationStore(self._database_path)
            result = reservations.reserve(order_id, selected)
            if not result["reserved"]:
                raise ValueError("Конфликт резерва: " + ", ".join(
                    f"{x['bot_name']} / {x['asset_id']} → {x['reserved_by']}"
                    for x in result["conflicts"]
                ))

            credential_stored = False
            try:
                safe_recipient = self._trade_credentials.store_trade_url(order_id, recipient)
                credential_stored = True
                with sqlite3.connect(self._database_path) as db:
                    db.execute("BEGIN IMMEDIATE")
                    if existing_order_id:
                        current = db.execute(
                            "SELECT status FROM delivery_orders WHERE order_id = ?", (order_id,)
                        ).fetchone()
                        if current is None or current[0] != "imported — ожидание подготовки":
                            raise ValueError("Заказ Playerok уже изменился. Обнови список и повтори.")
                        db.execute(
                            "UPDATE delivery_orders SET recipient_trade_url = ?, "
                            "status = 'reserved — не отправлен', asset_count = ? "
                            "WHERE order_id = ? AND status = 'imported — ожидание подготовки'",
                            (safe_recipient, len(selected), order_id),
                        )
                    else:
                        db.execute(
                            "INSERT INTO delivery_orders "
                            "(order_id, recipient_trade_url, status, asset_count, source_platform) "
                            "VALUES (?, ?, 'reserved — не отправлен', ?, 'manual')",
                            (order_id, safe_recipient, len(selected)),
                        )
                    db.executemany(
                        "INSERT INTO delivery_order_assets (order_id, bot_name, asset_id) VALUES (?, ?, ?)",
                        [(order_id, bot_name, asset_id) for bot_name, asset_id in selected],
                    )
            except Exception:
                reservations.release(order_id)
                if credential_stored:
                    try:
                        self._trade_credentials.delete_trade_url(order_id)
                    except OSError:
                        pass
                raise

            self._load_saved_orders()
            self._load_order_assets()
            QMessageBox.information(
                self, "Заказ подготовлен",
                f"Заказ {order_id} создан/подготовлен. Активов зарезервировано: {len(selected)}. "
                "Обмен не отправлялся."
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "Не удалось подготовить заказ", str(exc))

    def _preview_selected_order_with_asf(self):
        try:
            order_id = self._selected_order_id()
            logger.info("Trade send preflight started; order_id=%s", order_id)
            with sqlite3.connect(self._database_path) as db:
                order = db.execute(
                    "SELECT recipient_trade_url, status FROM delivery_orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                assets = db.execute(
                    "SELECT bot_name, asset_id FROM delivery_order_assets "
                    "WHERE order_id = ? ORDER BY bot_name, asset_id",
                    (order_id,),
                ).fetchall()
                reserved_assets = db.execute(
                    "SELECT bot_name, asset_id FROM steam_asset_reservations "
                    "WHERE order_id = ? ORDER BY bot_name, asset_id",
                    (order_id,),
                ).fetchall()

            if order is None:
                raise ValueError("Заказ не найден в базе данных.")
            recipient, status = order
            if status != "reserved — не отправлен":
                raise ValueError(
                    "Проверить через ASF можно только подготовленный заказ, "
                    "который ещё не отправлялся."
                )
            recipient = self._trade_credentials.load_trade_url(order_id, recipient)
            recipient = self._validate_trade_url(recipient)
            if not assets:
                raise ValueError("У заказа нет сохранённого состава активов.")
            if assets != reserved_assets:
                raise ValueError(
                    "Состав заказа не совпадает с действующими резервами активов. "
                    "Обнови резерв перед проверкой черновика."
                )
            bot_names = {bot_name for bot_name, _asset_id in assets}
            if len(bot_names) != 1:
                raise ValueError(
                    "Для одного предложения все активы должны принадлежать одному ASF-боту."
                )

            bot_name = next(iter(bot_names))
            ipc_password = self.asf_management.ipc_password.text().strip() or None
            preview = self.asf_management._client(timeout=30.0).preview_trade_offer(
                order_id=order_id,
                bot_name=bot_name,
                trade_url=recipient,
                asset_ids=[str(asset_id) for _bot, asset_id in assets],
            )

            lines = [
                f"Заказ: {preview['orderId']}",
                f"ASF-бот: {preview['botName']}",
                f"Получатель SteamID64: {preview['targetSteamId64']}",
                f"Активов подтверждено: {preview['assetCount']}",
                "",
                "Проверенный состав:",
            ]
            lines.extend(
                f"• {asset.get('name', 'Steam-предмет')} — asset {asset['assetId']}"
                for asset in preview["assets"]
            )
            lines.extend([
                "",
                "Черновик проверен по актуальному инвентарю ASF.",
                "Торговое предложение НЕ создавалось и НЕ отправлялось.",
            ])
            QMessageBox.information(self, "Черновик проверен через ASF", "\n".join(lines))
        except (OSError, sqlite3.Error, ValueError, ASFError) as exc:
            QMessageBox.warning(self, "Проверка черновика через ASF", str(exc))

    def _delete_trade_credential_best_effort(self, order_id):
        try:
            self._trade_credentials.delete_trade_url(order_id)
        except OSError:
            self.statusBar().showMessage(
                "Не удалось удалить сохранённый Steam trade token из системного хранилища."
            )

    def _start_send_selected_order_async(self):
        """Validate locally, then perform preview and send IPC calls off the GUI thread."""
        if getattr(self, "_asf_send_in_progress", False):
            return
        self._asf_send_in_progress = True
        self.send_offer_button.setEnabled(False)
        self.preview_offer_button.setEnabled(False)
        try:
            order_id = self._selected_order_id()
            with sqlite3.connect(self._database_path) as db:
                order = db.execute(
                    "SELECT recipient_trade_url, status FROM delivery_orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                assets = db.execute(
                    "SELECT bot_name, asset_id FROM delivery_order_assets "
                    "WHERE order_id = ? ORDER BY bot_name, asset_id",
                    (order_id,),
                ).fetchall()
                reservations = db.execute(
                    "SELECT bot_name, asset_id FROM steam_asset_reservations "
                    "WHERE order_id = ? ORDER BY bot_name, asset_id",
                    (order_id,),
                ).fetchall()
            if order is None:
                raise ValueError("Заказ не найден в локальной базе.")
            recipient, status = order
            if status != "reserved — не отправлен":
                raise ValueError("Отправлять можно только подготовленный заказ. Заказы с уже начатой или неизвестной отправкой повторно не запускаются.")
            recipient = self._trade_credentials.load_trade_url(order_id, recipient)
            recipient = self._validate_trade_url(recipient)
            if not assets:
                raise ValueError("У заказа нет сохранённого состава активов.")
            if assets != reservations:
                raise ValueError("Состав заказа не совпадает с действующими резервами активов. Отправка отменена.")
            if len(assets) > 255:
                raise ValueError("Один черновик ASF поддерживает не более 255 выбранных активов.")
            bot_names = {name for name, _asset_id in assets}
            if len(bot_names) != 1:
                raise ValueError("Все активы одного предложения должны принадлежать одному ASF-боту.")
            bot_name = next(iter(bot_names))
            asset_ids = [str(asset_id) for _bot, asset_id in assets]
            client = self.asf_management._client(timeout=45.0)
            logger.info("Async trade preflight complete; order_id=%s bot=%s asset_count=%s", order_id, bot_name, len(asset_ids))
            self.statusBar().showMessage(f"Проверка заказа {order_id} через ASF…")
            self._run_asf_call_async(
                lambda: client.preview_trade_offer(
                    order_id=order_id, bot_name=bot_name, trade_url=recipient, asset_ids=asset_ids
                ),
                lambda result, error: self._on_async_trade_preview(
                    order_id, bot_name, recipient, asset_ids, assets, client, result, error
                ),
            )
        except (OSError, sqlite3.Error, ValueError, ASFError, KeyError, TypeError) as exc:
            log_exception(logger, "async trade preflight", exc)
            self._finish_async_send_ui()
            QMessageBox.warning(self, "Отправка через ASF не начата", str(exc))

    def _run_asf_call_async(self, operation, callback):
        worker = _AsfCallWorker(operation, callback)
        if not hasattr(self, "_asf_workers"):
            self._asf_workers = set()
        self._asf_workers.add(worker)
        # A bound MainWindow QObject slot gives Qt an explicit GUI-thread receiver.
        worker.signals.finished.connect(self._dispatch_asf_call_result)
        QThreadPool.globalInstance().start(worker)

    def _dispatch_asf_call_result(self, worker, result, error):
        self._asf_workers.discard(worker)
        worker.callback(result, error)

    def _on_async_trade_preview(self, order_id, bot_name, recipient, asset_ids, assets, client, preview, error):
        if error is not None:
            log_exception(logger, "async ASF trade preview", error)
            self._finish_async_send_ui()
            QMessageBox.warning(self, "Проверка ASF не выполнена", f"{error}\n\nПредложение не отправлялось.")
            return
        try:
            if (
                not isinstance(preview, dict)
                or preview.get("success") is not True
                or preview.get("mode") != "preview-only"
                or preview.get("sendEnabled") is not False
                or preview.get("orderId") != order_id
                or str(preview.get("botName", "")).casefold() != bot_name.casefold()
                or preview.get("assetCount") != len(asset_ids)
                or [str(item.get("assetId", "")) for item in preview.get("assets", []) if isinstance(item, dict)] != asset_ids
            ):
                raise ASFError("ASF не подтвердил точное совпадение получателя, бота и состава активов. Отправка отменена.")
            confirmation_lines = [
                "Сейчас будет создано реальное предложение обмена Steam.", "",
                f"Заказ: {order_id}", f"Бот ASF: {bot_name}",
                f"Получатель SteamID64: {preview['targetSteamId64']}",
                f"Количество активов: {len(asset_ids)}", "", "Asset ID:", *asset_ids, "",
                "GameDelivery повторно проверил передаваемость через ASF.",
                "Отправка не означает, что получатель принял обмен.",
                "При неопределённом результате повторная отправка будет заблокирована.",
            ]
            answer = QMessageBox.question(
                self, "Подтвердить реальную отправку Steam", "\n".join(confirmation_lines),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                logger.info("Async trade send cancelled by user; order_id=%s", order_id)
                self.statusBar().showMessage(f"Отправка заказа {order_id} отменена пользователем.")
                self._finish_async_send_ui()
                return
            with sqlite3.connect(self._database_path) as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT status FROM delivery_orders WHERE order_id = ?", (order_id,)).fetchone()
                current_assets = db.execute(
                    "SELECT bot_name, asset_id FROM delivery_order_assets WHERE order_id = ? ORDER BY bot_name, asset_id",
                    (order_id,),
                ).fetchall()
                current_reservations = db.execute(
                    "SELECT bot_name, asset_id FROM steam_asset_reservations WHERE order_id = ? ORDER BY bot_name, asset_id",
                    (order_id,),
                ).fetchall()
                if current is None or current[0] != "reserved — не отправлен":
                    raise ValueError("Статус заказа изменился во время проверки. Обнови список.")
                if current_assets != assets or current_reservations != assets:
                    raise ValueError("Состав заказа или резервы изменились после проверки. Отправка отменена.")
                changed = db.execute(
                    "UPDATE delivery_orders SET status = 'sending — запрос ASF' "
                    "WHERE order_id = ? AND status = 'reserved — не отправлен'",
                    (order_id,),
                ).rowcount
                if changed != 1:
                    raise ValueError("Заказ уже обрабатывается другой операцией.")
            logger.info("Async trade IPC dispatch started; order_id=%s bot=%s asset_count=%s", order_id, bot_name, len(asset_ids))
            self.statusBar().showMessage(f"Отправка заказа {order_id} через ASF…")
            send_client = self.asf_management._client(timeout=120.0)
            self._run_asf_call_async(
                lambda: send_client.send_trade_offer(
                    order_id=order_id, bot_name=bot_name, trade_url=recipient, asset_ids=asset_ids
                ),
                lambda result, send_error: self._on_async_trade_send_complete(
                    order_id, bot_name, result, send_error
                ),
            )
        except (OSError, sqlite3.Error, ValueError, ASFError, KeyError, TypeError) as exc:
            log_exception(logger, "async trade preview confirmation", exc)
            self._finish_async_send_ui()
            QMessageBox.warning(self, "Отправка через ASF не начата", str(exc))

    def _on_async_trade_send_complete(self, order_id, bot_name, outcome, error):
        try:
            if error is not None:
                log_exception(logger, "async ASF trade send", error)
                unknown = isinstance(error, ASFError)
                new_status = (
                    "неизвестен результат отправки — проверь исходящие Steam"
                    if unknown else "reserved — не отправлен"
                )
                with sqlite3.connect(self._database_path) as db:
                    db.execute(
                        "UPDATE delivery_orders SET status = ? WHERE order_id = ? AND status = ?",
                        (new_status, order_id, "sending — запрос ASF"),
                    )
                if unknown:
                    self._delete_trade_credential_best_effort(order_id)
                    QMessageBox.warning(
                        self, "Результат отправки неизвестен",
                        f"{error}\n\nПроверь исходящие предложения Steam. Повторная отправка запрещена.",
                    )
                else:
                    QMessageBox.warning(self, "Команда ASF не отправлена", f"{error}\n\nПроверь локальные настройки.")
                return
            if not isinstance(outcome, dict):
                raise ASFError("ASF вернул некорректный ответ; результат отправки неизвестен.")
            if outcome.get("success") is True:
                offer_ids = outcome.get("offerIds", [])
                if not offer_ids:
                    raise ASFError("ASF вернул успех без ID предложения; статус остаётся неопределённым.")
                confirmation = str(outcome.get("confirmationStatus", "unknown"))
                status = "sent — требуется подтверждение Steam" if confirmation == "pending" else "sent — ожидание покупателя"
                offer_text = ", ".join(str(value) for value in offer_ids)
                with sqlite3.connect(self._database_path) as db:
                    db.execute(
                        "UPDATE delivery_orders SET status = ?, trade_offer_id = ?, trade_confirmation_status = ? "
                        "WHERE order_id = ? AND status = ?",
                        (status, offer_text, confirmation, order_id, "sending — запрос ASF"),
                    )
                self._delete_trade_credential_best_effort(order_id)
                logger.info("Async trade send confirmed; order_id=%s offer_count=%s", order_id, len(offer_ids))
                QMessageBox.information(
                    self, "Предложение Steam создано",
                    f"Заказ: {order_id}\nБот ASF: {bot_name}\nID предложения: {offer_text}\n"
                    f"Подтверждение: {confirmation}\n\nПредложение создано, но это не подтверждает принятие обмена покупателем.",
                )
            elif outcome.get("resultUnknown") is True:
                offer_ids = outcome.get("offerIds", [])
                valid_ids = sorted(
                    {str(value) for value in offer_ids if str(value).isdigit() and int(value) > 0},
                    key=int,
                ) if isinstance(offer_ids, list) else []
                offer_text = ", ".join(valid_ids)
                with sqlite3.connect(self._database_path) as db:
                    if offer_text:
                        db.execute(
                            "UPDATE delivery_orders SET status = ?, trade_offer_id = ?, trade_confirmation_status = 'unknown' "
                            "WHERE order_id = ? AND status = ?",
                            ("неизвестен результат отправки — проверь исходящие Steam", offer_text, order_id, "sending — запрос ASF"),
                        )
                    else:
                        db.execute(
                            "UPDATE delivery_orders SET status = ?, trade_confirmation_status = 'unknown' "
                            "WHERE order_id = ? AND status = ?",
                            ("неизвестен результат отправки — проверь исходящие Steam", order_id, "sending — запрос ASF"),
                        )
                self._delete_trade_credential_best_effort(order_id)
                logger.error("Async trade send result unknown; order_id=%s", order_id)
                QMessageBox.warning(
                    self, "Результат отправки неизвестен",
                    str(outcome.get("error") or "ASF не подтвердил результат отправки.")
                    + "\n\nПроверь исходящие предложения Steam; повторная отправка запрещена.",
                )
            else:
                with sqlite3.connect(self._database_path) as db:
                    db.execute(
                        "UPDATE delivery_orders SET status = ? WHERE order_id = ? AND status = ?",
                        ("reserved — не отправлен", order_id, "sending — запрос ASF"),
                    )
                QMessageBox.warning(
                    self, "ASF не отправил предложение",
                    str(outcome.get("error") or "ASF отклонил отправку до создания предложения."),
                )
        except (OSError, sqlite3.Error, ValueError, ASFError, KeyError, TypeError) as exc:
            log_exception(logger, "async trade send result handling", exc)
            with sqlite3.connect(self._database_path) as db:
                db.execute(
                    "UPDATE delivery_orders SET status = ?, trade_confirmation_status = 'unknown' "
                    "WHERE order_id = ? AND status = ?",
                    ("неизвестен результат отправки — проверь исходящие Steam", order_id, "sending — запрос ASF"),
                )
            self._delete_trade_credential_best_effort(order_id)
            QMessageBox.warning(
                self, "Результат отправки неизвестен",
                f"{exc}\n\nПроверь исходящие предложения Steam. Не отправляй заказ повторно.",
            )
        finally:
            self._load_saved_orders()
            self._load_order_assets()
            self._finish_async_send_ui()

    def _finish_async_send_ui(self):
        self._asf_send_in_progress = False
        self._load_saved_orders()
        self._update_order_action_buttons()

    def _send_selected_order_with_asf(self):
        request_started = False
        order_id = None
        try:
            order_id = self._selected_order_id()
            with sqlite3.connect(self._database_path) as db:
                order = db.execute(
                    "SELECT recipient_trade_url, status FROM delivery_orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                assets = db.execute(
                    "SELECT bot_name, asset_id FROM delivery_order_assets "
                    "WHERE order_id = ? ORDER BY bot_name, asset_id",
                    (order_id,),
                ).fetchall()
                reserved_assets = db.execute(
                    "SELECT bot_name, asset_id FROM steam_asset_reservations "
                    "WHERE order_id = ? ORDER BY bot_name, asset_id",
                    (order_id,),
                ).fetchall()

            if order is None:
                raise ValueError("Заказ не найден в локальной базе.")
            recipient, status = order
            if status != "reserved — не отправлен":
                raise ValueError(
                    "Отправлять можно только подготовленный заказ со статусом «reserved — не отправлен». "
                    "Заказы с уже начатой или неизвестной отправкой повторно не запускаются."
                )
            recipient = self._trade_credentials.load_trade_url(order_id, recipient)
            recipient = self._validate_trade_url(recipient)
            if not assets:
                raise ValueError("У заказа нет сохранённого состава активов.")
            if assets != reserved_assets:
                raise ValueError(
                    "Состав заказа не совпадает с действующими резервами активов. "
                    "Обнови резерв перед отправкой; предложение не создано."
                )
            if len(assets) > 255:
                raise ValueError("Один черновик ASF поддерживает не более 255 выбранных активов.")
            bot_names = {bot_name for bot_name, _asset_id in assets}
            if len(bot_names) != 1:
                raise ValueError("Все активы одного предложения должны принадлежать одному ASF-боту.")

            bot_name = next(iter(bot_names))
            asset_ids = [str(asset_id) for _bot, asset_id in assets]
            logger.info("Trade send preflight validated; order_id=%s bot=%s asset_count=%s", order_id, bot_name, len(asset_ids))
            ipc_password = self.asf_management.ipc_password.text().strip() or None
            client = self.asf_management._client(timeout=45.0)

            # Re-check the recipient, bot, exact asset list and tradability against the live ASF inventory.
            preview = client.preview_trade_offer(
                order_id=order_id,
                bot_name=bot_name,
                trade_url=recipient,
                asset_ids=asset_ids,
            )
            if (
                preview.get("success") is not True
                or preview.get("mode") != "preview-only"
                or preview.get("sendEnabled") is not False
                or preview.get("orderId") != order_id
                or str(preview.get("botName", "")).casefold() != bot_name.casefold()
                or preview.get("assetCount") != len(asset_ids)
                or [str(item.get("assetId", "")) for item in preview.get("assets", []) if isinstance(item, dict)] != asset_ids
            ):
                raise ASFError("ASF не подтвердил точное совпадение получателя, бота и состава активов. Отправка отменена.")

            confirmation_lines = [
                "Сейчас будет создано реальное предложение обмена Steam.",
                "",
                f"Заказ: {order_id}",
                f"Бот ASF: {bot_name}",
                f"Получатель SteamID64: {preview['targetSteamId64']}",
                f"Количество активов: {len(asset_ids)}",
                "",
                "Asset ID:",
                *asset_ids,
                "",
                "GameDelivery повторно проверил передаваемость через ASF.",
                "Отправка не означает, что получатель принял обмен.",
                "При неопределённом результате повторная отправка будет заблокирована.",
            ]
            answer = QMessageBox.question(
                self,
                "Подтвердить реальную отправку Steam",
                "\n".join(confirmation_lines),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                logger.info("Trade send cancelled by user; order_id=%s", order_id)
                self.statusBar().showMessage(f"Отправка заказа {order_id} отменена пользователем.")
                return

            # Compare-and-set prevents a second UI click from sending the same order concurrently.
            with sqlite3.connect(self._database_path) as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                current_assets = db.execute(
                    "SELECT bot_name, asset_id FROM delivery_order_assets "
                    "WHERE order_id = ? ORDER BY bot_name, asset_id",
                    (order_id,),
                ).fetchall()
                current_reservations = db.execute(
                    "SELECT bot_name, asset_id FROM steam_asset_reservations "
                    "WHERE order_id = ? ORDER BY bot_name, asset_id",
                    (order_id,),
                ).fetchall()
                if current is None or current[0] != "reserved — не отправлен":
                    raise ValueError("Статус заказа изменился во время проверки. Обнови список.")
                if current_assets != assets or current_reservations != assets:
                    raise ValueError(
                        "Состав заказа или резервы изменились после проверки. "
                        "Повтори проверку; предложение не отправлено."
                    )
                changed = db.execute(
                    "UPDATE delivery_orders SET status = 'sending — запрос ASF' "
                    "WHERE order_id = ? AND status = 'reserved — не отправлен'",
                    (order_id,),
                ).rowcount
                if changed != 1:
                    raise ValueError("Заказ уже обрабатывается другой операцией.")

            try:
                request_started = True
                logger.info("Trade send IPC dispatch started; order_id=%s bot=%s asset_count=%s", order_id, bot_name, len(asset_ids))
                outcome = self.asf_management._client(timeout=120.0).send_trade_offer(
                    order_id=order_id,
                    bot_name=bot_name,
                    trade_url=recipient,
                    asset_ids=asset_ids,
                )
            except (OSError, ValueError) as exc:
                # Local validation or one-use-secret file errors occur before command dispatch.
                with sqlite3.connect(self._database_path) as db:
                    db.execute(
                        "UPDATE delivery_orders SET status = ? WHERE order_id = ? AND status = ?",
                        ("reserved — не отправлен", order_id, "sending — запрос ASF"),
                    )
                self._load_saved_orders()
                QMessageBox.warning(
                    self,
                    "Команда ASF не отправлена",
                    f"{exc}\n\nПроверь локальные настройки и повтори проверку.",
                )
                return
            except ASFError as exc:
                # The IPC request might have reached ASF; never reset and blindly retry.
                with sqlite3.connect(self._database_path) as db:
                    db.execute(
                        "UPDATE delivery_orders SET status = ? WHERE order_id = ? AND status = ?",
                        ("неизвестен результат отправки — проверь исходящие Steam", order_id, "sending — запрос ASF"),
                    )
                self._delete_trade_credential_best_effort(order_id)
                self._load_saved_orders()
                QMessageBox.warning(
                    self,
                    "Результат отправки не подтверждён",
                    f"{exc}\n\nПроверь исходящие предложения Steam. Не отправляй этот заказ повторно.",
                )
                return

            if outcome.get("success") is True:
                offer_ids = outcome.get("offerIds", [])
                logger.info("Trade send confirmed by ASF; order_id=%s offer_count=%s", order_id, len(offer_ids))
                if not offer_ids:
                    raise ASFError("ASF вернул успех без ID предложения; статус сохранён как неопределённый.")
                confirmation_status = str(outcome.get("confirmationStatus", "unknown"))
                order_status = (
                    "sent — требуется подтверждение Steam"
                    if confirmation_status == "pending"
                    else "sent — ожидание покупателя"
                )
                offer_id_text = ", ".join(str(value) for value in offer_ids)
                with sqlite3.connect(self._database_path) as db:
                    db.execute(
                        "UPDATE delivery_orders SET status = ?, trade_offer_id = ?, "
                        "trade_confirmation_status = ? WHERE order_id = ? AND status = ?",
                        (order_status, offer_id_text, confirmation_status, order_id, "sending — запрос ASF"),
                    )
                self._delete_trade_credential_best_effort(order_id)
                self._load_saved_orders()
                self._load_order_assets()
                QMessageBox.information(
                    self,
                    "Предложение Steam создано",
                    f"Заказ: {order_id}\nБот ASF: {bot_name}\nID предложения: {offer_id_text}\n"
                    f"Подтверждение: {confirmation_status}\n\n"
                    "Предложение создано, но это не подтверждает принятие обмена покупателем.",
                )
                return

            if outcome.get("resultUnknown") is True:
                logger.error("Trade send result unknown; order_id=%s", order_id)
                status_message = "неизвестен результат отправки — проверь исходящие Steam"
                user_message = str(outcome.get("error") or "Результат отправки не подтверждён.")
                diagnostic_lines = []
                for field, label in (
                    ("exceptionType", "Тип исключения"),
                    ("exceptionMessage", "Сообщение исключения"),
                    ("missingMethod", "Отсутствующий метод"),
                    ("targetSite", "Место сбоя"),
                    ("stackTrace", "Стек вызовов"),
                ):
                    value = outcome.get(field)
                    if isinstance(value, str) and value.strip():
                        diagnostic_lines.append(f"{label}: {value[:8000]}")
                if diagnostic_lines:
                    user_message += "\n\nДиагностика ASF:\n" + "\n".join(diagnostic_lines)
                raw_offer_ids = outcome.get("offerIds", [])
                known_offer_ids = []
                if isinstance(raw_offer_ids, list):
                    for value in raw_offer_ids:
                        text_value = str(value)
                        if text_value.isdigit() and int(text_value) > 0 and text_value not in known_offer_ids:
                            known_offer_ids.append(text_value)
                known_offer_ids.sort(key=int)
                offer_id_text = ", ".join(known_offer_ids)
            else:
                status_message = "reserved — не отправлен"
                user_message = str(outcome.get("error") or "ASF отклонил отправку до создания предложения.")
                offer_id_text = ""

            with sqlite3.connect(self._database_path) as db:
                if outcome.get("resultUnknown") is True and offer_id_text:
                    db.execute(
                        "UPDATE delivery_orders SET status = ?, trade_offer_id = ?, "
                        "trade_confirmation_status = 'unknown' WHERE order_id = ? AND status = ?",
                        (status_message, offer_id_text, order_id, "sending — запрос ASF"),
                    )
                elif outcome.get("resultUnknown") is True:
                    db.execute(
                        "UPDATE delivery_orders SET status = ?, trade_confirmation_status = 'unknown' "
                        "WHERE order_id = ? AND status = ?",
                        (status_message, order_id, "sending — запрос ASF"),
                    )
                else:
                    db.execute(
                        "UPDATE delivery_orders SET status = ? WHERE order_id = ? AND status = ?",
                        (status_message, order_id, "sending — запрос ASF"),
                    )
            if outcome.get("resultUnknown") is True:
                self._delete_trade_credential_best_effort(order_id)
            self._load_saved_orders()
            if outcome.get("resultUnknown") is True:
                QMessageBox.warning(self, "Результат отправки неизвестен", user_message + "\n\nПроверь исходящие предложения Steam; повторная отправка запрещена.")
            else:
                QMessageBox.warning(self, "ASF не отправил предложение", user_message)

        except (OSError, sqlite3.Error, ValueError, ASFError, KeyError, TypeError) as exc:
            log_exception(logger, "trade send workflow", exc)
            if request_started and order_id:
                try:
                    with sqlite3.connect(self._database_path) as db:
                        db.execute(
                            "UPDATE delivery_orders SET status = ? WHERE order_id = ? AND status = ?",
                            ("неизвестен результат отправки — проверь исходящие Steam", order_id, "sending — запрос ASF"),
                        )
                except sqlite3.Error:
                    pass
                self._delete_trade_credential_best_effort(order_id)
            QMessageBox.warning(self, "Отправка через ASF не завершена", str(exc))
        finally:
            self._load_saved_orders()

    def _update_order_action_buttons(self):
        if not hasattr(self, "send_offer_button") or not hasattr(self, "orders"):
            return
        try:
            order_id = self._selected_order_id()
            with sqlite3.connect(self._database_path) as db:
                row = db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id = ?", (order_id,)
                ).fetchone()
            self.send_offer_button.setEnabled(
                not getattr(self, "_asf_send_in_progress", False)
                and row is not None
                and row[0] == "reserved — не отправлен"
            )
        except (OSError, sqlite3.Error, ValueError):
            self.send_offer_button.setEnabled(False)

    def _load_saved_orders(self):
        if not hasattr(self, "orders"):
            return
        self.orders.setRowCount(0)
        try:
            self._ensure_delivery_order_schema()
            with sqlite3.connect(self._database_path) as db:
                rows = db.execute(
                    "SELECT order_id, asset_count, status, created_at, source_platform, "
                    "source_title, source_buyer, trade_offer_id FROM delivery_orders ORDER BY created_at DESC"
                ).fetchall()
            for order_id, count, status, created, platform, title, buyer, trade_offer_id in rows:
                if platform == "playerok":
                    source = "Playerok" + (f" · {buyer}" if buyer else "")
                    items = title or "Заказ Playerok"
                    if count:
                        items += f" · {count} активов"
                else:
                    source = "Ручной заказ"
                    items = f"{count} активов"
                if trade_offer_id:
                    items += f" · offer {trade_offer_id}"
                row = self.orders.rowCount()
                self.orders.insertRow(row)
                for col, value in enumerate((order_id, source, items, status, created)):
                    self.orders.setItem(row, col, QTableWidgetItem(str(value)))
        except sqlite3.Error as exc:
            self.statusBar().showMessage(f"Не удалось загрузить сохранённые заказы: {exc}")
        finally:
            self._update_order_action_buttons()

    def _selected_order_id(self):
        row = self.orders.currentRow()
        if row < 0:
            raise ValueError("Сначала выбери заказ в таблице.")
        item = self.orders.item(row, 0)
        if item is None or not item.text().strip():
            raise ValueError("Не удалось определить выбранный заказ.")
        return item.text().strip()

    def _show_selected_order_details(self):
        try:
            self._show_order_details(self.orders.currentRow(), 0)
        except (OSError, sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "Состав заказа", str(exc))

    def _show_order_details(self, row, _column=0):
        if row < 0:
            return
        order_id = self.orders.item(row, 0).text()
        with sqlite3.connect(self._database_path) as db:
            order = db.execute(
                "SELECT recipient_trade_url, status, source_platform, source_order_id, "
                "source_title, source_buyer, source_status, source_amount, source_url "
                "FROM delivery_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            assets = db.execute(
                "SELECT bot_name, asset_id FROM delivery_order_assets WHERE order_id = ? "
                "ORDER BY bot_name, asset_id",
                (order_id,),
            ).fetchall()
        if order is None:
            raise ValueError("Заказ не найден в базе данных.")
        lines = [f"Заказ GameDelivery: {order_id}", f"Статус: {order[1]}"]
        if order[2] == "playerok":
            lines.extend([
                f"Источник: Playerok",
                f"ID сделки Playerok: {order[3] or '—'}",
                f"Товар: {order[4] or '—'}",
                f"Покупатель Playerok: {order[5] or '—'}",
                f"Статус на площадке при последнем обновлении: {order[6] or '—'}",
                f"Сумма на площадке при последнем обновлении: {order[7] or '—'}",
                f"Ссылка на сделку: {order[8] or '—'}",
            ])
        lines.extend([
            f"Steam trade URL: {order[0] or 'не указан'}",
            "Токен Steam хранится отдельно в системном хранилище учётных данных.",
            "",
            "Зарезервированные активы:",
        ])
        lines.extend(f"• {bot} — asset {asset}" for bot, asset in assets)
        if not assets:
            lines.append("Состав не сохранён (старый заказ).")
        QMessageBox.information(self, "Состав заказа", "\n".join(lines))

    def _cancel_selected_order(self):
        try:
            order_id = self._selected_order_id()
            with sqlite3.connect(self._database_path) as db:
                row = db.execute(
                    "SELECT status, source_platform FROM delivery_orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
            if row is None:
                raise ValueError("Заказ не найден в базе данных.")
            original_status, source_platform = row
            unknown_status = "неизвестен результат отправки — проверь исходящие Steam"
            if original_status not in {
                "reserved — не отправлен",
                "imported — ожидание подготовки",
                unknown_status,
            }:
                raise ValueError(
                    "Этот заказ уже отправлялся или завершил обработку. "
                    "Для отмены предложения Steam сначала нужно отозвать само предложение."
                )
            is_unknown = original_status == unknown_status
            cancelled_status = (
                "cancelled — результат отправки требует сверки"
                if is_unknown
                else (
                    "cancelled — резерв снят"
                    if original_status == "reserved — не отправлен"
                    else "cancelled — отказ от заказа"
                )
            )
            action_text = (
                "закрыть заказ, сохранив резерв до ручной проверки исходящих Steam"
                if is_unknown
                else (
                    "освободить зарезервированные активы"
                    if original_status == "reserved — не отправлен"
                    else "убрать импортированный заказ из очереди"
                )
            )
            answer = QMessageBox.question(
                self, "Отменить заказ",
                f"Отменить {order_id} и {action_text}? Обмен не отправляется.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            with sqlite3.connect(self._database_path) as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id = ?", (order_id,)
                ).fetchone()
                if current is None or current[0] != original_status:
                    raise ValueError("Статус заказа изменился. Обнови список и повтори действие.")
                db.execute(
                    "UPDATE delivery_orders SET status = ? WHERE order_id = ? AND status = ?",
                    (cancelled_status, order_id, original_status),
                )
                if not is_unknown:
                    db.execute(
                        "DELETE FROM steam_asset_reservations WHERE order_id = ?", (order_id,)
                    )
            self._delete_trade_credential_best_effort(order_id)
            self._load_saved_orders()
            self._load_order_assets()
            self.statusBar().showMessage(f"Заказ {order_id} отменён.")
        except (OSError, sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "Не удалось отменить заказ", str(exc))

    def _reconcile_cancelled_order_reservation(self):
        """Release a retained reservation only after operator confirms the Steam offer is cancelled."""
        try:
            order_id = self._selected_order_id()
            unknown_status = "неизвестен результат отправки — проверь исходящие Steam"
            reconcilable_statuses = {
                unknown_status,
                "cancelled — результат отправки требует сверки",
                "sent — ожидание покупателя",
                "sent — требуется подтверждение Steam",
            }
            with sqlite3.connect(self._database_path) as db:
                row = db.execute(
                    "SELECT status, trade_offer_id FROM delivery_orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                reservations = db.execute(
                    "SELECT COUNT(*) FROM steam_asset_reservations WHERE order_id = ?",
                    (order_id,),
                ).fetchone()[0]
            if row is None:
                raise ValueError("Заказ не найден в базе данных.")
            status, offer_ids = row
            if status not in reconcilable_statuses:
                raise ValueError(
                    "Снять резерв этим действием можно только после отправки или при неизвестном результате. "
                    "Для неотправленного заказа используй обычную отмену."
                )
            if not reservations:
                QMessageBox.information(
                    self, "Резерв уже отсутствует",
                    f"У заказа {order_id} нет активных резервов."
                )
                return
            answer = QMessageBox.question(
                self,
                "Подтвердить отмену предложения Steam",
                f"Заказ: {order_id}\n"
                f"ID предложения: {offer_ids or 'не получен'}\n"
                f"Активных резервов: {reservations}\n\n"
                "Подтверди, что ты лично проверил исходящие предложения Steam и "
                "предложение по этому заказу отменено, отклонено или отсутствует. "
                "После подтверждения GameDelivery освободит активы. "
                "Это действие не отменяет предложение в Steam и не отправляет обмен.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            with sqlite3.connect(self._database_path) as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id = ?", (order_id,)
                ).fetchone()
                if current is None or current[0] != status or current[0] not in reconcilable_statuses:
                    raise ValueError("Статус заказа изменился. Обнови список и повтори сверку.")
                db.execute(
                    "DELETE FROM steam_asset_reservations WHERE order_id = ?", (order_id,)
                )
                db.execute(
                    "UPDATE delivery_orders SET status = ?, trade_confirmation_status = ? "
                    "WHERE order_id = ?",
                    ("cancelled — предложение Steam отменено оператором",
                     "cancelled_by_operator", order_id),
                )
            self._load_saved_orders()
            self._load_order_assets()
            self.statusBar().showMessage(
                f"Резерв заказа {order_id} освобождён после ручного подтверждения отмены предложения."
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "Не удалось снять резерв", str(exc))

    def _delete_selected_order(self):
        """Delete a local order only when no sent/unknown Steam offer can still be active."""
        try:
            order_id = self._selected_order_id()
            deletable_statuses = {
                "imported — ожидание подготовки",
                "reserved — не отправлен",
                "cancelled — резерв снят",
                "cancelled — отказ от заказа",
                "cancelled — предложение Steam отменено оператором",
            }
            with sqlite3.connect(self._database_path) as db:
                row = db.execute(
                    "SELECT status, source_platform FROM delivery_orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                reservation_count = db.execute(
                    "SELECT COUNT(*) FROM steam_asset_reservations WHERE order_id = ?",
                    (order_id,),
                ).fetchone()[0]
            if row is None:
                raise ValueError("Заказ не найден в базе данных.")
            status, platform = row
            if status not in deletable_statuses:
                raise ValueError(
                    "Нельзя удалить заказ с активным или неизвестным предложением Steam. "
                    "Сначала отмени/сверь предложение и сними резерв отдельным действием."
                )
            answer = QMessageBox.question(
                self,
                "Удалить заказ",
                f"Удалить заказ {order_id} из GameDelivery?\n"
                f"Источник: {'Playerok' if platform == 'playerok' else 'ручной'}\n"
                f"Активных резервов будет освобождено: {reservation_count}\n\n"
                "Будут удалены запись заказа и его локальный состав. "
                "Никаких действий в Steam или Playerok выполнено не будет. "
                "Удалённую сделку Playerok можно будет импортировать повторно.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            with sqlite3.connect(self._database_path) as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id = ?", (order_id,)
                ).fetchone()
                if current is None or current[0] != status or current[0] not in deletable_statuses:
                    raise ValueError("Статус заказа изменился. Обнови список и повтори удаление.")
                # A reserved-but-never-sent order is safe to delete; release orphan rows too.
                db.execute("DELETE FROM steam_asset_reservations WHERE order_id = ?", (order_id,))
                db.execute("DELETE FROM delivery_order_assets WHERE order_id = ?", (order_id,))
                db.execute("DELETE FROM delivery_orders WHERE order_id = ?", (order_id,))
            self._delete_trade_credential_best_effort(order_id)
            self._load_saved_orders()
            self._load_order_assets()
            self.statusBar().showMessage(f"Заказ {order_id} удалён из локального списка.")
        except (OSError, sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "Не удалось удалить заказ", str(exc))

    def _inventory_tab(self):
        return InventoryPanel(bot_selection_model=self.bot_selection_model)

    def _games_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Каталог игровых модулей")
        heading.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(heading)
        description = QLabel(
            "Выбери игру, чтобы увидеть состояние её модуля. Доступные функции "
            "открываются отдельно; незавершённые модули не влияют на текущую работу."
        )
        description.setObjectName("muted")
        description.setWordWrap(True)
        layout.addWidget(description)

        self.game_modules = [
            {
                "id": "cs2",
                "name": "Counter-Strike 2",
                "state": "В разработке",
                "details": "Модуль предметов и скинов Counter-Strike 2. "
                           "Инвентарь, качество, фильтры и торговые сценарии будут подключены отдельным этапом.",
                "available": False,
            },
            {
                "id": "dota2",
                "name": "Dota 2",
                "state": "В разработке",
                "details": "Модуль предметов Dota 2. Инвентарь и торговые сценарии ещё не подключены.",
                "available": False,
            },
            {
                "id": "steam_cards",
                "name": "Steam",
                "state": "Доступен · карточки",
                "details": "Текущий модуль Steam-карточек: просмотр снимков инвентаря, "
                           "выбор конкретных карточек, резервирование активов и подготовка заказов.",
                "available": True,
            },
            {
                "id": "tf2",
                "name": "Team Fortress 2",
                "state": "В разработке",
                "details": "Модуль предметов Team Fortress 2. Инвентарь и торговые сценарии ещё не подключены.",
                "available": False,
            },
            {
                "id": "rust",
                "name": "Rust",
                "state": "В разработке",
                "details": "Модуль предметов Rust. Инвентарь и торговые сценарии ещё не подключены.",
                "available": False,
            },
        ]
        self.game_search = QLineEdit()
        self.game_search.setPlaceholderText("Поиск игры...")
        self.game_search.textChanged.connect(self._filter_game_modules)
        layout.addWidget(self.game_search)

        content = QHBoxLayout()
        self.game_list = QListWidget()
        self.game_list.setMinimumWidth(240)
        self.game_list.currentItemChanged.connect(self._show_game_module_details)
        content.addWidget(self.game_list, 1)

        details_card = QFrame()
        details_card.setObjectName("card")
        details_layout = QVBoxLayout(details_card)
        self.game_module_name = QLabel("Выбери игру из списка")
        self.game_module_name.setStyleSheet("font-size: 16px; font-weight: 700;")
        self.game_module_state = QLabel("")
        self.game_module_state.setObjectName("muted")
        self.game_module_description = QLabel(
            "Здесь появится описание выбранного игрового модуля."
        )
        self.game_module_description.setWordWrap(True)
        details_layout.addWidget(self.game_module_name)
        details_layout.addWidget(self.game_module_state)
        details_layout.addWidget(self.game_module_description, 1)
        self.open_game_inventory_button = QPushButton("Открыть инвентарь Steam-карточек")
        self.open_game_inventory_button.setEnabled(False)
        self.open_game_inventory_button.clicked.connect(self._open_selected_game_inventory)
        details_layout.addWidget(self.open_game_inventory_button)
        content.addWidget(details_card, 2)
        layout.addLayout(content, 1)

        self._filter_game_modules()
        if self.game_list.count():
            self.game_list.setCurrentRow(0)
        return page

    def _filter_game_modules(self, text=""):
        query = str(text or "").strip().casefold()
        self.game_list.clear()
        for module in self.game_modules:
            if query and query not in module["name"].casefold():
                continue
            item = QListWidgetItem(f'{module["name"]}  ·  {module["state"]}')
            item.setData(Qt.ItemDataRole.UserRole, module["id"])
            self.game_list.addItem(item)
        if self.game_list.count():
            self.game_list.setCurrentRow(0)
        else:
            self._show_game_module_details(None, None)

    def _show_game_module_details(self, current, previous=None):
        module_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        module = next(
            (entry for entry in self.game_modules if entry["id"] == module_id),
            None,
        )
        if module is None:
            self.game_module_name.setText("Игра не найдена")
            self.game_module_state.setText("")
            self.game_module_description.setText("Измени поисковый запрос.")
            self.open_game_inventory_button.setEnabled(False)
            return
        self.game_module_name.setText(module["name"])
        self.game_module_state.setText(module["state"])
        self.game_module_description.setText(module["details"])
        self.open_game_inventory_button.setEnabled(module["available"])

    def _open_selected_game_inventory(self):
        current = self.game_list.currentItem()
        if current is None or current.data(Qt.ItemDataRole.UserRole) != "steam_cards":
            return
        for index in range(self.tabs.count()):
            if self.tabs.widget(index) is self.inventory_panel:
                self.tabs.setCurrentIndex(index)
                return

    def _logs_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Локальный журнал ASF и управления процессом"))
        toolbar.addStretch()
        open_folder = QPushButton("Открыть папку логов")
        open_folder.clicked.connect(self._open_log_directory)
        toolbar.addWidget(open_folder)
        refresh = QPushButton("Обновить журнал")
        refresh.clicked.connect(self._refresh_logs)
        toolbar.addWidget(refresh)
        layout.addLayout(toolbar)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText(
            "Журнал ASF и безопасная диагностика сетевых запросов появятся после использования."
        )
        layout.addWidget(self.log_view, 1)
        self._refresh_logs()
        return page

    def _refresh_logs(self):
        log_paths = (
            ("GameDelivery", get_log_directory() / "gamedelivery.log"),
            ("ASF / управление", Path.home() / ".gamedelivery" / "asf-supervisor.log"),
            ("OpenSkin / proxy", Path.home() / ".gamedelivery" / "openskin-proxy.log"),
        )
        try:
            sections = []
            for title, log_path in log_paths:
                if log_path.is_file():
                    lines = log_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    if lines:
                        sections.append(f"=== {title} ===\n" + "\n".join(lines[-1500:]))
            content = "\n\n".join(sections) or "Локальный журнал пока пуст."
            self.log_view.setPlainText(content)
        except OSError as exc:
            self.log_view.setPlainText(f"Не удалось прочитать локальный журнал: {exc}")

    def _open_log_directory(self):
        directory = get_log_directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
                raise OSError("Не удалось открыть папку диагностических журналов")
        except OSError as exc:
            log_exception(logger, "open diagnostics folder", exc)
            QMessageBox.warning(self, "Журналы GameDelivery", str(exc))

    def _append_log_line(self, line: str):
        self.log_view.append(line)

    def _settings_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Steam-аккаунты и прокси"))
        self.proxy_settings = ProxySettingsWidget()
        layout.addWidget(self.proxy_settings, 1)
        telegram = QLabel("Telegram: только уведомления и статистика.")
        telegram.setObjectName("muted")
        layout.addWidget(telegram)
        warning = QLabel(
            "Прокси можно применить через локальный ASF IPC. Для Steam CM приложение "
            "запросит отдельное подтверждение глобального SteamProtocols=4. Фактическая "
            "маршрутизация трафика и реальная отправка предметов пока не подтверждены."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #f2c879;")
        layout.addWidget(warning)
        return page


def main():
    log_directory = configure_logging()
    logger.info("GameDelivery starting; log_directory=%s", log_directory)
    app = QApplication(sys.argv)
    configure_openskin_diagnostics()
    window = MainWindow()
    window.show()
    logger.info("Main window shown")
    exit_code = app.exec()
    logger.info("GameDelivery stopped; exit_code=%s", exit_code)
    sys.exit(exit_code)
