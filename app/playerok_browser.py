from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, parse_qs, quote

from PySide6.QtCore import QUrl, Signal
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


def playerok_profile_root() -> Path:
    return Path.home() / ".gamedelivery" / "playerok-web-profile"


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
        self._clear_pending = False
        self._page_storage_clear_ok = False
        self._profile_route_buttons: dict[str, QPushButton] = {}
        root = QVBoxLayout(self)
        nav = QHBoxLayout()
        self.back_button = QPushButton("Назад")
        self.forward_button = QPushButton("Вперёд")
        self.reload_button = QPushButton("Обновить")
        self.home_button = QPushButton("Домой")
        self.clear_session_button = QPushButton("Очистить данные сайта")
        for button in (self.back_button, self.forward_button, self.reload_button, self.home_button, self.clear_session_button):
            nav.addWidget(button)
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
            profile_dir = playerok_profile_root()
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
            self.web.loadStarted.connect(lambda: self.browser_status.setText("Загрузка Playerok…"))
            self.web.loadFinished.connect(self._load_finished)
            self.back_button.clicked.connect(self.web.back)
            self.forward_button.clicked.connect(self.web.forward)
            self.reload_button.clicked.connect(self.web.reload)
            self.home_button.clicked.connect(lambda: self.navigate(PLAYEROK_ORIGIN + "/"))
            self.clear_session_button.clicked.connect(self.clear_browser_session)
            split.addWidget(self.web)
            self.browser_status.setText("Войдите на сайте Playerok. Сессия хранится локально в профиле браузера.")
            self.navigate(PLAYEROK_ORIGIN + "/")
        else:
            self.web = None
            for button in (self.back_button, self.forward_button, self.reload_button):
                button.setEnabled(False)
            self.home_button.clicked.connect(lambda: self.navigate(PLAYEROK_ORIGIN + "/"))
            self.clear_session_button.setEnabled(False)
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
        self.browser_status.setText("Username профиля не получен из браузерной страницы. Откройте нужный раздел через сайт.")

    def _load_finished(self, ok: bool) -> None:
        self.browser_status.setText(
            "Браузер открыт; вход Playerok не подтверждён."
            if ok else "Не удалось загрузить страницу Playerok."
        )

    def clear_browser_session(self) -> bool:
        if self._profile is None:
            self.browser_status.setText("Профиль WebEngine недоступен в этом режиме.")
            return False
        answer = QMessageBox.question(
            self, "Очистить сессию Playerok",
            "Очистить Web Storage и HTTP-кэш Playerok и запросить удаление cookies? "
            "Qt не сообщает о завершении удаления cookies, поэтому полное завершение выхода подтвердить нельзя.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._clear_pending = True
        self.clear_session_button.setEnabled(False)
        self.browser_status.setText("Очищаю Web Storage и HTTP-кэш; отправляю запрос на удаление cookies…")
        self._profile.clearHttpCacheCompleted.connect(self._finish_browser_clear)
        self.web.page().runJavaScript(
            "(() => { try { localStorage.clear(); sessionStorage.clear(); return true; } catch (_) { return false; } })()",
            self._page_storage_cleared,
        )
        return True

    def _page_storage_cleared(self, cleared: bool) -> None:
        if not self._clear_pending:
            return
        self._page_storage_clear_ok = bool(cleared)
        self._profile.cookieStore().deleteAllCookies()
        self._profile.clearHttpCache()

    def _finish_browser_clear(self) -> None:
        if not self._clear_pending:
            return
        self._profile.clearHttpCacheCompleted.disconnect(self._finish_browser_clear)
        self._clear_pending = False
        old_page = self.web.page()
        self.web.setPage(QWebEnginePage(self._profile, self.web))
        old_page.deleteLater()
        self.web.setUrl(QUrl("about:blank"))
        self.address.clear()
        self.clear_session_button.setEnabled(True)
        if not self._page_storage_clear_ok:
            self.browser_status.setText(
                "HTTP-кэш очищен; запрос на удаление cookies отправлен. Очистку Web Storage подтвердить не удалось, "
                "а удаление cookies и прочих persistent site data не подтверждено."
            )
        else:
            self.browser_status.setText(
                "Web Storage и HTTP-кэш очищены; запрос на удаление cookies отправлен. "
                "Qt не подтверждает завершение удаления cookies/прочих persistent site data, поэтому полный выход не подтверждён."
            )

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
