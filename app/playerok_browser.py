from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, parse_qs, quote

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QSpinBox, QPlainTextEdit, QSplitter, QMessageBox,
)

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
    WEBENGINE_AVAILABLE = True
except Exception:  # CI/headless installs may not ship QtWebEngine.
    QWebEngineView = QWebEngineProfile = QWebEnginePage = None
    WEBENGINE_AVAILABLE = False


PLAYEROK_ORIGIN = "https://playerok.com"
PLAYEROK_FIXED_ROUTES = {"Чаты": "/chats"}
PLAYEROK_PROFILE_ROUTE_LABELS = (
    "Мои объявления",
    "Продажи: Оплаченные (PAID)",
    "Продажи: Отправленные (SENT)",
    "Продажи: Завершённые (CONFIRMED)",
    "Продажи: Возврат средств",
)


def playerok_profile_routes(username: str) -> dict[str, str]:
    if not isinstance(username, str) or not username.strip():
        raise ValueError("username текущего профиля Playerok недоступен.")
    seller = username.strip()
    if seller in {".", ".."}:
        raise ValueError("username текущего профиля Playerok некорректен.")
    seller = quote(seller, safe="")
    return {
        "Мои объявления": f"/profile/{seller}/products",
        "Продажи: Оплаченные (PAID)": f"/profile/{seller}/sales?status=paid",
        "Продажи: Отправленные (SENT)": f"/profile/{seller}/sales?status=sent",
        "Продажи: Завершённые (CONFIRMED)": f"/profile/{seller}/sales?status=confirmed",
        "Продажи: Возврат средств": f"/profile/{seller}/sales?status=rolled-back",
}


class _PlayerokProfileLookup(QThread):
    def __init__(self, adapter, parent=None):
        super().__init__(parent)
        self.adapter = adapter
        self.profile = None
        self.failed = False

    def run(self) -> None:
        try:
            self.profile = self.adapter.restore_session()
        except Exception:
            # Keep exception text and any transport details out of the UI.
            self.failed = True


def playerok_resource_url(resource: str, identifier: str) -> str:
    value = str(identifier or "").strip()
    if not value or "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("Некорректный идентификатор Playerok.")
    if resource == "deal":
        return f"{PLAYEROK_ORIGIN}/deal/{value}"
    if resource == "chat":
        return f"{PLAYEROK_ORIGIN}/chats/{value}"
    raise ValueError("Неизвестный тип ресурса Playerok.")


class PlayerokBrowserController:
    """Small navigation controller, independently testable without WebEngine."""

    def __init__(self, navigate):
        self._navigate = navigate

    def open_deal(self, deal_id: str) -> str:
        url = playerok_resource_url("deal", deal_id)
        self._navigate(url)
        return url

    def open_chat(self, chat_id: str) -> str:
        url = playerok_resource_url("chat", chat_id)
        self._navigate(url)
        return url

    def open_profile_route(self, username: str, route_label: str) -> str:
        path = playerok_profile_routes(username).get(route_label)
        if path is None:
            raise ValueError("Неизвестный быстрый маршрут профиля Playerok.")
        url = PLAYEROK_ORIGIN + path
        self._navigate(url)
        return url


