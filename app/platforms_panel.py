"""Playerok browser and order-import panel."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
)
from app.playerok_browser import PlayerokBrowserPanel
from integrations.playerok.sync import PlayerokDealSyncService


class MarketplacePanel(QWidget):
    order_import_requested = Signal(object)

    def __init__(
        self,
        parent=None,
        *, database=None, sync_service: PlayerokDealSyncService | None = None,
    ):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        heading = QLabel("Playerok")
        heading.setStyleSheet("font-size: 20px; font-weight: 700;")
        root.addWidget(heading)
        self.status_label = QLabel(
            "Вход Playerok через браузер не подтверждён. Синхронизация через браузер будет реализована на следующем этапе."
        )
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("muted")
        root.addWidget(self.status_label)
        self.sync_service = sync_service
        self._sales_reader = None
        self._sales_sync_timeout = QTimer(self)
        self._sales_sync_timeout.setSingleShot(True)
        self._sales_sync_timeout.setInterval(60_000)
        self._sales_sync_timeout.timeout.connect(self._sales_read_timed_out)

        self.browser_panel = PlayerokBrowserPanel(database=database, parent=self)
        root.addWidget(self.browser_panel, 5)

        root.addWidget(QLabel("Заказы Playerok"))
        self.orders_table = QTableWidget(0, 8)
        self.orders_table.setHorizontalHeaderLabels([
            "Deal ID", "Chat ID", "Listing ID", "Товар", "Покупатель", "Статус", "Сумма", "Создано"
        ])
        self.orders_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.orders_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.orders_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.orders_table.itemSelectionChanged.connect(self._update_button_state)
        self.orders_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.orders_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.orders_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        self.orders_table.cellClicked.connect(self._open_selected_playerok_resource)
        root.addWidget(self.orders_table, 1)

        actions = QHBoxLayout()
        self.sync_sales_button = QPushButton("Синхронизировать продажи из браузера")
        self.sync_sales_button.clicked.connect(self._sync_sales)
        actions.addWidget(self.sync_sales_button)
        self.import_order_button = QPushButton("Добавить выбранный заказ в GameDelivery")
        self.import_order_button.setEnabled(False)
        self.import_order_button.clicked.connect(self._import_selected_order)
        actions.addWidget(self.import_order_button)
        actions.addStretch()
        root.addLayout(actions)
        self._update_button_state()

    def _sync_sales(self) -> None:
        if self._sales_reader is not None:
            return
        if self.sync_service is None:
            self.status_label.setText("Служба сохранения сделок не настроена.")
            return
        self.sync_sales_button.setEnabled(False)
        self.status_label.setText("Читаю продажи Playerok через сессию встроенного браузера…")
        try:
            self._sales_reader = self.browser_panel.read_sales(
                self._sales_read_completed,
                on_request_started=self._sales_sync_timeout.start,
            )
        except Exception as exc:
            self._sales_reader = None
            self.sync_sales_button.setEnabled(True)
            self.status_label.setText(str(exc))

    def _sales_read_timed_out(self) -> None:
        if self._sales_reader is not None:
            self._sales_reader.cancel("Истекло время ожидания ответа Playerok.")

    def _sales_read_completed(self, snapshot, error: str | None) -> None:
        self._sales_sync_timeout.stop()
        self._sales_reader = None
        self.sync_sales_button.setEnabled(True)
        if error or not isinstance(snapshot, dict):
            self.status_label.setText(error or "Не удалось получить полный снимок продаж.")
            return
        try:
            result = self.sync_service.sync_browser_snapshot(
                snapshot["account_user_id"], snapshot["deals"]
            )
        except Exception as exc:
            self.status_label.setText(f"Продажи не сохранены: {type(exc).__name__}.")
            return
        deals = result["deals"]
        self._show_orders(deals)
        if result["baseline"]:
            self.status_label.setText(
                f"Baseline завершён: {result['historical_deals']} старых сделок сохранено как исторические."
            )
        else:
            self.status_label.setText(
                f"Продажи синхронизированы: {len(deals)}. Новых сделок: {result['new_deals']}."
            )

    def _update_button_state(self) -> None:
        row = self.orders_table.currentRow()
        item = self.orders_table.item(row, 0) if row >= 0 else None
        order = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        self.import_order_button.setEnabled(
            isinstance(order, dict) and bool(order.get("order_id"))
        )

    def _import_selected_order(self) -> None:
        row = self.orders_table.currentRow()
        if row < 0:
            self.status_label.setText("Сначала выбери сделку в таблице.")
            return
        item = self.orders_table.item(row, 0)
        order = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(order, dict) or not order.get("order_id"):
            self.status_label.setText("Не удалось прочитать выбранную сделку.")
            return
        self.order_import_requested.emit(dict(order))
        self.status_label.setText(
            f"Передаю сделку {order['order_id']} в очередь GameDelivery. "
            "Итог импорта отображается в статусной строке главного окна."
        )

    def _show_orders(self, orders: list[dict[str, Any]]) -> None:
        self.orders_table.setRowCount(0)
        for order in orders:
            row = self.orders_table.rowCount()
            self.orders_table.insertRow(row)
            values = (
                order.get("deal_id") or order.get("order_id", ""),
                order.get("chat_id", ""),
                order.get("listing_id", ""),
                order.get("title", ""),
                order.get("buyer", ""),
                order.get("status", ""),
                order.get("amount", ""),
                order.get("created_at", ""),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, dict(order))
                    if order.get("deal_url") or order.get("url"):
                        cell.setToolTip(str(order.get("deal_url") or order["url"]))
                elif column == 1 and order.get("chat_url"):
                    cell.setToolTip(str(order["chat_url"]))
                self.orders_table.setItem(row, column, cell)
        self._update_button_state()

    def _open_selected_playerok_resource(self, row: int, column: int) -> None:
        item = self.orders_table.item(row, 0)
        order = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(order, dict):
            return
        try:
            if column == 1 and order.get("chat_id"):
                self.browser_panel.open_chat(str(order["chat_id"]))
            elif column == 0:
                deal_id = order.get("deal_id") or order.get("external_deal_id") or order.get("order_id")
                if deal_id:
                    self.browser_panel.open_deal(str(deal_id))
        except ValueError as exc:
            self.status_label.setText(str(exc))
