from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QDateTime, Qt

import app.playerok_browser as playerok_browser
from app.playerok_browser import PlayerokBrowserPanel
from integrations.playerok.client import _USER_AGENT
from integrations.playerok.graphql_http import (
    PlayerokGraphQLTransportError,
    PlayerokReadOnlyGraphQLClient,
)

TOKEN = "synthetic-token-sentinel"


class MemoryKeyring:
    def __init__(self, value):
        self.value = value

    def get_password(self, service, account):
        return self.value

    def set_password(self, service, account, value):
        self.value = value


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def disconnect(self, callback):
        self.callbacks.remove(callback)

    def emit(self, value=None):
        for callback in tuple(self.callbacks):
            callback() if value is None else callback(value)


class FakeTimer:
    instances = []

    def __init__(self, _parent=None):
        self.timeout = FakeSignal()
        self.active = False
        self.interval = None
        self.deleted = False
        self.instances.append(self)

    def setSingleShot(self, value):
        self.single_shot = value

    def setInterval(self, value):
        self.interval = value

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def deleteLater(self):
        self.deleted = True

    def fire(self):
        if self.active:
            self.active = False
            self.timeout.emit()


class FakeCookie:
    def __init__(self, name, value, domain=b".playerok.com", *, path=b"/", secure=True,
                 http_only=True, session=False, expiry=None):
        self._name = name.encode() if isinstance(name, str) else name
        self._value = value.encode() if isinstance(value, str) else value
        self._domain = domain
        self._path = path
        self._secure = secure
        self._http_only = http_only
        self._session = session
        self._expiry = expiry

    def name(self):
        return self._name

    def value(self):
        return self._value

    def domain(self):
        return self._domain

    def path(self):
        return self._path

    def isSecure(self):
        return self._secure

    def isHttpOnly(self):
        return self._http_only

    def isSessionCookie(self):
        return self._session

    def expirationDate(self):
        return self._expiry


class FakeCookieStore:
    def __init__(self, *, load_error=None):
        self.cookieAdded = FakeSignal()
        self.load_error = load_error
        self.loaded = False

    def loadAllCookies(self):
        self.loaded = True
        if self.load_error:
            raise self.load_error


class FakeProfile:
    def __init__(self, store):
        self.store = store

    def cookieStore(self):
        return self.store

    def httpUserAgent(self):
        return _USER_AGENT


class FakePage:
    def __init__(self, profile):
        self._profile = profile

    def profile(self):
        return self._profile

    def url(self):
        return FakeUrl()


class FakeUrl:
    def toString(self):
        return "https://www.playerok.com/chats"


class FakeWeb:
    def __init__(self, page):
        self._page = page

    def page(self):
        return self._page


class PlayerokGraphQLSessionCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        FakeTimer.instances = []
        self.panel = PlayerokBrowserPanel(enable_webengine=False)
        self.store = FakeCookieStore()
        self.panel._profile = FakeProfile(self.store)
        self.panel.web = FakeWeb(FakePage(self.panel._profile))
        self.panel.sales_session_available = lambda: True

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.qt_app.processEvents()

    def _save_with_memory_keyring(self, keyring):
        save = PlayerokReadOnlyGraphQLClient.save_browser_context

        def save_to_test_keyring(cookies, user_agent):
            return save(cookies, user_agent, keyring_backend=keyring)

        return patch.object(
            PlayerokReadOnlyGraphQLClient,
            "save_browser_context",
            side_effect=save_to_test_keyring,
        )

    def test_button_starts_current_profile_cookie_capture(self):
        self.panel.graphql_context_button.setEnabled(True)
        with patch.object(playerok_browser, "QTimer", FakeTimer):
            self.panel.graphql_context_button.click()

        self.assertTrue(self.store.loaded)
        self.assertIsNotNone(self.panel._graphql_context_capture)
        self.assertTrue(self.panel.browser_status.text().startswith("Ожидаю cookies"))

    def test_delayed_token_after_first_quiet_window_saves_and_reads_back(self):
        keyring = MemoryKeyring(None)
        completed = []
        with patch.object(playerok_browser, "QTimer", FakeTimer), self._save_with_memory_keyring(keyring):
            self.assertTrue(self.panel.save_graphql_session_context(finished=lambda *args: completed.append(args)))
            quiet_timer, hard_timer = FakeTimer.instances
            self.assertFalse(quiet_timer.active)
            self.assertTrue(hard_timer.active)

            self.store.cookieAdded.emit(FakeCookie("__cf_bm", "synthetic-cf-cookie"))
            quiet_timer.fire()
            self.assertEqual(completed, [])
            self.assertIsNotNone(self.panel._graphql_context_capture)

            self.store.cookieAdded.emit(FakeCookie("token", TOKEN))
            quiet_timer.fire()

        self.assertEqual(completed, [(True, None)])
        self.assertIsNone(self.panel._graphql_context_capture)
        self.assertIn("подтверждён чтением из keyring", self.panel.browser_status.text())
        self.assertEqual(
            dict(PlayerokGraphQLSessionCaptureTests._context_cookies(keyring)),
            {"__cf_bm": "synthetic-cf-cookie", "token": TOKEN},
        )

    @staticmethod
    def _context_cookies(keyring):
        from integrations.playerok.graphql_http import PlayerokAuthContext
        return PlayerokAuthContext.from_keyring(keyring).cookies

    def test_missing_token_waits_for_hard_deadline_and_reports_incomplete_reason(self):
        completed = []
        with patch.object(playerok_browser, "QTimer", FakeTimer):
            self.panel.save_graphql_session_context(finished=lambda *args: completed.append(args))
            quiet_timer, hard_timer = FakeTimer.instances
            self.store.cookieAdded.emit(FakeCookie("__cf_bm", "synthetic-cf-cookie"))
            quiet_timer.fire()
            self.assertEqual(completed, [])
            hard_timer.fire()

        self.assertEqual(completed, [(False, "session_cookie_missing")])
        self.assertIn("Контекст GraphQL неполный", self.panel.browser_status.text())
        self.assertIn("cookie сессии Playerok не получена", self.panel.browser_status.text())
        self.assertIsNone(self.panel._graphql_context_capture)

    def test_keyring_write_error_is_reported_with_safe_reason(self):
        completed = []
        with patch.object(playerok_browser, "QTimer", FakeTimer), self._save_with_memory_keyring(MemoryKeyring(None)):
            self.panel.save_graphql_session_context(finished=lambda *args: completed.append(args))
            quiet_timer, _hard_timer = FakeTimer.instances
            self.store.cookieAdded.emit(FakeCookie("token", TOKEN))

            def fail_save(_cookies, _user_agent):
                raise PlayerokGraphQLTransportError("keyring_write_failed")

            with patch.object(PlayerokReadOnlyGraphQLClient, "save_browser_context", side_effect=fail_save):
                quiet_timer.fire()

        self.assertEqual(completed, [(False, "keyring_write_failed")])
        self.assertIn("Ошибка сохранения GraphQL-контекста", self.panel.browser_status.text())
        self.assertIn("Windows keyring отклонил запись", self.panel.browser_status.text())
        self.assertNotIn(TOKEN, self.panel.browser_status.text())

    def test_cookie_store_start_failure_is_reported_and_controls_reenabled(self):
        self.store.load_error = RuntimeError("synthetic error containing secret")
        completed = []
        with patch.object(playerok_browser, "QTimer", FakeTimer):
            self.assertFalse(self.panel.save_graphql_session_context(finished=lambda *args: completed.append(args)))

        self.assertEqual(completed, [(False, "cookie_store_start_failed")])
        self.assertIn("Ошибка сохранения GraphQL-контекста", self.panel.browser_status.text())
        self.assertNotIn("synthetic error", self.panel.browser_status.text())
        self.assertTrue(self.panel.graphql_context_button.isEnabled())
        self.assertTrue(self.panel.clear_session_button.isEnabled())

    def test_browser_status_is_in_visible_root_layout(self):
        self.assertGreaterEqual(self.panel.layout().indexOf(self.panel.browser_status), 0)

    def test_capture_fails_closed_if_current_page_uses_another_profile(self):
        self.panel.web = FakeWeb(FakePage(FakeProfile(self.store)))
        completed = []

        self.assertFalse(self.panel.save_graphql_session_context(finished=lambda *args: completed.append(args)))

        self.assertEqual(completed, [(False, "profile_mismatch")])
        self.assertFalse(self.store.loaded)
        self.assertIn("другой профиль WebEngine", self.panel.browser_status.text())

    def test_missing_token_reports_first_party_metadata_without_cookie_values(self):
        secret = "cookie-value-must-never-appear"
        completed = []
        expiry = QDateTime.fromString("2026-11-21T12:30:00Z", Qt.DateFormat.ISODate)
        self.assertTrue(expiry.isValid())
        with patch.object(playerok_browser, "QTimer", FakeTimer):
            self.panel.save_graphql_session_context(finished=lambda *args: completed.append(args))
            quiet_timer, hard_timer = FakeTimer.instances
            self.store.cookieAdded.emit(FakeCookie(
                "session_hint", secret, path=b"/graphql", secure=True,
                http_only=True, session=False, expiry=expiry,
            ))
            quiet_timer.fire()
            hard_timer.fire()

        status = self.panel.browser_status.text()
        self.assertEqual(completed, [(False, "session_cookie_missing")])
        self.assertIn("name=session_hint", status)
        self.assertIn("domain=.playerok.com", status)
        self.assertIn("path=/graphql", status)
        self.assertIn("secure=1", status)
        self.assertIn("http_only=1", status)
        self.assertIn("session=0", status)
        self.assertIn("expires_utc=2026-11-21T12:30:00Z", status)
        self.assertIn("type=FakeCookie", status)
        self.assertNotIn(secret, status)
        self.assertNotIn("cookie-value", status)


if __name__ == "__main__":
    unittest.main()
