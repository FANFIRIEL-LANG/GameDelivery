"""Playerok connection and order-import panel.

Network calls are kept off the Qt UI thread. The panel communicates through
the generic marketplace adapter contract so other marketplaces can be added.
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QFrame, QMessageBox,
)
from integrations.marketplaces import MarketplaceRegistry
from integrations.playerok.client import PlayerokAdapter


class _MarketplaceTask(QThread):
    completed = Signal(str, bool, object)

    def __init__(self, action: str, operation: Callable[[], Any], parent=None):
        super().__init__(parent)
        self.action = action
        self.operation = operation

    def run(self) -> None:
        try:
            self.completed.emit(self.action, True, self.operation())
        except Exception as exc:
            self.completed.emit(self.action, False, str(exc) or exc.__class__.__name__)


class MarketplacePanel(QWidget):
    connection_status_changed = Signal(str)
    order_import_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.registry = MarketplaceRegistry()
        self.registry.register(PlayerokAdapter())
        self.playerok = self.registry.get("playerok")
        self._worker: _MarketplaceTask | None = None
        self._connected = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        heading = QLabel("Подключение торговой площадки")
        heading.setStyleSheet("font-size: 20px; font-weight: 700;")
        description = QLabel(
            "Вход в Playerok по коду из e-mail. Сессионный токен хранится в системном "
            "хранилище учётных данных, не в базе заказов. Клиент Playerok сторонний и "
            "неофициальный; после авторизации сначала только читаются активные сделки."
        )
        description.setWordWrap(True)
        description.setObjectName("muted")
        root.addWidget(heading)
        root.addWidget(description)

        connection_card = QFrame()
        connection_card.setObjectName("card")
        form = QVBoxLayout(connection_card)
        form.setSpacing(8)
        self.profile_label = QLabel("Аккаунт не подключён")
        self.profile_label.setStyleSheet("font-weight: 600;")
        form.addWidget(self.profile_label)

        email_row = QHBoxLayout()
        self.email_input = QLineEdit()
        self.email_input.setPlaceholderText("E-mail аккаунта Playerok")
        self.send_code_button = QPushButton("Получить код")
        self.send_code_button.clicked.connect(self._request_code)
        email_row.addWidget(self.email_input, 1)
        email_row.addWidget(self.send_code_button)
        form.addLayout(email_row)

        code_row = QHBoxLayout()
        self.email_code_input = QLineEdit()
        self.email_code_input.setPlaceholderText("Код из письма")
        self.email_code_input.setMaxLength(32)
        self.confirm_code_button = QPushButton("Подтвердить код")
        self.confirm_code_button.clicked.connect(self._confirm_email_code)
        code_row.addWidget(self.email_code_input, 1)
        code_row.addWidget(self.confirm_code_button)
        form.addLayout(code_row)

        two_factor_row = QHBoxLayout()
        self.two_factor_input = QLineEdit()
        self.two_factor_input.setPlaceholderText("Код 2FA, если запрошен")
        self.two_factor_input.setMaxLength(32)
        self.two_factor_button = QPushButton("Подтвердить 2FA")
        self.two_factor_button.clicked.connect(self._confirm_second_factor)
        two_factor_row.addWidget(self.two_factor_input, 1)
        two_factor_row.addWidget(self.two_factor_button)
        form.addLayout(two_factor_row)

        action_row = QHBoxLayout()
        self.restore_button = QPushButton("Проверить сохранённый вход")
        self.restore_button.clicked.connect(self._restore_session)
        self.disconnect_button = QPushButton("Выйти из Playerok")
        self.disconnect_button.clicked.connect(self._disconnect)
        self.fetch_orders_button = QPushButton("Загрузить активные заказы")
        self.fetch_orders_button.clicked.connect(self._fetch_orders)
        self.import_order_button = QPushButton("Добавить выбранный заказ в GameDelivery")
        self.import_order_button.clicked.connect(self._import_selected_order)
        action_row.addWidget(self.restore_button)
        action_row.addWidget(self.disconnect_button)
        action_row.addWidget(self.fetch_orders_button)
        action_row.addWidget(self.import_order_button)
        form.addLayout(action_row)
        root.addWidget(connection_card)

        self.status_label = QLabel("Готово к подключению.")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.orders_table = QTableWidget(0, 6)
        self.orders_table.setHorizontalHeaderLabels([
            "ID сделки", "Товар", "Покупатель", "Статус", "Сумма", "Создано"
        ])
        self.orders_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.orders_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.orders_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.orders_table.itemSelectionChanged.connect(self._update_button_state)
        self.orders_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.orders_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.orders_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(QLabel("Активные продажи Playerok (только чтение):"))
        root.addWidget(self.orders_table, 1)

        if self.playerok.has_saved_session():
            self.profile_label.setText("Найдена сохранённая сессия — проверь вход")
            self.status_label.setText("Сессия найдена в системном хранилище. Проверь её перед загрузкой заказов.")
        self._update_button_state()

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self.send_code_button, self.confirm_code_button, self.two_factor_button,
            self.restore_button, self.disconnect_button, self.fetch_orders_button,
            self.import_order_button,
        ):
            widget.setEnabled(not busy)
        if not busy:
            self._update_button_state()

    def _update_button_state(self) -> None:
        running = self._worker is not None and self._worker.isRunning()
        for widget in (
            self.send_code_button, self.confirm_code_button, self.two_factor_button,
            self.restore_button, self.disconnect_button,
        ):
            widget.setEnabled(not running)
        self.fetch_orders_button.setEnabled(not running and self._connected)
        self.import_order_button.setEnabled(
            not running and self._connected and self.orders_table.currentRow() >= 0
        )
        self.two_factor_button.setEnabled(
            not running and bool(getattr(self.playerok, "_pending_second_factor_session", None))
        )
        self.disconnect_button.setEnabled(not running and self._connected)

    def _start_task(self, action: str, operation: Callable[[], Any], message: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.status_label.setText(message)
        self._set_busy(True)
        worker = _MarketplaceTask(action, operation, self)
        worker.completed.connect(self._task_completed)
        worker.finished.connect(self._worker_finished)
        self._worker = worker
        worker.start()

    def _worker_finished(self) -> None:
        worker = self.sender()
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        self._update_button_state()

    def _request_code(self) -> None:
        email = self.email_input.text().strip()
        self._start_task(
            "request_code",
            lambda: self.playerok.request_login_code(email),
            "Запрашиваю код Playerok…",
        )

    def _confirm_email_code(self) -> None:
        email = self.email_input.text().strip()
        code = self.email_code_input.text().strip()
        self._start_task(
            "confirm_code",
            lambda: self.playerok.confirm_login_code(email, code),
            "Проверяю код…",
        )

    def _confirm_second_factor(self) -> None:
        code = self.two_factor_input.text().strip()
        self._start_task(
            "confirm_2fa",
            lambda: self.playerok.confirm_second_factor(code),
            "Проверяю второй фактор…",
        )

    def _restore_session(self) -> None:
        self._start_task(
            "restore",
            self.playerok.restore_session,
            "Проверяю сохранённую сессию Playerok…",
        )

    def _disconnect(self) -> None:
        if QMessageBox.question(
            self, "Выход из Playerok",
            "Завершить сессию и удалить её из системного хранилища?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._start_task("disconnect", self.playerok.disconnect, "Завершаю сессию…")

    def _fetch_orders(self) -> None:
        self._start_task(
            "orders",
            self.playerok.fetch_orders,
            "Загружаю активные продажи Playerok…",
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
        self.status_label.setText(f"Заказ {order['order_id']} передан в очередь GameDelivery.")

    def _task_completed(self, action: str, ok: bool, payload: Any) -> None:
        self._set_busy(False)
        if not ok:
            self.status_label.setText(f"Ошибка: {payload}")
            return

        if action == "request_code":
            self.status_label.setText(f"Код отправлен на {payload['email']}. Проверь почту.")
        elif action == "confirm_code" and payload.get("requires_second_factor"):
            self.status_label.setText("Код принят. Для завершения входа введи код 2FA.")
            self.two_factor_input.setFocus()
        elif action in {"confirm_code", "confirm_2fa", "restore"} and payload.get("connected"):
            self._connected = True
            username = payload.get("username") or "Аккаунт Playerok"
            self.profile_label.setText(f"Подключено: {username}")
            if payload.get("session_saved"):
                self.status_label.setText("Вход выполнен. Сессия сохранена в системном хранилище.")
            else:
                self.status_label.setText(
                    "Вход выполнен только на время работы приложения: системное хранилище "
                    "недоступно, после перезапуска придётся войти снова."
                )
            self.connection_status_changed.emit(f"Playerok: подключён · {username}")
            self._update_button_state()
        elif action == "disconnect":
            self._connected = False
            self.profile_label.setText("Аккаунт не подключён")
            self.status_label.setText(payload.get("message", "Сессия удалена."))
            self.orders_table.setRowCount(0)
            self.connection_status_changed.emit("Playerok: не подключён")
            self._update_button_state()
        elif action == "orders":
            self._show_orders(payload)
            self.status_label.setText(f"Загружено активных сделок: {len(payload)}.")
        self._update_button_state()

    def _show_orders(self, orders: list[dict[str, Any]]) -> None:
        self.orders_table.setRowCount(0)
        for order in orders:
            row = self.orders_table.rowCount()
            self.orders_table.insertRow(row)
            values = (
                order.get("order_id", ""),
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
                    if order.get("url"):
                        cell.setToolTip(str(order["url"]))
                self.orders_table.setItem(row, column, cell)
