import sys
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QFrame,
    QLineEdit, QComboBox, QListWidget, QListWidgetItem, QMessageBox,
)
from games.steam_cards.snapshot import InventorySnapshotStore
from games.steam_cards.reservations import AssetReservationStore
from app.inventory_panel import InventoryPanel
from app.proxy_settings import ProxySettingsWidget
from app.asf_management import ASFManagementWidget
from app.platforms_panel import MarketplacePanel


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GameDelivery — локальная панель")
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
        self.tabs.addTab(self._orders_tab(), "Заказы")
        self.marketplace_panel = MarketplacePanel()
        self.marketplace_panel.connection_status_changed.connect(self.platform_status.setText)
        self.marketplace_panel.order_import_requested.connect(self._import_marketplace_order)
        self.tabs.addTab(self.marketplace_panel, "Площадки")
        self.inventory_panel = self._inventory_tab()
        self.tabs.addTab(self.inventory_panel, "Инвентарь")
        self.tabs.addTab(self._games_tab(), "Модули игр")
        self.tabs.addTab(self._logs_tab(), "Журнал")
        self.tabs.addTab(self._settings_tab(), "Настройки")
        self.asf_management = ASFManagementWidget()
        self.asf_management.inventory_snapshot_updated.connect(self.inventory_panel.select_bot_snapshot)
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

    def _orders_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "Подготовка доставки: выбери бота и конкретные активы из локального снимка ASF. "
            "Резервирование локальное; обмены не отправляются."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QHBoxLayout()
        self.order_recipient = QLineEdit()
        self.order_recipient.setPlaceholderText("HTTPS trade URL получателя")
        self.order_bot = QComboBox()
        self.order_bot.setMinimumWidth(160)
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
        actions.addWidget(self.prepare_order_button)
        actions.addStretch()
        layout.addLayout(actions)

        AssetReservationStore(self._database_path)
        self._ensure_delivery_order_schema()
        self.orders = QTableWidget(0, 5)
        self.orders.setHorizontalHeaderLabels(["ID заказа", "Источник", "Предметы", "Статус", "Обновлено"])
        self.orders.horizontalHeader().setStretchLastSection(True)
        self.orders.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.orders.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.orders.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.orders.cellDoubleClicked.connect(self._show_order_details)
        layout.addWidget(self.orders, 2)
        order_actions = QHBoxLayout()
        self.order_details_button = QPushButton("Состав заказа")
        self.order_details_button.clicked.connect(self._show_selected_order_details)
        self.cancel_order_button = QPushButton("Отменить заказ и снять резерв")
        self.cancel_order_button.clicked.connect(self._cancel_selected_order)
        order_actions.addWidget(self.order_details_button)
        order_actions.addWidget(self.cancel_order_button)
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
                    source_buyer TEXT NOT NULL DEFAULT ''
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

    def _import_marketplace_order(self, marketplace_order):
        platform = str(marketplace_order.get("platform") or "").strip().lower()
        external_id = str(marketplace_order.get("order_id") or "").strip()
        title = str(marketplace_order.get("title") or "Заказ с торговой площадки").strip()
        buyer = str(marketplace_order.get("buyer") or "").strip()
        if platform != "playerok" or not external_id:
            self.statusBar().showMessage("Не удалось импортировать заказ: нет поддерживаемого источника или ID.")
            return
        local_order_id = f"GD-{uuid4().hex[:12].upper()}"
        try:
            self._ensure_delivery_order_schema()
            with sqlite3.connect(self._database_path) as db:
                existing = db.execute(
                    "SELECT order_id FROM delivery_orders WHERE source_platform = ? AND source_order_id = ?",
                    (platform, external_id),
                ).fetchone()
                if existing:
                    self.statusBar().showMessage(
                        f"Сделка {external_id} уже импортирована как {existing[0]}."
                    )
                    self.tabs.setCurrentIndex(0)
                    return
                db.execute("""
                    INSERT INTO delivery_orders
                        (order_id, recipient_trade_url, status, asset_count,
                         source_platform, source_order_id, source_title, source_buyer)
                    VALUES (?, '', 'imported — ожидание подготовки', 0, ?, ?, ?, ?)
                """, (local_order_id, platform, external_id, title, buyer))
            self._load_saved_orders()
            self.tabs.setCurrentIndex(0)
            for row in range(self.orders.rowCount()):
                if self.orders.item(row, 0).text() == local_order_id:
                    self.orders.selectRow(row)
                    break
            self.statusBar().showMessage(
                f"Сделка Playerok {external_id} добавлена как {local_order_id}. "
                "Укажи trade URL покупателя и выбери активы для подготовки."
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
                    "SELECT asset_id, game_name, card_name, is_trading_card, is_foil "
                    "FROM steam_inventory_snapshot WHERE bot_name = ? ORDER BY game_name, card_name, asset_id",
                    (bot,),
                ).fetchall()
                existing = db.execute(
                    "SELECT asset_id FROM steam_asset_reservations WHERE bot_name = ?", (bot,)
                ).fetchall()
            reserved_ids = {row[0] for row in existing}
            for asset_id, game, name, is_card, is_foil in rows:
                kind = "Foil" if is_foil else ("Карточка" if is_card else "Предмет")
                label = f"{game} — {name} [{kind}] · asset {asset_id}"
                if asset_id in reserved_ids:
                    label += " · УЖЕ ЗАРЕЗЕРВИРОВАН"
                    item = QListWidgetItem(label)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                else:
                    item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, (bot, asset_id))
                self.order_assets.addItem(item)
            if not rows:
                self.order_assets.addItem("В снимке нет предметов.")
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

            order_id = f"GD-{uuid4().hex[:12].upper()}"
            reservations = AssetReservationStore(self._database_path)
            result = reservations.reserve(order_id, selected)
            if not result["reserved"]:
                raise ValueError("Конфликт резерва: " + ", ".join(
                    f"{x['bot_name']} / {x['asset_id']} → {x['reserved_by']}"
                    for x in result["conflicts"]
                ))

            try:
                with sqlite3.connect(self._database_path) as db:
                    db.execute("""
                        CREATE TABLE IF NOT EXISTS delivery_orders (
                            order_id TEXT PRIMARY KEY,
                            recipient_trade_url TEXT NOT NULL,
                            status TEXT NOT NULL,
                            asset_count INTEGER NOT NULL,
                            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                        )
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
                    db.execute(
                        "INSERT INTO delivery_orders (order_id, recipient_trade_url, status, asset_count) "
                        "VALUES (?, ?, 'reserved — не отправлен', ?)",
                        (order_id, recipient, len(selected)),
                    )
                    db.executemany(
                        "INSERT INTO delivery_order_assets (order_id, bot_name, asset_id) VALUES (?, ?, ?)",
                        [(order_id, bot_name, asset_id) for bot_name, asset_id in selected],
                    )
            except Exception:
                reservations.release(order_id)
                raise

            self._load_saved_orders()
            self._load_order_assets()
            QMessageBox.information(
                self, "Заказ подготовлен",
                f"Заказ {order_id} создан. Активов зарезервировано: {len(selected)}. "
                "Обмен не отправлялся."
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "Не удалось подготовить заказ", str(exc))

    def _load_saved_orders(self):
        if not hasattr(self, "orders"):
            return
        self.orders.setRowCount(0)
        try:
            with sqlite3.connect(self._database_path) as db:
                rows = db.execute(
                    "SELECT order_id, recipient_trade_url, asset_count, status, created_at "
                    "FROM delivery_orders ORDER BY created_at DESC"
                ).fetchall()
            for order_id, recipient, count, status, created in rows:
                row = self.orders.rowCount()
                self.orders.insertRow(row)
                for col, value in enumerate((order_id, "Steam trade URL", str(count), status, created)):
                    self.orders.setItem(row, col, QTableWidgetItem(str(value)))
        except sqlite3.Error as exc:
            self.statusBar().showMessage(f"Не удалось загрузить сохранённые заказы: {exc}")

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
                "SELECT recipient_trade_url, status FROM delivery_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            assets = db.execute(
                "SELECT bot_name, asset_id FROM delivery_order_assets WHERE order_id = ? "
                "ORDER BY bot_name, asset_id",
                (order_id,),
            ).fetchall()
        if order is None:
            raise ValueError("Заказ не найден в базе данных.")
        lines = [f"Заказ: {order_id}", f"Статус: {order[1]}", f"Trade URL: {order[0]}", "", "Активы:"]
        lines.extend(f"• {bot} — asset {asset}" for bot, asset in assets)
        if not assets:
            lines.append("Состав не сохранён (старый заказ).")
        QMessageBox.information(self, "Состав заказа", "\\n".join(lines))

    def _cancel_selected_order(self):
        try:
            order_id = self._selected_order_id()
            with sqlite3.connect(self._database_path) as db:
                row = db.execute(
                    "SELECT status FROM delivery_orders WHERE order_id = ?", (order_id,)
                ).fetchone()
            if row is None:
                raise ValueError("Заказ не найден в базе данных.")
            if row[0] != "reserved — не отправлен":
                raise ValueError("Снять резерв можно только у заказа в статусе «reserved — не отправлен».")
            answer = QMessageBox.question(
                self, "Отменить заказ",
                f"Отменить {order_id} и освободить его активы? Это не отправляет обмен.",
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
                if current is None or current[0] != "reserved — не отправлен":
                    raise ValueError("Статус заказа изменился. Обнови список и повтори действие.")
                db.execute(
                    "UPDATE delivery_orders SET status = 'cancelled — резерв снят' WHERE order_id = ?",
                    (order_id,),
                )
                db.execute(
                    "DELETE FROM steam_asset_reservations WHERE order_id = ?", (order_id,)
                )
            self._load_saved_orders()
            self._load_order_assets()
            self.statusBar().showMessage(f"Заказ {order_id} отменён; резерв снят.")
        except (OSError, sqlite3.Error, ValueError) as exc:
            QMessageBox.warning(self, "Не удалось отменить заказ", str(exc))

    def _inventory_tab(self):
        return InventoryPanel()

    def _games_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        for name, state in [
            ("Steam-карточки", "Первый модуль · планируется"),
            ("Counter-Strike 2", "Скины и предметы · планируется"),
            ("Другие игры", "Подключаемые модули · будущий этап"),
        ]:
            card = QFrame()
            card.setObjectName("card")
            row = QHBoxLayout(card)
            name_label = QLabel(name)
            name_label.setStyleSheet("font-weight: 600;")
            state_label = QLabel(state)
            state_label.setObjectName("muted")
            row.addWidget(name_label)
            row.addStretch()
            row.addWidget(state_label)
            layout.addWidget(card)
        layout.addStretch()
        return page

    def _logs_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        log = QTextEdit()
        log.setReadOnly(True)
        log.setPlainText("GameDelivery: тестовый режим.\nPlayerok не подключён.\nSteam API не используется.\nРеальная отправка предметов отключена.")
        layout.addWidget(log)
        return page

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
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