class PlayerokBrowserPanel(QWidget):
    listing_config_saved = Signal(object)

    def __init__(self, *, database=None, parent=None, enable_webengine: bool | None = None):
        super().__init__(parent)
        self.database = database
        self.controller = PlayerokBrowserController(self.navigate)
        self._profile = None
        self._seller_username: str | None = None
        self._profile_lookup_thread: _PlayerokProfileLookup | None = None
        self._pending_profile_route: str | None = None
        self._profile_route_buttons: dict[str, QPushButton] = {}
        connection_signal = getattr(parent, "connection_status_changed", None)
        if connection_signal is not None and callable(getattr(connection_signal, "connect", None)):
            connection_signal.connect(self._invalidate_seller_username)
        root = QVBoxLayout(self)
        nav = QHBoxLayout()
        self.address = QLineEdit(PLAYEROK_ORIGIN)
        self.go_button = QPushButton("Перейти")
        self.go_button.clicked.connect(lambda: self.navigate(self.address.text().strip()))
        nav.addWidget(self.address, 1)
        nav.addWidget(self.go_button)
        root.addLayout(nav)
        quick = QHBoxLayout()
        for label in PLAYEROK_PROFILE_ROUTE_LABELS:
            button = QPushButton(label)
            button.clicked.connect(
                lambda checked=False, route_label=label: self._open_profile_route(route_label)
            )
            self._profile_route_buttons[label] = button
            quick.addWidget(button)
        for label, path in PLAYEROK_FIXED_ROUTES.items():
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, p=path: self.navigate(PLAYEROK_ORIGIN + p))
            quick.addWidget(button)
        root.addLayout(quick)

        split = QSplitter()
        self.browser_status = QLabel()
        headless_platform = os.environ.get("QT_QPA_PLATFORM", "").strip().lower() in {"offscreen", "minimal", "minimalegl"}
        use_webengine = (
            WEBENGINE_AVAILABLE and not headless_platform
            if enable_webengine is None
            else (WEBENGINE_AVAILABLE and enable_webengine)
        )
        if use_webengine:
            self.web = QWebEngineView()
            profile_dir = Path.home() / ".gamedelivery" / "playerok-web-profile"
            profile_dir.mkdir(parents=True, exist_ok=True)
            self._profile = QWebEngineProfile("GameDelivery-Playerok", self)
            self._profile.setPersistentStoragePath(str(profile_dir / "storage"))
            self._profile.setCachePath(str(profile_dir / "cache"))
            self._profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
            )
            page = QWebEnginePage(self._profile, self.web)
            self.web.setPage(page)
            self.web.urlChanged.connect(lambda u: self.address.setText(u.toString()))
            split.addWidget(self.web)
            self.browser_status.setText("Сессия браузера хранится отдельно в профиле GameDelivery.")
        else:
            self.web = None
            self.browser_status.setText(
                "QtWebEngine недоступен (headless/CI). Контроллер навигации и панель настроек доступны."
            )
            split.addWidget(self.browser_status)

        editor = QWidget()
        form = QVBoxLayout(editor)
        form.addWidget(QLabel("Привязка объявления → ConfigVersion"))
        self.listing_id = QLineEdit()
        self.listing_id.setPlaceholderText("listing_id Playerok")
        self.listing_title = QLineEdit()
        self.listing_title.setPlaceholderText("Название объявления")
        self.listing_url = QLineEdit()
        self.listing_url.setPlaceholderText("URL объявления")
        for field in (self.listing_id, self.listing_title, self.listing_url):
            form.addWidget(field)
        capture = QPushButton("Взять URL и данные из открытой страницы")
        capture.clicked.connect(self._capture_current_listing)
        form.addWidget(capture)
        self.mode = QComboBox()
        self.mode.addItem("Коллекционные карточки Steam (753/6)", "steam_cards")
        self.mode.addItem("Предметы CS2 (730/2)", "cs2_items")
        self.mode.addItem("Без Steam-обмена", "manual")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        form.addWidget(self.mode)
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 9999)
        self.quantity.setPrefix("Количество за покупку: ")
        form.addWidget(self.quantity)
        self.bots = QLineEdit()
        self.bots.setPlaceholderText("Разрешённые боты через запятую")
        form.addWidget(self.bots)
        self.selectors = QPlainTextEdit()
        self.selectors.setPlaceholderText('CS2 selectors JSON, например: [{"market_hash_name":"Кейс"}]')
        form.addWidget(self.selectors, 1)
        self.messages = QPlainTextEdit()
        self.messages.setPlaceholderText('Автосообщения JSON: {"paid":"...","sent":"...","confirmed":"..."}')
        form.addWidget(self.messages, 1)
        self.save_button = QPushButton("Сохранить новую версию ConfigVersion")
        self.save_button.clicked.connect(self._save_config)
        form.addWidget(self.save_button)
        split.addWidget(editor)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)
        self._mode_changed()

    def navigate(self, url: str) -> None:
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in {"playerok.com", "www.playerok.com"}:
            raise ValueError("Встроенный браузер разрешает только HTTPS Playerok.")
        if self.web is not None:
            self.web.setUrl(QUrl(url))
        self.address.setText(url)

    def open_deal(self, deal_id: str) -> str:
        return self.controller.open_deal(deal_id)

    def open_chat(self, chat_id: str) -> str:
        return self.controller.open_chat(chat_id)

    def _open_profile_route(self, route_label: str) -> None:
        if self._seller_username:
            self.controller.open_profile_route(self._seller_username, route_label)
            return
        if self._profile_lookup_thread is not None:
            return
        adapter = getattr(self.parent(), "playerok", None)
        if adapter is None or not callable(getattr(adapter, "restore_session", None)):
            self._profile_route_lookup_failed()
            return
        self._pending_profile_route = route_label
        self.browser_status.setText("Проверяю сохранённый профиль Playerok для быстрого перехода…")
        for button in self._profile_route_buttons.values():
            button.setEnabled(False)
        worker = _PlayerokProfileLookup(adapter, self)
        worker.finished.connect(lambda: self._profile_route_lookup_finished(worker))
        self._profile_lookup_thread = worker
        worker.start()

    def _profile_route_lookup_finished(self, worker: _PlayerokProfileLookup) -> None:
        if self._profile_lookup_thread is worker:
            self._profile_lookup_thread = None
        username = worker.profile.get("username") if isinstance(worker.profile, dict) else None
        route_label = self._pending_profile_route
        self._pending_profile_route = None
        if (
            not worker.failed
            and isinstance(username, str)
            and username.strip()
            and username.strip() != "Аккаунт Playerok"
            and route_label
        ):
            self._seller_username = username.strip()
            try:
                self.controller.open_profile_route(self._seller_username, route_label)
            except ValueError:
                self._seller_username = None
                self._profile_route_lookup_failed()
            else:
                self.browser_status.setText("Профиль Playerok подтверждён; быстрый переход выполнен.")
        else:
            self._profile_route_lookup_failed()
        for button in self._profile_route_buttons.values():
            button.setEnabled(True)
        worker.deleteLater()

    def _profile_route_lookup_failed(self) -> None:
        self.browser_status.setText(
            "Не удалось получить username из профиля Playerok. Быстрая ссылка не открыта; "
            "остальные функции браузера доступны."
        )

    def _invalidate_seller_username(self, *_args) -> None:
        self._seller_username = None

    def _capture_current_listing(self):
        current = self.address.text().strip()
        parsed = urlsplit(current)
        if parsed.scheme != "https" or parsed.hostname not in {"playerok.com", "www.playerok.com"}:
            self.browser_status.setText("Открой объявление Playerok во встроенном браузере.")
            return
        self.listing_url.setText(current)
        query_id = parse_qs(parsed.query).get("id", [""])[0]
        segments = [part for part in parsed.path.split("/") if part]
        candidate = query_id or (segments[-1] if len(segments) >= 2 and segments[-2].lower() in {"product", "products", "listing", "listings"} else "")
        if candidate:
            self.listing_id.setText(candidate)
        if self.web is not None and self.web.title():
            self.listing_title.setText(self.web.title())
        self.browser_status.setText("URL страницы скопирован. Проверь listing_id и название перед сохранением.")

    def _mode_changed(self, *_):
        mode = self.mode.currentData()
        self.quantity.setVisible(mode != "manual")
        self.bots.setVisible(mode != "manual")
        self.selectors.setVisible(mode == "cs2_items")
        self.messages.setVisible(mode == "manual")

    def _save_config(self):
        if self.database is None:
            QMessageBox.warning(self, "ConfigVersion", "Локальная база не передана панели.")
            return
        try:
            from integrations.playerok.listing_config import PlayerokListingConfigService
            mode = self.mode.currentData()
            selectors = json.loads(self.selectors.toPlainText() or "[]") if mode == "cs2_items" else None
            messages = json.loads(self.messages.toPlainText() or "{}") if mode == "manual" else None
            result = PlayerokListingConfigService(self.database).save(
                external_listing_id=self.listing_id.text(),
                title=self.listing_title.text(),
                url=self.listing_url.text(),
                mode=mode,
                quantity=self.quantity.value(),
                bot_pool=[x.strip() for x in self.bots.text().split(",") if x.strip()],
                selectors=selectors,
                auto_messages=messages,
            )
            self.listing_config_saved.emit(result)
            self.browser_status.setText(
                f"Сохранена неизменяемая ConfigVersion #{result['version_number']}."
            )
        except Exception as exc:
            QMessageBox.warning(self, "ConfigVersion", str(exc))
