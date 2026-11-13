from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
PLAYEROK_ROUTES = {
    "Мои объявления": "/products",
    "Продажи: Оплаченные (PAID)": "/sales?status=PAID",
    "Продажи: Отправленные (SENT)": "/sales?status=SENT",
    "Продажи: Завершённые (CONFIRMED)": "/sales?status=CONFIRMED",
    "Чаты": "/chats",
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


class PlayerokBrowserPanel(QWidget):
    listing_config_saved = Signal(object)

    def __init__(self, *, database=None, parent=None, enable_webengine: bool | None = None):
        super().__init__(parent)
        self.database = database
        self.controller = PlayerokBrowserController(self.navigate)
        self._profile = None
        root = QVBoxLayout(self)
        nav = QHBoxLayout()
        self.address = QLineEdit(PLAYEROK_ORIGIN)
        self.go_button = QPushButton("Перейти")
        self.go_button.clicked.connect(lambda: self.navigate(self.address.text().strip()))
        nav.addWidget(self.address, 1)
        nav.addWidget(self.go_button)
        root.addLayout(nav)
        quick = QHBoxLayout()
        for label, path in PLAYEROK_ROUTES.items():
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, p=path: self.navigate(PLAYEROK_ORIGIN + p))
            quick.addWidget(button)
        root.addLayout(quick)

        split = QSplitter()
        self.browser_status = QLabel()
        use_webengine = WEBENGINE_AVAILABLE if enable_webengine is None else (WEBENGINE_AVAILABLE and enable_webengine)
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
