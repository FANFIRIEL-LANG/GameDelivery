"""Playerok browser and order-import panel."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
)
from app.playerok_browser import PlayerokBrowserPanel
from integrations.playerok import browser_sales
from integrations.playerok.browser_sales import (
    BrowserSalesSnapshot,
    is_trusted_browser_sales_snapshot,
)
from integrations.playerok.sync import PlayerokDealSyncService


class MarketplacePanel(QWidget):
    order_import_requested = Signal(object)
    browser_auth_status_changed = Signal(bool)

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
            "Статус входа Playerok не проверен. Синхронизация продаж через браузер будет реализована на следующем этапе."
        )
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("muted")
        root.addWidget(self.status_label)
        self.check_browser_auth_button = QPushButton("Проверить авторизацию Playerok")
        self.check_browser_auth_button.clicked.connect(self._check_browser_authentication)
        root.addWidget(self.check_browser_auth_button)
        self.sync_service = sync_service
        self._sales_reader = None
        self._diagnostic_sales_reader = None
        self._diagnostic_sales_starting = False
        self._diagnostic_reader_start_requested = False
        self._diagnostic_page_hostname = "unknown"
        self._sales_result_callback = None
        self._sales_expected_account_id = None
        self._sales_sync_timeout = QTimer(self)
        self._sales_sync_timeout.setSingleShot(True)
        self._sales_sync_timeout.setInterval(60_000)
        self._sales_sync_timeout.timeout.connect(self._sales_read_timed_out)
        self._diagnostic_sales_timeout = QTimer(self)
        self._diagnostic_sales_timeout.setSingleShot(True)
        self._diagnostic_sales_timeout.setInterval(60_000)
        self._diagnostic_sales_timeout.timeout.connect(self._diagnostic_sales_read_timed_out)

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
        self.diagnostic_sales_button = QPushButton("Диагностически прочитать продажи")
        self.diagnostic_sales_button.clicked.connect(self._diagnostic_read_sales)
        actions.addWidget(self.diagnostic_sales_button)
        self.import_order_button = QPushButton("Добавить выбранный заказ в GameDelivery")
        self.import_order_button.setEnabled(False)
        self.import_order_button.clicked.connect(self._import_selected_order)
        actions.addWidget(self.import_order_button)
        actions.addStretch()
        root.addLayout(actions)
        self.diagnostic_sales_report = QLabel("Диагностическое чтение продаж ещё не выполнялось.")
        self.diagnostic_sales_report.setWordWrap(True)
        self.diagnostic_sales_report.setObjectName("muted")
        root.addWidget(self.diagnostic_sales_report)
        self._update_button_state()
        self._update_playerok_read_controls()

    def _check_browser_authentication(self) -> None:
        if self._sales_reader is not None or self._diagnostic_sales_reader is not None or self._diagnostic_sales_starting:
            self.status_label.setText("Дождитесь завершения чтения продаж Playerok.")
            return
        self.check_browser_auth_button.setEnabled(False)
        self.status_label.setText("Проверяю авторизацию на текущей странице Playerok…")
        try:
            self.browser_panel.check_authentication(self._browser_authentication_checked)
            self._update_playerok_read_controls()
        except Exception:
            # Do not surface arbitrary exception text from the Qt/WebEngine boundary.
            self._browser_authentication_checked(
                False, "javascript_start_error: не удалось запустить проверку WebEngine."
            )

    def _browser_authentication_checked(self, authorized: bool, error: str | None) -> None:
        self.check_browser_auth_button.setEnabled(True)
        if authorized:
            self.status_label.setText(
                "Авторизация Playerok подтверждена. Синхронизация продаж через браузер будет подключена отдельно."
            )
        else:
            detail = error or "viewer_parse_error: ответ не подтвердил авторизацию."
            self.status_label.setText(f"Авторизация Playerok не подтверждена: {detail}")
        self.browser_auth_status_changed.emit(bool(authorized))
        self._update_playerok_read_controls()

    def _update_playerok_read_controls(self) -> None:
        sales_read_active = (
            self._sales_reader is not None
            or self._diagnostic_sales_reader is not None
            or self._diagnostic_sales_starting
        )
        busy = sales_read_active or self.browser_panel.viewer_check_active
        self.check_browser_auth_button.setEnabled(not busy)
        self.sync_sales_button.setEnabled(not busy)
        self.diagnostic_sales_button.setEnabled(not busy)

    def _sync_sales(self) -> None:
        self._start_sales_sync()

    def _diagnostic_read_sales(self) -> None:
        """Run BrowserSalesReader directly and report only safe aggregate diagnostics."""
        if (
            self._sales_reader is not None
            or self._diagnostic_sales_reader is not None
            or self._diagnostic_sales_starting
            or self.browser_panel.viewer_check_active
        ):
            self.diagnostic_sales_report.setText(
                "Диагностическое чтение не запущено: уже выполняется другая проверка или чтение."
            )
            return

        self._diagnostic_sales_starting = True
        self._diagnostic_reader_start_requested = False
        self._diagnostic_page_hostname = "unknown"
        self._update_playerok_read_controls()
        self.diagnostic_sales_report.setText(
            "Идёт read-only чтение продаж из текущей сессии браузера Playerok…"
        )
        self._diagnostic_sales_timeout.start()
        try:
            self.browser_panel.read_page_hostname(self._diagnostic_page_hostname_received)
        except Exception:
            self._diagnostic_page_hostname_received(None)

    def _diagnostic_page_hostname_received(self, value) -> None:
        if not self._diagnostic_sales_starting or self._diagnostic_reader_start_requested:
            return
        self._diagnostic_reader_start_requested = True
        self._diagnostic_page_hostname = self._safe_hostname(value)
        self._start_diagnostic_sales_reader()

    def _start_diagnostic_sales_reader(self) -> None:
        holder: dict[str, Any] = {"reader": None, "pending_result": None}

        def completed(snapshot, error):
            reader = holder["reader"]
            if reader is None:
                holder["pending_result"] = (snapshot, error)
                return
            self._finish_diagnostic_sales_read(reader, snapshot, error)

        try:
            reader = self.browser_panel.read_sales(
                completed,
                on_request_started=self._diagnostic_sales_timeout.start,
            )
        except Exception:
            self._diagnostic_sales_timeout.stop()
            self._diagnostic_sales_starting = False
            self._finish_diagnostic_sales_read(None, None, "diagnostic_read_error")
            return

        holder["reader"] = reader
        self._diagnostic_sales_reader = reader
        self._diagnostic_sales_starting = False
        pending_result = holder["pending_result"]
        self._update_playerok_read_controls()
        if pending_result is not None:
            self._finish_diagnostic_sales_read(reader, *pending_result)

    def _diagnostic_sales_read_timed_out(self) -> None:
        reader = self._diagnostic_sales_reader
        if reader is not None:
            reader.cancel(stage="timeout")
        elif self._diagnostic_sales_starting:
            self._finish_diagnostic_sales_read(None, None, "diagnostic_timeout")

    def _finish_diagnostic_sales_read(self, reader, snapshot, error) -> None:
        self._diagnostic_sales_timeout.stop()
        self._diagnostic_sales_starting = False
        self._diagnostic_reader_start_requested = False
        if reader is None or self._diagnostic_sales_reader is reader:
            self._diagnostic_sales_reader = None

        pages_read = max(0, int(getattr(reader, "pages_read", 0) or 0))
        deals_read = max(0, int(getattr(reader, "deals_read", 0) or 0))
        initial_viewer = bool(getattr(reader, "initial_viewer_received", False))
        pagination_complete = bool(getattr(reader, "pagination_complete", False))
        viewer_matched = getattr(reader, "viewer_matched", None) is True
        http_status = self._safe_http_status(
            getattr(reader, "last_graphql_http_status", None)
        )
        trusted = is_trusted_browser_sales_snapshot(snapshot)
        successful = (
            error is None
            and isinstance(snapshot, BrowserSalesSnapshot)
            and initial_viewer
            and pagination_complete
            and viewer_matched
            and trusted
        )

        lines = [
            f"Диагностическое чтение: {'успешно' if successful else 'не подтверждено'}",
            f"Страниц прочитано: {pages_read}",
            f"Сделок: {deals_read}",
            f"Page hostname: {self._diagnostic_page_hostname}",
            f"GraphQL hostname: {self._graphql_hostname()}",
            f"HTTP status: {http_status if http_status is not None else 'unknown'}",
            f"Начальный viewer: {'подтверждён' if initial_viewer else 'не подтверждён'}",
            f"Полная пагинация: {'да' if pagination_complete else 'нет'}",
            f"Viewer совпал: {'да' if viewer_matched else 'не подтверждено'}",
            f"Trusted snapshot: {'зарегистрирован' if trusted else 'нет'}",
        ]
        if not successful:
            lines.append(f"Ошибка: {self._safe_diagnostic_error_category(error, trusted)}")
        self.diagnostic_sales_report.setText("\n".join(lines))
        self._update_playerok_read_controls()

    @staticmethod
    def _safe_hostname(value) -> str:
        if not isinstance(value, str):
            return "unknown"
        hostname = value.strip().lower().rstrip(".")
        if (
            not hostname
            or len(hostname) > 253
            or re.fullmatch(r"[a-z0-9.-]+", hostname) is None
            or hostname.startswith(".")
            or ".." in hostname
        ):
            return "unknown"
        return hostname

    @classmethod
    def _graphql_hostname(cls) -> str:
        try:
            # BrowserSalesReader uses this same module-level endpoint to send Deals.
            return cls._safe_hostname(urlsplit(browser_sales._GRAPHQL_URL).hostname)
        except (TypeError, ValueError):
            return "unknown"

    @staticmethod
    def _safe_http_status(value) -> int | None:
        if type(value) is int and 100 <= value <= 599:
            return value
        return None

    @staticmethod
    def _safe_diagnostic_error_category(error, trusted: bool) -> str:
        if error is None:
            return "provenance_error" if not trusted else "incomplete_read"
        text = str(error)
        prefix = text.split(":", 1)[0]
        categories = {
            "javascript_start_error", "request_id_error", "poll_state_error", "fetch_error",
            "http_error", "json_parse_error", "viewer_parse_error", "diagnostic_timeout",
        }
        if prefix in categories:
            if prefix == "http_error":
                match = re.fullmatch(r"http_error: Playerok ответил HTTP ([0-9]{3})\.", text)
                if match is not None:
                    status_code = int(match.group(1))
                    if 100 <= status_code <= 599:
                        return f"http_error (HTTP {status_code})"
            return prefix
        lowered = text.casefold()
        if "аккаунт playerok изменился" in lowered:
            return "viewer_mismatch"
        if "viewer" in lowered or "аккаунт" in lowered:
            return "viewer_validation_error"
        if any(token in lowered for token in ("pageinfo", "totalcount", "курсор", "пагинац", "сделок не совпало")):
            return "pagination_error"
        if any(token in lowered for token in ("продаж", "сделк", "идентификатор", "запись продажи")):
            return "sales_page_error"
        return "read_error"

    def read_sales_for_delivery(self, expected_account_user_id: str, callback) -> None:
        """Read and persist one complete browser snapshot for a delivery gate."""
        if not callable(callback):
            raise TypeError("callback должен быть вызываемым")
        expected_account = str(expected_account_user_id or "").strip()
        if not expected_account:
            callback(None, "В заказе отсутствует подтверждённый Playerok account ID.")
            return
        if self._sales_reader is not None:
            callback(None, "Синхронизация Playerok уже выполняется.")
            return
        self._start_sales_sync(callback, expected_account_id=expected_account)

    def _start_sales_sync(self, result_callback=None, *, expected_account_id=None) -> None:
        if self._diagnostic_sales_reader is not None or self._diagnostic_sales_starting:
            message = "Диагностическое чтение продаж Playerok уже выполняется."
            self.status_label.setText(message)
            if callable(result_callback):
                result_callback(None, message)
            return
        if self.browser_panel.viewer_check_active:
            message = "Сначала дождитесь завершения проверки авторизации Playerok."
            self.status_label.setText(message)
            if callable(result_callback):
                result_callback(None, message)
            return
        if self._sales_reader is not None:
            if callable(result_callback):
                result_callback(None, "Синхронизация Playerok уже выполняется.")
            return
        if self.sync_service is None:
            self.status_label.setText("Служба сохранения сделок не настроена.")
            if callable(result_callback):
                result_callback(None, "Служба сохранения сделок не настроена.")
            return
        self._sales_result_callback = result_callback
        self._sales_expected_account_id = expected_account_id
        self.sync_sales_button.setEnabled(False)
        self.status_label.setText(
            "Проверяю статус сделки через браузер Playerok…"
            if callable(result_callback)
            else "Читаю продажи Playerok через сессию встроенного браузера…"
        )
        try:
            self._sales_reader = self.browser_panel.read_sales(
                self._sales_read_completed,
                on_request_started=self._sales_sync_timeout.start,
            )
            self._update_playerok_read_controls()
        except Exception as exc:
            self._sales_reader = None
            self._update_playerok_read_controls()
            self.status_label.setText(str(exc))
            callback, self._sales_result_callback = self._sales_result_callback, None
            self._sales_expected_account_id = None
            if callable(callback):
                callback(None, str(exc))

    def _sales_read_timed_out(self) -> None:
        if self._sales_reader is not None:
            self._sales_reader.cancel(stage="timeout")

    def _sales_read_completed(self, snapshot, error: str | None) -> None:
        self._sales_sync_timeout.stop()
        self._sales_reader = None
        self._update_playerok_read_controls()
        if error or not is_trusted_browser_sales_snapshot(snapshot):
            self.status_label.setText(error or "Не удалось получить полный снимок продаж.")
            callback, self._sales_result_callback = self._sales_result_callback, None
            self._sales_expected_account_id = None
            if callable(callback):
                callback(None, error or "Не удалось получить полный снимок продаж.")
            return
        expected_account = self._sales_expected_account_id
        if expected_account and str(snapshot.get("account_user_id") or "").strip() != expected_account:
            self.status_label.setText("Аккаунт браузера Playerok не совпадает с аккаунтом заказа.")
            callback, self._sales_result_callback = self._sales_result_callback, None
            self._sales_expected_account_id = None
            if callable(callback):
                callback(None, "Аккаунт браузера Playerok не совпадает с аккаунтом заказа.")
            return
        try:
            result = self.sync_service.sync_browser_snapshot(snapshot)
        except Exception as exc:
            self.status_label.setText(f"Продажи не сохранены: {type(exc).__name__}.")
            callback, self._sales_result_callback = self._sales_result_callback, None
            self._sales_expected_account_id = None
            if callable(callback):
                callback(None, f"Продажи не сохранены: {type(exc).__name__}.")
            return
        deals = result["deals"]
        account_id = str(snapshot.account_user_id).strip()
        self._show_orders([
            {**deal, "account_user_id": account_id}
            for deal in deals
        ])
        if result["baseline"]:
            self.status_label.setText(
                f"Baseline завершён: {result['historical_deals']} старых сделок сохранено как исторические."
            )
        else:
            self.status_label.setText(
                f"Продажи синхронизированы: {len(deals)}. Новых сделок: {result['new_deals']}."
            )
        callback, self._sales_result_callback = self._sales_result_callback, None
        self._sales_expected_account_id = None
        if callable(callback):
            callback(snapshot, None)

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
