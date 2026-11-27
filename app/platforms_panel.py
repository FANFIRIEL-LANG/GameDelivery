"""Playerok browser and order-import panel."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QRunnable, Qt, QSettings, Signal, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox, QSpinBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QInputDialog, QMessageBox,
)
from app.playerok_browser import PlayerokBrowserPanel
from integrations.playerok import browser_sales
from integrations.playerok.browser_sales import (
    BrowserSalesSnapshot,
    is_trusted_browser_sales_snapshot,
)
from integrations.playerok.sync import PlayerokDealSyncService
from integrations.playerok.sales_polling import PlayerokSalesPoller
from integrations.playerok.browser_chat_api import PlayerokBrowserChatAPI, WebEngineGraphQLTransport
from integrations.playerok.browser_chat_listener import PlayerokBrowserChatListener
from integrations.playerok.graphql_http import (
    PlayerokBrowserGraphQLSalesReader,
    is_fresh_trusted_graphql_sales_snapshot,
)


class _SnapshotSyncSignals(QObject):
    completed = Signal(object, object, object)


class _SnapshotSyncWorker(QRunnable):
    def __init__(self, sync_service, snapshot):
        super().__init__()
        self.sync_service = sync_service
        self.snapshot = snapshot
        self.account_user_id = snapshot.account_user_id
        self.signals = _SnapshotSyncSignals()

    def run(self):
        try:
            if isinstance(self.snapshot, BrowserSalesSnapshot):
                result = self.sync_service.sync_browser_snapshot(self.snapshot)
            else:
                result = self.sync_service.sync_graphql_snapshot(self.snapshot)
            self.signals.completed.emit(result, None, self.account_user_id)
        except Exception as exc:
            self.signals.completed.emit(None, type(exc).__name__, self.account_user_id)


class MarketplacePanel(QWidget):
    order_import_requested = Signal(object)
    browser_auth_status_changed = Signal(bool)
    playerok_chat_discovered = Signal(object)
    playerok_chat_updated = Signal(object)
    playerok_message_received = Signal(object)
    playerok_system_event = Signal(object)
    playerok_chat_connection_state = Signal(str)
    playerok_chat_subscription_event = Signal(str)

    PLAYEROK_AUTO_SYNC_KEY = "playerok/auto_sales_sync_enabled"
    PLAYEROK_POLL_INTERVAL_KEY = "playerok/sales_poll_interval_minutes"
    DEFAULT_POLL_INTERVAL_MINUTES = 5

    def __init__(
        self,
        parent=None,
        *, database=None, sync_service: PlayerokDealSyncService | None = None,
    ):
        super().__init__(parent)
        self.database = database or getattr(sync_service, "database", None)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        heading = QLabel("Playerok")
        heading.setStyleSheet("font-size: 20px; font-weight: 700;")
        root.addWidget(heading)
        self.status_label = QLabel(
            "Ожидаю загрузку и проверку сессии Playerok для read-only автообновления сделок."
        )
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("muted")
        root.addWidget(self.status_label)
        self.check_browser_auth_button = QPushButton("Проверить авторизацию Playerok")
        self.check_browser_auth_button.clicked.connect(self._check_browser_authentication)
        root.addWidget(self.check_browser_auth_button)
        self.chat_api_diagnostic_button = QPushButton("Проверить API чатов (read-only)")
        self.chat_api_diagnostic_button.clicked.connect(self._diagnose_chat_api)
        self.chat_api_diagnostic_button.setEnabled(False)
        root.addWidget(self.chat_api_diagnostic_button)
        self.chat_api_diagnostic_report = QLabel("Проверка API чатов ещё не выполнялась.")
        self.chat_api_diagnostic_report.setWordWrap(True)
        self.chat_api_diagnostic_report.setObjectName("muted")
        root.addWidget(self.chat_api_diagnostic_report)
        self.chat_ws_diagnostic_report = QLabel("Chat WebSocket: не запущен.")
        self.chat_ws_diagnostic_report.setWordWrap(True)
        self.chat_ws_diagnostic_report.setObjectName("muted")
        root.addWidget(self.chat_ws_diagnostic_report)
        self._chat_ws_event_counts = {"chatUpdated": 0, "chatMessageCreated": 0}
        self._chat_ws_last_state = "not_started"
        self._chat_diagnostic_generation = 0
        self.sync_service = sync_service
        self._chat_transport = None
        self._chat_api = None
        self._chat_listener = None
        self._playerok_auth_confirmed = False
        self._settings = QSettings("FANFIRIEL-LANG", "GameDelivery")
        self._sales_reader = None
        self._diagnostic_sales_reader = None
        self._diagnostic_sales_starting = False
        self._diagnostic_reader_start_requested = False
        self._diagnostic_page_hostname = "unknown"
        self._sales_result_callback = None
        self._sales_expected_account_id = None
        self._poll_sync_worker_active = False
        self._poll_sync_callback = None
        self._poll_sync_worker = None
        self._graphql_read_worker = None
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

        auto_sync_enabled = self._settings.value(self.PLAYEROK_AUTO_SYNC_KEY, True, type=bool)
        poll_interval = self._settings.value(
            self.PLAYEROK_POLL_INTERVAL_KEY, self.DEFAULT_POLL_INTERVAL_MINUTES, type=int
        )
        if not 1 <= poll_interval <= 60:
            poll_interval = self.DEFAULT_POLL_INTERVAL_MINUTES

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
        self.sync_sales_button = QPushButton("Синхронизировать сделки через GraphQL")
        self.sync_sales_button.clicked.connect(self._sync_sales)
        actions.addWidget(self.sync_sales_button)
        self.auto_sales_sync_checkbox = QCheckBox("Автоматически обновлять сделки")
        self.auto_sales_sync_checkbox.setChecked(auto_sync_enabled)
        actions.addWidget(self.auto_sales_sync_checkbox)
        actions.addWidget(QLabel("Интервал, мин:"))
        self.sales_poll_interval = QSpinBox()
        self.sales_poll_interval.setRange(1, 60)
        self.sales_poll_interval.setValue(poll_interval)
        self.sales_poll_interval.setToolTip("Период полного read-only чтения списка сделок Playerok.")
        actions.addWidget(self.sales_poll_interval)
        self.diagnostic_sales_button = QPushButton("Диагностически прочитать продажи")
        self.diagnostic_sales_button.clicked.connect(self._diagnostic_read_sales)
        actions.addWidget(self.diagnostic_sales_button)
        self.import_order_button = QPushButton("Добавить выбранный заказ в GameDelivery")
        self.import_order_button.setEnabled(False)
        self.import_order_button.clicked.connect(self._import_selected_order)
        actions.addWidget(self.import_order_button)
        self.bind_paid_config_button = QPushButton("Выбрать ConfigVersion для PAID")
        self.bind_paid_config_button.setEnabled(False)
        self.bind_paid_config_button.clicked.connect(self._bind_selected_paid_config)
        actions.addWidget(self.bind_paid_config_button)
        actions.addStretch()
        root.addLayout(actions)
        self.diagnostic_sales_report = QLabel("Диагностическое чтение продаж ещё не выполнялось.")
        self.diagnostic_sales_report.setWordWrap(True)
        self.diagnostic_sales_report.setObjectName("muted")
        root.addWidget(self.diagnostic_sales_report)

        self._sales_poll_timer = QTimer(self)
        self._sales_poller = PlayerokSalesPoller(
            timer=self._sales_poll_timer,
            reader_factory=self._start_graphql_poll_reader,
            sync_snapshot=self._dispatch_poll_snapshot_sync,
            session_available=lambda: (
                self._playerok_auth_confirmed and self.browser_panel.sales_session_available()
            ),
            externally_busy=self._sales_read_busy,
            status_changed=self._sales_poll_status_changed,
            interval_ms=poll_interval * 60_000,
        )
        self.auto_sales_sync_checkbox.toggled.connect(self._set_auto_sales_sync_enabled)
        self.sales_poll_interval.valueChanged.connect(self._set_sales_poll_interval)
        self.browser_auth_status_changed.connect(self._on_browser_auth_status_changed)
        self.playerok_chat_connection_state.connect(self._chat_ws_connection_changed)
        self.playerok_chat_subscription_event.connect(self._chat_ws_event_received)
        if self.browser_panel.web is not None:
            self.browser_panel.web.loadStarted.connect(self._on_sales_page_load_started)
            self.browser_panel.web.loadFinished.connect(self._on_sales_page_load_finished)
            self.browser_panel.web.urlChanged.connect(self._on_sales_page_url_changed)
            self.browser_panel.clear_session_button.clicked.connect(self._on_browser_session_clearing)
        application = QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self._stop_sales_polling)
        self._update_button_state()
        self._update_playerok_read_controls()

    def _sales_read_busy(self) -> bool:
        return bool(
            self._sales_reader is not None
            or self._graphql_read_worker is not None
            or self._diagnostic_sales_reader is not None
            or self._diagnostic_sales_starting
            or self.browser_panel.viewer_check_active
            or self._poll_sync_worker_active
        )

    def _start_poll_reader(self, completed):
        if self.sync_service is None:
            raise RuntimeError("Playerok sync service is not configured.")

        reader_ref = [None]

        def reader_completed(snapshot, error):
            self._sales_sync_timeout.stop()
            if self._sales_reader is reader_ref[0]:
                self._sales_reader = None
            self._update_playerok_read_controls()
            completed(snapshot, error)

        reader = self.browser_panel.read_sales(
            reader_completed,
            on_request_started=self._sales_sync_timeout.start,
        )
        reader_ref[0] = reader
        self._sales_reader = reader
        self._update_playerok_read_controls()
        return reader

    def _start_graphql_poll_reader(self, completed, *, expected_account_user_id=None):
        if self.sync_service is None:
            raise RuntimeError("Playerok sync service is not configured.")
        if self._graphql_read_worker is not None:
            raise RuntimeError("GraphQL sales read is already running.")
        holder: dict[str, Any] = {"reader": None, "pending": None}

        def reader_completed(snapshot, error):
            reader = holder["reader"]
            if reader is None:
                holder["pending"] = (snapshot, error)
                return
            self._graphql_sales_read_completed(reader, snapshot, error, completed)

        try:
            reader = self.browser_panel.read_graphql_sales_snapshot(
                reader_completed, expected_account_user_id=expected_account_user_id
            )
        except RuntimeError as exc:
            raise RuntimeError(str(exc) if str(exc) in {
                "browser_session_unavailable", "browser_profile_unavailable"
            } else "browser_session_unavailable") from None
        holder["reader"] = reader
        self._graphql_read_worker = reader
        self._update_playerok_read_controls()
        pending = holder["pending"]
        if pending is not None:
            self._graphql_sales_read_completed(reader, *pending, completed)
        return reader

    def _graphql_sales_read_completed(self, reader, snapshot, error, completed):
        if self._graphql_read_worker is not reader:
            return
        self._graphql_read_worker = None
        self._update_playerok_read_controls()
        completed(snapshot, error)

    def _dispatch_poll_snapshot_sync(self, snapshot, completed) -> None:
        if self.sync_service is None:
            completed(None, "SyncServiceUnavailable")
            return
        self._poll_sync_worker_active = True
        self._poll_sync_callback = completed
        worker = _SnapshotSyncWorker(self.sync_service, snapshot)
        self._poll_sync_worker = worker
        worker.signals.completed.connect(self._poll_snapshot_sync_completed)
        self._update_playerok_read_controls()
        QThreadPool.globalInstance().start(worker)

    def _poll_snapshot_sync_completed(self, result, error, account_user_id) -> None:
        self._poll_sync_worker_active = False
        self._poll_sync_worker = None
        callback, self._poll_sync_callback = self._poll_sync_callback, None
        if error is None and isinstance(result, dict):
            deals = result.get("deals", [])
            self._show_orders([
                {**deal, "account_user_id": str(account_user_id or "")}
                for deal in deals if isinstance(deal, dict)
            ])
        if callable(callback):
            callback(result, error)
        self._update_playerok_read_controls()

    def _set_auto_sales_sync_enabled(self, enabled: bool) -> None:
        self._settings.setValue(self.PLAYEROK_AUTO_SYNC_KEY, bool(enabled))
        if not enabled:
            self._sales_sync_timeout.stop()
            self._sales_poller.stop("disabled")
            return
        if self.sync_service is not None and self.browser_panel.sales_session_available():
            self._check_browser_authentication()

    def _set_sales_poll_interval(self, minutes: int) -> None:
        if type(minutes) is not int or not 1 <= minutes <= 60:
            return
        self._settings.setValue(self.PLAYEROK_POLL_INTERVAL_KEY, minutes)
        self._sales_poller.set_interval(minutes * 60_000)

    def _on_browser_auth_status_changed(self, authorized: bool) -> None:
        self._playerok_auth_confirmed = bool(authorized)
        if not authorized:
            self._stop_chat_listener()
            if self._sales_poller.active:
                self._sales_poller.stop("session_lost")
            return
        self._start_chat_listener()
        if (
            authorized and self.sync_service is not None
            and self.auto_sales_sync_checkbox.isChecked()
            and self.browser_panel.sales_session_available()
        ):
            self._sales_poller.start()

    def _on_sales_page_load_finished(self, loaded: bool) -> None:
        if not loaded:
            self._sales_sync_timeout.stop()
            self._sales_poller.stop("session_lost")
            self._stop_chat_listener()
            return
        if self.browser_panel.sales_session_available() and (
            self._chat_listener is None or not self._chat_listener.active
            or (self.auto_sales_sync_checkbox.isChecked() and self.sync_service is not None
                and not self._sales_poller.active)
        ):
            self._check_browser_authentication()

    def _on_sales_page_load_started(self) -> None:
        self._stop_chat_listener()

    def _on_sales_page_url_changed(self, _url) -> None:
        if self._sales_poller.active and not self.browser_panel.sales_session_available():
            self._sales_sync_timeout.stop()
            self._sales_poller.stop("session_lost")
        if not self.browser_panel.sales_session_available():
            self._stop_chat_listener()

    def _on_browser_session_clearing(self) -> None:
        self._sales_sync_timeout.stop()
        self._sales_poller.stop("session_lost")
        self._stop_chat_listener()

    def _stop_sales_polling(self) -> None:
        self._sales_sync_timeout.stop()
        self._sales_poller.stop("stopped")
        self._stop_chat_listener()

    def _start_chat_listener(self) -> None:
        if self._chat_listener is not None and self._chat_listener.active:
            return
        if not self.browser_panel.sales_session_available():
            return
        transport = WebEngineGraphQLTransport(
            self.browser_panel._run_current_page_javascript,
            schedule=lambda callback: QTimer.singleShot(50, callback),
        )
        api = PlayerokBrowserChatAPI(transport)
        listener = PlayerokBrowserChatListener(
            api, self.browser_panel._run_current_page_javascript, self,
        )
        listener.chat_discovered.connect(self.playerok_chat_discovered.emit)
        listener.chat_updated.connect(self.playerok_chat_updated.emit)
        listener.message_received.connect(self.playerok_message_received.emit)
        listener.system_event.connect(self.playerok_system_event.emit)
        listener.connection_state.connect(self.playerok_chat_connection_state.emit)
        listener.subscription_event.connect(self.playerok_chat_subscription_event.emit)
        self._chat_transport = transport
        self._chat_api = api
        self._chat_listener = listener
        listener.start()

    def _stop_chat_listener(self) -> None:
        listener, self._chat_listener = self._chat_listener, None
        if listener is not None:
            listener.stop()
        self._chat_api = None
        self._chat_transport = None
        self.chat_api_diagnostic_button.setEnabled(False)

    @staticmethod
    def _chat_page_counts():
        return {"pages": 0, "cursor_pages": 0, "error": None}

    @staticmethod
    def _chat_page_observer(counts):
        def observed(has_cursor, error):
            counts["pages"] += 1
            counts["cursor_pages"] += int(bool(has_cursor))
            if error:
                counts["error"] = error if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(error)) else "api_error"
        return observed

    @staticmethod
    def _chat_id_from_path(path: str) -> str | None:
        if type(path) is not str:
            return None
        match = re.fullmatch(r"/chats/([^/\s?#]{1,256})/?", path)
        return match.group(1) if match else None

    def _diagnose_chat_api(self) -> None:
        api = self._chat_api
        listener = self._chat_listener
        if api is None and self._playerok_auth_confirmed and self.browser_panel.sales_session_available():
            transport = WebEngineGraphQLTransport(
                self.browser_panel._run_current_page_javascript,
                schedule=lambda callback: QTimer.singleShot(50, callback),
            )
            api = PlayerokBrowserChatAPI(transport)
            self._chat_transport = transport
            self._chat_api = api
        if api is None or not (
            (listener is not None and listener.account_user_id)
            or (self._playerok_auth_confirmed and self.browser_panel.sales_session_available())
        ):
            self.chat_api_diagnostic_report.setText("BLOCKED: авторизованный chat API недоступен.")
            self.chat_api_diagnostic_button.setEnabled(False)
            return
        if self._sales_read_busy() or self._sales_poller.busy:
            self.chat_api_diagnostic_report.setText("Ожидание завершения текущего чтения Playerok.")
            return
        if self.browser_panel.web is None:
            self.chat_api_diagnostic_report.setText("BLOCKED: WebEngine-страница недоступна.")
            return
        path = urlsplit(self.browser_panel.web.url().toString()).path
        chat_id = self._chat_id_from_path(path)
        if not chat_id:
            parts = tuple(part for part in path.split("/") if part)
            if not parts:
                shape = "root_path"
            elif parts == ("chats",):
                shape = "chats_collection_path"
            elif parts[0] == "chats":
                shape = "chat_path_id_unrecognized"
            else:
                shape = "non_chat_path"
            self.chat_api_diagnostic_report.setText(
                f"BLOCKED: откройте доступный чат в текущей вкладке (route={shape})."
            )
            return
        self._chat_diagnostic_generation += 1
        generation = self._chat_diagnostic_generation
        chats_pages = self._chat_page_counts()
        messages_pages = self._chat_page_counts()
        self.chat_api_diagnostic_button.setEnabled(False)
        self.chat_api_diagnostic_report.setText("Выполняется read-only проверка списка чатов и сообщений…")

        def complete_if_current(text):
            if generation != self._chat_diagnostic_generation:
                return
            self.chat_api_diagnostic_report.setText(text)
            self._update_playerok_read_controls()

        results = {"chats": None, "chat": None, "messages": None}
        chat_count = 0

        def render_if_complete():
            if any(value is None for value in results.values()):
                return
            complete_if_current(
                f"chats={results['chats']} ({chat_count}), pages={chats_pages['pages']}, "
                f"cursor_pages={chats_pages['cursor_pages']}; chat={results['chat']}; "
                f"messages={results['messages']}; pages={messages_pages['pages']}, "
                f"cursor_pages={messages_pages['cursor_pages']}; content=not displayed"
            )

        def chats_ready(chats, error):
            nonlocal chat_count
            if error or chats is None:
                results["chats"] = "FAIL(" + self._safe_chat_api_error(error) + ")"
            else:
                chat_count = len(chats)
                results["chats"] = "PASS"
            render_if_complete()

        def chat_ready(chat, error):
            if error or chat is None:
                results["chat"] = "FAIL(" + self._safe_chat_api_error(error) + ")"
            elif chat.id != chat_id:
                results["chat"] = "FAIL(chat_identity_mismatch)"
            else:
                results["chat"] = "PASS"
            render_if_complete()

        def messages_ready(messages, error):
            if error or messages is None:
                results["messages"] = "FAIL(" + self._safe_chat_api_error(error) + ")"
            elif not all(message.chat_id == chat_id for message in messages):
                results["messages"] = f"FAIL(chat_id_mismatch,{len(messages)})"
            else:
                results["messages"] = f"PASS({len(messages)})"
            render_if_complete()

        def run_reads(user_id):
            api.get_chats(
                user_id,
                chats_ready,
                page_observer=self._chat_page_observer(chats_pages),
            )
            api.get_chat(chat_id, chat_ready)
            api.get_chat_messages(
                chat_id,
                messages_ready,
                page_observer=self._chat_page_observer(messages_pages),
            )
            self._start_chat_ws_diagnostic(api, user_id, chat_id)

        account_id = listener.account_user_id if listener is not None else None
        if account_id:
            run_reads(account_id)
        else:
            api.get_viewer(lambda user_id, error: (
                complete_if_current("BLOCKED: авторизованный viewer недоступен.")
                if error or not user_id else run_reads(user_id)
            ))

    def _start_chat_ws_diagnostic(self, api, user_id: str, chat_id: str) -> None:
        listener = self._chat_listener
        if listener is None:
            listener = PlayerokBrowserChatListener(
                api, self.browser_panel._run_current_page_javascript, self,
            )
            listener.connection_state.connect(self.playerok_chat_connection_state.emit)
            listener.subscription_event.connect(self.playerok_chat_subscription_event.emit)
            self._chat_listener = listener
        if not listener.active:
            listener.start_for_read_only_diagnostic(user_id, chat_id)

    @staticmethod
    def _safe_chat_api_error(error) -> str:
        if isinstance(error, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error):
            return error
        return "api_error"

    def _chat_ws_connection_changed(self, state: str) -> None:
        safe_state = state if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", state or "") else "unknown"
        if safe_state != "stopped":
            self._chat_ws_last_state = safe_state
        displayed_state = (
            f"stopped after {self._chat_ws_last_state}" if safe_state == "stopped" else safe_state
        )
        self.chat_ws_diagnostic_report.setText(
            f"Chat WebSocket: {displayed_state}; chatUpdated events={self._chat_ws_event_counts['chatUpdated']}; "
            f"chatMessageCreated events={self._chat_ws_event_counts['chatMessageCreated']}"
        )

    def _chat_ws_event_received(self, event_name: str) -> None:
        if event_name not in self._chat_ws_event_counts:
            return
        self._chat_ws_event_counts[event_name] += 1
        self._chat_ws_connection_changed("event_observed")

    def read_playerok_chat_messages(self, chat_id: str, callback) -> None:
        if self._chat_api is None or self._chat_listener is None or not self._chat_listener.active:
            callback(None, "session_unavailable")
            return
        self._chat_api.get_chat_messages(chat_id, callback)

    def read_playerok_chats(self, callback) -> None:
        if self._chat_api is None or self._chat_listener is None or not self._chat_listener.active:
            callback(None, "session_unavailable")
            return
        self._chat_api.get_chats(self._chat_listener.account_user_id, callback)

    def read_playerok_chat(self, chat_id: str, callback) -> None:
        if self._chat_api is None or self._chat_listener is None or not self._chat_listener.active:
            callback(None, "session_unavailable")
            return
        self._chat_api.get_chat(chat_id, callback)

    def send_playerok_chat_text(self, chat_id: str, text: str, callback) -> None:
        if self._chat_api is None or self._chat_listener is None or not self._chat_listener.active:
            callback(None, "session_unavailable")
            return
        self._chat_api.send_text_message(chat_id, text, callback)

    def _sales_poll_status_changed(self, state: str, detail) -> None:
        if state == "success" and isinstance(detail, dict):
            if detail.get("baseline"):
                message = f"Автосинхронизация: baseline сохранён ({detail.get('historical_deals', 0)} исторических сделок)."
            else:
                message = (
                    f"Автосинхронизация: сделок {len(detail.get('deals', []))}, "
                    f"новых {detail.get('new_deals', 0)}."
                )
            self.status_label.setText(message)
        elif state == "read_error":
            self.status_label.setText(
                f"Автосинхронизация: чтение не завершено; данные не изменены. Следующая попытка через "
                f"{self.sales_poll_interval.value()} мин. {detail or ''}"
            )
        elif state == "sync_error":
            self.status_label.setText(
                f"Автосинхронизация: снимок не сохранён ({detail}); последняя сохранённая выборка оставлена."
            )
        elif state == "session_lost":
            self.status_label.setText("Автосинхронизация остановлена: страница или авторизация Playerok недоступна.")
        elif state == "account_changed":
            self.status_label.setText("Автосинхронизация остановлена: изменился аккаунт Playerok.")
        elif state == "disabled":
            self.status_label.setText("Автоматическое обновление сделок выключено.")
        elif state == "started":
            self.status_label.setText(
                f"Автосинхронизация сделок включена; интервал {self.sales_poll_interval.value()} мин."
            )
        self._update_playerok_read_controls()

    def _check_browser_authentication(self) -> None:
        if (
            self._sales_reader is not None or self._diagnostic_sales_reader is not None
            or self._diagnostic_sales_starting or self._poll_sync_worker_active
        ):
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
                "Авторизация Playerok подтверждена. Используется текущая сессия встроенного браузера."
            )
        else:
            detail = error or "viewer_parse_error: ответ не подтвердил авторизацию."
            self.status_label.setText(f"Авторизация Playerok не подтверждена: {detail}")
        self.browser_auth_status_changed.emit(bool(authorized))
        self._update_playerok_read_controls()

    def _update_playerok_read_controls(self) -> None:
        sales_read_active = (
            self._sales_reader is not None
            or self._graphql_read_worker is not None
            or self._diagnostic_sales_reader is not None
            or self._diagnostic_sales_starting
            or self._poll_sync_worker_active
            or (getattr(self, "_sales_poller", None) is not None and self._sales_poller.busy)
        )
        busy = sales_read_active or self.browser_panel.viewer_check_active
        self.check_browser_auth_button.setEnabled(not busy)
        self.sync_sales_button.setEnabled(not busy)
        self.diagnostic_sales_button.setEnabled(not busy)
        chat_ready = bool(
            (self._chat_api is not None and self._chat_listener is not None
             and self._chat_listener.account_user_id)
            or (self._playerok_auth_confirmed and self.browser_panel.sales_session_available())
        )
        self.chat_api_diagnostic_button.setEnabled(chat_ready and not busy)

    def _sync_sales(self) -> None:
        if self._sales_read_busy():
            self.status_label.setText("Синхронизация Playerok уже выполняется.")
            return
        self.status_label.setText("Читаю все страницы сделок Playerok через GraphQL…")
        try:
            self._start_graphql_poll_reader(self._manual_graphql_snapshot_ready)
        except RuntimeError as exc:
            messages = {
                "browser_session_unavailable": "Откройте авторизованную страницу Playerok во встроенном браузере.",
                "browser_profile_unavailable": "Профиль встроенного браузера Playerok недоступен.",
            }
            self.status_label.setText(
                "GraphQL-синхронизация не запущена: "
                + messages.get(str(exc), "не удалось подготовить браузерное чтение.")
            )

    def _manual_graphql_snapshot_ready(self, snapshot, error):
        if error or snapshot is None:
            if error == "session_expired":
                message = "Сессия Playerok истекла. Проверь авторизацию и повтори синхронизацию."
            elif error in {"page_lost", "timeout"}:
                message = "Страница Playerok недоступна; данные не изменены. Откройте страницу и повторите чтение."
            else:
                message = f"GraphQL-снимок не получен полностью ({error or 'read_error'}); данные не изменены."
            self.status_label.setText(message)
            return

        def synced(result, sync_error):
            if sync_error or not isinstance(result, dict):
                self.status_label.setText(
                    f"Сделки не сохранены ({sync_error or 'sync_error'}); предыдущие данные сохранены."
                )
                return
            deals = result.get("deals", [])
            self._show_orders([
                {**deal, "account_user_id": snapshot.account_user_id}
                for deal in deals if isinstance(deal, dict)
            ])
            if result.get("baseline"):
                self.status_label.setText(
                    f"Baseline завершён: {result.get('historical_deals', 0)} исторических сделок."
                )
            else:
                self.status_label.setText(
                    f"GraphQL-синхронизация завершена: получено {len(deals)}, "
                    f"новых {result.get('new_deals', 0)}."
                )

        self._dispatch_poll_snapshot_sync(snapshot, synced)

    def _diagnostic_read_sales(self) -> None:
        """Read sales through GraphQL and report only safe aggregate diagnostics."""
        if (
            self._sales_reader is not None
            or self._graphql_read_worker is not None
            or self._diagnostic_sales_reader is not None
            or self._diagnostic_sales_starting
            or self._poll_sync_worker_active
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
            reader = self.browser_panel.read_graphql_sales_snapshot(completed)
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
        trusted = is_fresh_trusted_graphql_sales_snapshot(snapshot)
        successful = (
            error is None
            and trusted
            and initial_viewer
            and pagination_complete
            and viewer_matched
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
        if self._sales_read_busy():
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
        if self._sales_reader is not None or self._graphql_read_worker is not None or self._poll_sync_worker_active:
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
            self._sales_sync_timeout.start()
            self._start_graphql_poll_reader(
                self._sales_read_completed,
                expected_account_user_id=expected_account_id,
            )
            self._update_playerok_read_controls()
        except Exception as exc:
            self._sales_sync_timeout.stop()
            self._sales_reader = None
            self._update_playerok_read_controls()
            self.status_label.setText(str(exc))
            callback, self._sales_result_callback = self._sales_result_callback, None
            self._sales_expected_account_id = None
            if callable(callback):
                callback(None, str(exc))

    def _sales_read_timed_out(self) -> None:
        reader = self._graphql_read_worker or self._sales_reader
        if reader is not None:
            reader.cancel(stage="timeout")

    def _sales_read_completed(self, snapshot, error: str | None) -> None:
        self._sales_sync_timeout.stop()
        self._sales_reader = None
        self._update_playerok_read_controls()
        if error or not is_fresh_trusted_graphql_sales_snapshot(snapshot):
            self.status_label.setText(error or "Не удалось получить полный снимок продаж.")
            callback, self._sales_result_callback = self._sales_result_callback, None
            self._sales_expected_account_id = None
            if callable(callback):
                callback(None, error or "Не удалось получить полный снимок продаж.")
            return
        expected_account = self._sales_expected_account_id
        if expected_account and str(snapshot.account_user_id or "").strip() != expected_account:
            self.status_label.setText("Аккаунт браузера Playerok не совпадает с аккаунтом заказа.")
            callback, self._sales_result_callback = self._sales_result_callback, None
            self._sales_expected_account_id = None
            if callable(callback):
                callback(None, "Аккаунт браузера Playerok не совпадает с аккаунтом заказа.")
            return
        try:
            result = self.sync_service.sync_graphql_snapshot(snapshot)
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
            isinstance(order, dict) and bool(
                order.get("order_id") or order.get("paid_config_binding_id")
                or (str(order.get("status") or "").upper() == "PAID" and order.get("config_version_id"))
            )
        )
        self.bind_paid_config_button.setEnabled(
            self.sync_service is not None
            and isinstance(order, dict)
            and str(order.get("status") or "").upper() == "PAID"
            and bool(order.get("account_user_id"))
            and bool(order.get("deal_id") or order.get("external_deal_id"))
            and not order.get("config_version_id")
        )

    def _bind_selected_paid_config(self) -> None:
        row = self.orders_table.currentRow()
        item = self.orders_table.item(row, 0) if row >= 0 else None
        deal_data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(deal_data, dict) or self.sync_service is None:
            self.status_label.setText("Выбери актуальную PAID-сделку.")
            return
        account_id = str(deal_data.get("account_user_id") or "").strip()
        external_id = str(deal_data.get("deal_id") or deal_data.get("external_deal_id") or "").strip()
        try:
            versions = self.sync_service.list_config_versions_for_paid_deal(
                account_user_id=account_id, external_deal_id=external_id
            )
            if not versions:
                raise ValueError("Для объявления нет сохранённых ConfigVersion.")
            labels = [
                f"v{value['version_number']} — {value['config_version_id']}"
                for value in versions
            ]
            selected, accepted = QInputDialog.getItem(
                self, "ConfigVersion оплаченной сделки",
                "Выбери точную версию, подтверждённую для момента оплаты:",
                labels, 0, False,
            )
            if not accepted:
                return
            selected_index = labels.index(selected)
            reason, accepted = QInputDialog.getText(
                self, "Аудит выбора ConfigVersion",
                "Причина подтверждения версии:",
            )
            if not accepted:
                return
            result = self.sync_service.bind_paid_deal_config_version(
                account_user_id=account_id,
                external_deal_id=external_id,
                config_version_id=versions[selected_index]["config_version_id"],
                operator_id="desktop-operator",
                reason=reason,
            )
            self.status_label.setText(
                f"ConfigVersion {result['config_version_id']} закреплена. "
                "Заказ будет создан атомарно вместе с manifest и резервом при подготовке."
            )
            deal_data["config_version_id"] = result["config_version_id"]
            deal_data["order_id"] = result["order_id"]
            deal_data["paid_config_binding_id"] = result["binding_id"]
            item.setData(Qt.ItemDataRole.UserRole, dict(deal_data))
            self._update_button_state()
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось закрепить ConfigVersion", str(exc))
            self.status_label.setText(f"Привязка ConfigVersion не выполнена: {exc}")

    def _import_selected_order(self) -> None:
        row = self.orders_table.currentRow()
        if row < 0:
            self.status_label.setText("Сначала выбери сделку в таблице.")
            return
        item = self.orders_table.item(row, 0)
        order = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(order, dict) or not (
            order.get("order_id") or order.get("paid_config_binding_id")
            or (str(order.get("status") or "").upper() == "PAID" and order.get("config_version_id"))
        ):
            self.status_label.setText("Не удалось прочитать выбранную сделку.")
            return
        self.order_import_requested.emit(dict(order))
        self.status_label.setText(
            f"Передаю сделку {order.get('deal_id', order.get('order_id', ''))} "
            "на атомарную подготовку в GameDelivery. Итог отобразится в статусной строке главного окна."
        )

    def _show_orders(self, orders: list[dict[str, Any]]) -> None:
        self.orders_table.setRowCount(0)
        for order in orders:
            if (
                str(order.get("platform") or "").casefold() == "playerok"
                and str(order.get("status") or "").upper() == "PAID"
                and self.database is not None
                and order.get("account_user_id")
                and (order.get("deal_id") or order.get("external_deal_id"))
            ):
                with self.database.connect() as db:
                    binding = db.execute(
                        "SELECT b.binding_id,b.config_version_id FROM gd_deals d "
                        "JOIN playerok_paid_config_bindings b ON b.deal_id=d.deal_id "
                        "AND b.account_user_id=d.playerok_account_id "
                        "WHERE d.source_platform='playerok' AND d.playerok_account_id=? "
                        "AND d.external_deal_id=?",
                        (str(order["account_user_id"]), str(order.get("deal_id") or order.get("external_deal_id"))),
                    ).fetchone()
                if binding is not None:
                    order = {
                        **order,
                        "config_version_id": str(binding["config_version_id"]),
                        "paid_config_binding_id": str(binding["binding_id"]),
                    }
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
