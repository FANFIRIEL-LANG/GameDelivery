from __future__ import annotations

import json
import os
import weakref
import shiboken6
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, quote

from PySide6.QtCore import QTimer, QUrl, Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QSpinBox, QPlainTextEdit, QSplitter, QMessageBox, QDialog,
)
from integrations.playerok.browser_sales import BrowserSalesReader, BrowserViewerChecker

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


class _PlayerokDevToolsDialog(QDialog):
    def __init__(self, parent, closed_callback):
        super().__init__(parent, Qt.WindowType.Window)
        self._closed_callback = closed_callback

    def closeEvent(self, event) -> None:
        callback = self._closed_callback
        self._closed_callback = None
        if callback is not None:
            callback(self)
        event.accept()


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
        self._devtools_window = None
        self._devtools_view = None
        self._devtools_page = None
        self._devtools_target_page_ref = None
        self._devtools_destroy_slot = None
        self._clear_pending = False
        self._page_storage_clear_ok = False
        self._page_ready = False
        self._viewer_checker = None
        self._profile_route_buttons: dict[str, QPushButton] = {}
        self._viewer_check_timeout = QTimer(self)
        self._viewer_check_timeout.setSingleShot(True)
        self._viewer_check_timeout.setInterval(30_000)
        self._viewer_check_timeout.timeout.connect(self._viewer_check_timed_out)
        root = QVBoxLayout(self)
        nav = QHBoxLayout()
        self.back_button = QPushButton("Назад")
        self.forward_button = QPushButton("Вперёд")
        self.reload_button = QPushButton("Обновить")
        self.home_button = QPushButton("Домой")
        self.devtools_button = QPushButton("Открыть DevTools")
        self.clear_session_button = QPushButton("Очистить данные сайта")
        self.devtools_button.clicked.connect(self.open_devtools)
        for button in (
            self.back_button, self.forward_button, self.reload_button,
            self.home_button, self.devtools_button, self.clear_session_button,
        ):
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

    def open_devtools(self) -> bool:
        """Open Chromium DevTools for the existing Playerok page, without navigation."""
        if self.web is None or self._profile is None or self._clear_pending:
            self.browser_status.setText(
                "Не удалось открыть DevTools: страница Playerok сейчас недоступна."
            )
            return False

        try:
            page = self.web.page()
            if page is None or not shiboken6.isValid(page):
                raise RuntimeError("invalid page")
        except (RuntimeError, TypeError, AttributeError):
            self.browser_status.setText(
                "Не удалось открыть DevTools: страница Playerok сейчас недоступна."
            )
            return False

        try:
            page_url = urlsplit(page.url().toString())
        except (RuntimeError, TypeError, AttributeError):
            page_url = None
        if (
            page_url is None
            or page_url.scheme != "https"
            or page_url.hostname not in {"playerok.com", "www.playerok.com"}
        ):
            self.browser_status.setText(
                "Не удалось открыть DevTools: сначала откройте Playerok во встроенном браузере."
            )
            return False

        current_target_ref = self._devtools_target_page_ref
        if current_target_ref is not None and current_target_ref() is page:
            try:
                attached_page = page.devToolsPage()
                if (
                    attached_page is self._devtools_page
                    and self._devtools_window is not None
                    and shiboken6.isValid(self._devtools_window)
                ):
                    self._activate_devtools_window()
                    return True
            except (RuntimeError, TypeError, AttributeError):
                pass
            self._discard_devtools_references(delete_window=True)
        elif self._devtools_window is not None:
            self._discard_devtools_references(delete_window=True)

        try:
            attached_page = page.devToolsPage()
        except (RuntimeError, TypeError, AttributeError):
            self.browser_status.setText(
                "Не удалось открыть DevTools: состояние страницы Playerok недоступно."
            )
            return False
        if attached_page is not None:
            self.browser_status.setText(
                "Не удалось открыть DevTools: к странице Playerok уже подключён другой инспектор."
            )
            return False

        window = None
        try:
            window = _PlayerokDevToolsDialog(self, self._devtools_window_closed)
            window.setWindowTitle("Chrome DevTools — Playerok")
            window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            view = QWebEngineView(window)
            devtools_page = QWebEnginePage(self._profile, view)
            view.setPage(devtools_page)
            layout = QVBoxLayout(window)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(view)

            target_page_ref = weakref.ref(page)
            destroy_slot = lambda *_args, ref=target_page_ref: self._inspected_page_destroyed(ref)
            page.destroyed.connect(destroy_slot)

            self._devtools_window = window
            self._devtools_view = view
            self._devtools_page = devtools_page
            self._devtools_target_page_ref = target_page_ref
            self._devtools_destroy_slot = destroy_slot

            page.setDevToolsPage(devtools_page)
            window.resize(1100, 750)
            self._activate_devtools_window()
            self.browser_status.setText(
                "Chrome DevTools подключён к текущей странице Playerok."
            )
            return True
        except Exception:
            if self._devtools_window is window and window is not None:
                target_ref = self._devtools_target_page_ref
                target_page = target_ref() if target_ref is not None else None
                try:
                    if (
                        target_page is not None
                        and shiboken6.isValid(target_page)
                        and target_page.devToolsPage() is self._devtools_page
                    ):
                        target_page.setDevToolsPage(None)
                except (RuntimeError, TypeError, AttributeError):
                    pass
                self._discard_devtools_references(delete_window=True)
            elif window is not None:
                try:
                    window.close()
                    window.deleteLater()
                except (RuntimeError, AttributeError):
                    pass
            self.browser_status.setText(
                "Не удалось подключить DevTools к странице Playerok."
            )
            return False

    def _activate_devtools_window(self) -> None:
        window = self._devtools_window
        if window is None or not shiboken6.isValid(window):
            return
        window.show()
        window.raise_()
        window.activateWindow()

    def _inspected_page_destroyed(self, target_page_ref) -> None:
        if self._devtools_target_page_ref is not target_page_ref:
            return
        self._discard_devtools_references(delete_window=True)
        if shiboken6.isValid(self):
            self.browser_status.setText(
                "Страница Playerok закрыта; окно DevTools закрыто."
            )

    def _devtools_window_closed(self, window) -> None:
        if not shiboken6.isValid(self) or self._devtools_window is not window:
            return
        self._discard_devtools_references(delete_window=True, close_window=False)

    def _discard_devtools_references(
        self, *, delete_window: bool, close_window: bool = True
    ) -> None:
        window = self._devtools_window
        target_ref = self._devtools_target_page_ref
        destroy_slot = self._devtools_destroy_slot
        devtools_page = self._devtools_page
        self._devtools_window = None
        self._devtools_view = None
        self._devtools_page = None
        self._devtools_target_page_ref = None
        self._devtools_destroy_slot = None

        target_page = target_ref() if target_ref is not None else None
        if target_page is not None and destroy_slot is not None:
            try:
                if (
                    shiboken6.isValid(target_page)
                    and target_page.devToolsPage() is devtools_page
                ):
                    target_page.setDevToolsPage(None)
            except (RuntimeError, TypeError):
                pass
            try:
                target_page.destroyed.disconnect(destroy_slot)
            except (RuntimeError, TypeError):
                pass
        if window is not None:
            try:
                if close_window:
                    window.close()
                if delete_window:
                    window.deleteLater()
            except (RuntimeError, AttributeError):
                pass

    def open_deal(self, deal_id: str) -> str:
        return self.controller.open_deal(deal_id)

    def open_chat(self, chat_id: str) -> str:
        return self.controller.open_chat(chat_id)

    def _open_profile_route(self, route_label: str) -> None:
        self.browser_status.setText("Username профиля не получен из браузерной страницы. Откройте нужный раздел через сайт.")

    def _load_finished(self, ok: bool) -> None:
        self._page_ready = bool(ok)
        self.browser_status.setText(
            "Браузер открыт; статус входа Playerok не проверен."
            if ok else "Не удалось загрузить страницу Playerok."
        )

    @property
    def viewer_check_active(self) -> bool:
        return bool(self._viewer_checker is not None and self._viewer_checker.active)

    def check_authentication(self, finished) -> BrowserViewerChecker | None:
        """Check the current page session with one read-only viewer GET; do not sync sales."""
        if not callable(finished):
            raise TypeError("finished должен быть вызываемым")
        if self._viewer_checker is not None and self._viewer_checker.active:
            finished(False, "poll_state_error: проверка авторизации уже выполняется.")
            return None
        if self.web is None or not self._page_ready or self._clear_pending:
            message = "javascript_start_error: страница Playerok не готова для проверки авторизации."
            self.browser_status.setText(message)
            finished(False, message)
            return None

        page = self.web.page()
        current_url = urlsplit(page.url().toString())
        if current_url.scheme != "https" or current_url.hostname not in {"playerok.com", "www.playerok.com"}:
            message = "javascript_start_error: откройте Playerok во встроенном браузере."
            self.browser_status.setText(message)
            finished(False, message)
            return None

        checker_ref = None
        page_destroyed_state = [False]

        def page_destroyed(*_args):
            page_destroyed_state[0] = True
            checker = checker_ref[0] if checker_ref else None
            if checker is not None:
                checker.cancel(stage="page_destroyed")

        def page_is_alive() -> bool:
            return not page_destroyed_state[0] and shiboken6.isValid(page)

        def checker_finished(authorized: bool, error: str | None):
            self._viewer_check_timeout.stop()
            try:
                page.destroyed.disconnect(page_destroyed)
            except (RuntimeError, TypeError):
                pass
            checker = checker_ref[0] if checker_ref else None
            if self._viewer_checker is checker:
                self._viewer_checker = None
            if authorized:
                self.browser_status.setText("Авторизация Playerok подтверждена.")
            else:
                self.browser_status.setText(error or "viewer_parse_error: авторизация не подтверждена.")
            finished(authorized, error)

        checker = BrowserViewerChecker(
            page.runJavaScript,
            checker_finished,
            schedule=lambda callback: QTimer.singleShot(50, callback),
            is_page_alive=page_is_alive,
        )
        checker_ref = [checker]
        self._viewer_checker = checker
        page.destroyed.connect(page_destroyed)
        self._viewer_check_timeout.start()
        checker.start()
        return checker

    def _viewer_check_timed_out(self) -> None:
        if self._viewer_checker is not None:
            self._viewer_checker.cancel(stage="timeout")

    def read_page_hostname(self, finished) -> None:
        """Read only location.hostname from the existing loaded WebEngine page."""
        if not callable(finished):
            raise TypeError("finished должен быть вызываемым")
        if self.web is None or not self._page_ready or self._clear_pending:
            finished(None)
            return

        page = self.web.page()
        completed = [False]

        def complete(value=None):
            if completed[0]:
                return
            completed[0] = True
            try:
                page.destroyed.disconnect(page_destroyed)
            except (AttributeError, RuntimeError, TypeError):
                pass
            finished(value if type(value) is str else None)

        def page_destroyed(*_args):
            complete(None)

        try:
            page.destroyed.connect(page_destroyed)
        except (AttributeError, RuntimeError, TypeError):
            pass
        try:
            page.runJavaScript("location.hostname", complete)
        except Exception:
            complete(None)

    def read_sales(self, finished, *, on_request_started=None) -> BrowserSalesReader:
        if self.web is None or not self._page_ready or self._clear_pending:
            raise RuntimeError("Сначала дождитесь загрузки сайта Playerok во встроенном браузере.")
        page = self.web.page()
        current_url = urlsplit(page.url().toString())
        if current_url.scheme != "https" or current_url.hostname not in {"playerok.com", "www.playerok.com"}:
            raise RuntimeError("Откройте Playerok во встроенном браузере перед синхронизацией продаж.")
        reader_ref = None

        def page_destroyed(*_args):
            current_reader = reader_ref() if reader_ref is not None else None
            if current_reader is not None:
                current_reader.cancel(stage="page_destroyed")

        def reader_finished(snapshot, error):
            try:
                page.destroyed.disconnect(page_destroyed)
            except (RuntimeError, TypeError):
                pass
            finished(snapshot, error)

        reader = BrowserSalesReader(
            page.runJavaScript,
            reader_finished,
            schedule=lambda callback: QTimer.singleShot(50, callback),
            on_request_started=on_request_started,
        )
        reader_ref = weakref.ref(reader)
        page.destroyed.connect(page_destroyed)
        reader.start()
        return reader

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
        # A route slug or query parameter is not evidence of canonical item.id.
        # Clear any stale value so copying another page cannot silently bind it.
        self.listing_id.clear()
        if self.web is not None and self.web.title():
            self.listing_title.setText(self.web.title())
        self.browser_status.setText(
            "URL скопирован. Slug не используется как listing_id; введи подтверждённый canonical item.id отдельно."
        )

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
