from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path

from core.database.db import Database
from integrations.playerok.listing_config import PlayerokListingConfigService
import app.playerok_browser as playerok_browser
from app.playerok_browser import (
    PLAYEROK_ORIGIN, PlayerokBrowserController, PlayerokBrowserPanel, WEBENGINE_AVAILABLE,
)
from app.platforms_panel import MarketplacePanel
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from PySide6.QtCore import QUrl
from integrations.playerok.sync import PlayerokDealSyncService



class PlayerokListingConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "playerok.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def versions(self, listing_id):
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT version_number,delivery_quantity_per_deal,item_selectors_json,bot_pool_json "
                "FROM gd_config_versions WHERE listing_id=? ORDER BY version_number",
                (listing_id,),
            ).fetchall()

    def test_steam_cards_config_creates_immutable_versions(self):
        service = PlayerokListingConfigService(self.db)
        first = service.save(external_listing_id="cards-1", title="Cards", url="https://playerok.com/products/cards",
                             mode="steam_cards", quantity=3, bot_pool=["BotA"])
        second = service.save(external_listing_id="cards-1", title="Cards v2", url="", mode="steam_cards",
                              quantity=5, bot_pool=["BotB"])
        rows = self.versions(first["listing_id"])
        self.assertEqual([r["version_number"] for r in rows], [1, 2])
        self.assertEqual([r["delivery_quantity_per_deal"] for r in rows], [3, 5])
        self.assertEqual(first["config_version_id"], "playerok-listing:" + first["listing_id"].split(":")[-1] + ":config:1")
        self.assertNotEqual(first["config_version_id"], second["config_version_id"])

    def test_cs2_config_stores_selectors_and_730_2(self):
        result = PlayerokListingConfigService(self.db).save(
            external_listing_id="cs2-1", title="CS2", url="", mode="cs2_items",
            quantity=2, bot_pool=["BotA"], selectors=[{"market_hash_name": "Case"}],
        )
        self.assertEqual(result["item_selectors"]["app_id"], 730)
        self.assertEqual(result["item_selectors"]["context_id"], 2)
        self.assertEqual(result["item_selectors"]["items"][0]["market_hash_name"], "Case")

    def test_manual_mode_stores_messages_without_steam_bots(self):
        result = PlayerokListingConfigService(self.db).save(
            external_listing_id="manual-1", title="Text", url="", mode="manual",
            auto_messages={"paid": "Hello"},
        )
        self.assertEqual(result["item_selectors"]["delivery_mode"], "manual")
        self.assertEqual(result["item_selectors"]["auto_messages"]["paid"], "Hello")
        self.assertEqual(result["bot_pool"], [])

    def test_controller_opens_exact_deal_and_chat_urls(self):
        opened = []
        controller = PlayerokBrowserController(opened.append)
        self.assertEqual(controller.open_deal("deal-123"), "https://playerok.com/deal/deal-123")
        self.assertEqual(controller.open_chat("chat-456"), "https://playerok.com/chats/chat-456")
        self.assertEqual(opened, [
            "https://playerok.com/deal/deal-123",
            "https://playerok.com/chats/chat-456",
        ])

    def test_browser_navigation_keeps_exact_deal_chat_ids_and_url_allowlist(self):
        panel = PlayerokBrowserPanel(database=self.db, enable_webengine=False)
        self.assertEqual(panel.open_deal("deal-123"), "https://playerok.com/deal/deal-123")
        self.assertEqual(panel.open_chat("chat-456"), "https://playerok.com/chats/chat-456")
        self.assertEqual(panel.address.text(), "https://playerok.com/chats/chat-456")
        for url in ("http://playerok.com/", "https://evil.example/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                panel.navigate(url)
        panel.close()

    def test_persistent_profile_path_is_user_local_and_outside_repository(self):
        profile_root = playerok_browser.playerok_profile_root().resolve()
        repository = Path(__file__).resolve().parents[1]
        self.assertEqual(profile_root.parts[-2:], (".gamedelivery", "playerok-web-profile"))
        self.assertFalse(profile_root.is_relative_to(repository))

    def test_profile_routes_are_built_for_all_sales_statuses(self):
        self.assertEqual(playerok_browser.playerok_profile_routes("seller"), {
            "Мои объявления": "/profile/seller/products",
            "Продажи: Оплаченные (PAID)": "/profile/seller/sales?status=paid",
            "Продажи: Отправленные (SENT)": "/profile/seller/sales?status=sent",
            "Продажи: Завершённые (CONFIRMED)": "/profile/seller/sales?status=confirmed",
            "Продажи: Возврат средств": "/profile/seller/sales?status=rolled-back",
        })

    def test_profile_username_is_encoded_as_one_path_segment(self):
        routes = playerok_browser.playerok_profile_routes("seller name/тест")
        self.assertEqual(routes["Мои объявления"], "/profile/seller%20name%2F%D1%82%D0%B5%D1%81%D1%82/products")

    def test_profile_routes_reject_missing_username_before_navigation(self):
        opened = []
        controller = PlayerokBrowserController(opened.append)
        with self.assertRaisesRegex(ValueError, "username"):
            controller.open_profile_route(None, "Мои объявления")
        self.assertEqual(opened, [])

    def test_quick_profile_route_does_not_guess_username_or_call_adapter(self):
        class Adapter:
            def restore_session(self):
                raise AssertionError("adapter session must not be accessed")

        parent = QWidget()
        parent.playerok = Adapter()
        panel = PlayerokBrowserPanel(database=self.db, parent=parent, enable_webengine=False)
        panel._profile_route_buttons["Мои объявления"].click()
        self.assertEqual(panel.address.text(), PLAYEROK_ORIGIN)
        self.assertIn("username", panel.browser_status.text().lower())
        panel.close()
        parent.close()

    def test_chat_quick_route_remains_available_without_profile(self):
        self.assertEqual(playerok_browser.PLAYEROK_FIXED_ROUTES, {"Чаты": "/chats"})

    def test_headless_panel_constructs_without_webengine(self):
        # CI may have WebEngine installed or omitted; both are valid.
        self.assertIsInstance(WEBENGINE_AVAILABLE, bool)
        panel = PlayerokBrowserPanel(database=self.db)
        self.assertIsNone(panel.web)
        self.assertEqual(panel.address.text(), PLAYEROK_ORIGIN)
        self.assertIn("headless", panel.browser_status.text())
        panel.close()

    def test_marketplace_panel_has_browser_status_and_no_adapter_login_or_sync(self):
        panel = MarketplacePanel(database=self.db)
        visible_text = " ".join(widget.text() for widget in panel.findChildren(QLabel))
        for legacy_label in ("Получить код", "Подтвердить код", "Подтвердить 2FA", "E-mail аккаунта"):
            self.assertNotIn(legacy_label, visible_text)
        self.assertNotIn("не подключён", visible_text.lower())
        self.assertIn("не подтверждён", panel.status_label.text().lower())
        self.assertIn("следующем этапе", panel.status_label.text().lower())
        self.assertFalse(hasattr(panel, "_sync_timer"))
        self.assertFalse(hasattr(panel, "_pending_initial_sync"))
        self.assertFalse(hasattr(panel, "_fetch_orders"))
        self.assertFalse(hasattr(panel, "playerok"))
        self.assertFalse(panel.import_order_button.isEnabled())
        panel.close()

    def test_failed_browser_read_does_not_call_sync_service_or_commit_baseline(self):
        service = PlayerokDealSyncService(self.db)
        panel = MarketplacePanel(database=self.db, sync_service=service)
        panel._sales_read_completed(None, "Неполная загрузка; снимок не сохранён.")
        self.assertIn("снимок не сохранён", panel.status_label.text())
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playerok_sync_state").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)
        panel.close()

    def test_page_destruction_cancels_browser_reader_once(self):
        class SignalStub:
            def __init__(self):
                self.callback = None

            def connect(self, callback):
                self.callback = callback

            def disconnect(self, callback):
                if self.callback == callback:
                    self.callback = None

            def emit(self):
                self.callback(object())

        class PageStub:
            def __init__(self):
                self.destroyed = SignalStub()
                self.requests = []

            def url(self):
                return QUrl("https://playerok.com/sales")

            def runJavaScript(self, script, callback):
                self.requests.append((script, callback))

        class WebStub:
            def __init__(self, page):
                self._page = page

            def page(self):
                return self._page

        panel = PlayerokBrowserPanel(database=self.db, enable_webengine=False)
        page = PageStub()
        panel.web = WebStub(page)
        panel._page_ready = True
        completed = []
        reader = panel.read_sales(lambda result, error: completed.append((result, error)))
        page.destroyed.emit()
        page.requests[0][1]("__late_start_callback")
        self.assertFalse(reader.active)
        self.assertEqual(len(completed), 1)
        self.assertIsNone(completed[0][0])
        self.assertIn("закрыта", completed[0][1])
        self.assertIsNone(page.destroyed.callback)
        panel.close()

    def test_sales_read_rejects_non_playerok_page(self):
        class PageStub:
            def url(self):
                return QUrl("https://example.org/")

        class WebStub:
            def page(self):
                return PageStub()

        panel = PlayerokBrowserPanel(database=self.db, enable_webengine=False)
        panel.web = WebStub()
        panel._page_ready = True
        with self.assertRaisesRegex(RuntimeError, "Откройте Playerok"):
            panel.read_sales(lambda _snapshot, _error: None)
        panel.close()

    def test_browser_clear_waits_for_async_steps_and_reports_cookie_limit(self):
        class SignalStub:
            def __init__(self):
                self.callback = None

            def connect(self, callback):
                self.callback = callback

            def disconnect(self, callback):
                self.callback = None

            def emit(self):
                self.callback()

        class PageStub:
            def runJavaScript(self, script, callback):
                self.script = script
                self.callback = callback

            def deleteLater(self):
                pass

        class ProfileStub:
            def __init__(self):
                self.clearHttpCacheCompleted = SignalStub()
                self.cookie_requested = False
                self.cache_requested = False

            def cookieStore(self):
                return self

            def deleteAllCookies(self):
                self.cookie_requested = True

            def clearHttpCache(self):
                self.cache_requested = True

        class WebStub:
            def __init__(self):
                self.current_page = PageStub()

            def page(self):
                return self.current_page

            def setPage(self, page):
                self.current_page = page

            def setUrl(self, url):
                self.url = url.toString()

        panel = PlayerokBrowserPanel(database=self.db, enable_webengine=False)
        profile, web = ProfileStub(), WebStub()
        panel._profile, panel.web = profile, web
        with patch.object(playerok_browser.QMessageBox, "question", return_value=playerok_browser.QMessageBox.StandardButton.No):
            self.assertFalse(panel.clear_browser_session())
        self.assertFalse(profile.cookie_requested)

        with patch.object(playerok_browser.QMessageBox, "question", return_value=playerok_browser.QMessageBox.StandardButton.Yes), \
             patch.object(playerok_browser, "QWebEnginePage", side_effect=lambda profile, parent: PageStub()):
            self.assertTrue(panel.clear_browser_session())
            self.assertIn("Очищаю", panel.browser_status.text())
            web.page().callback(True)
            self.assertTrue(profile.cookie_requested)
            self.assertTrue(profile.cache_requested)
            self.assertIn("Очищаю", panel.browser_status.text())
            profile.clearHttpCacheCompleted.emit()

        self.assertIn("не подтверждён", panel.browser_status.text().lower())
        self.assertIsNot(web.page(), None)
        self.assertEqual(web.url, "about:blank")
        self.assertEqual(panel.address.text(), "")
        panel.close()

    def test_import_keeps_original_order_payload_without_adapter_session(self):
        panel = MarketplacePanel(database=self.db)
        order = {
            "order_id": "order-1", "deal_id": "deal-1", "chat_id": "chat-1",
            "listing_id": "listing-1", "title": "Item", "buyer": "buyer",
            "status": "PAID", "amount": "10", "created_at": "now",
        }
        panel._show_orders([order])
        received = []
        panel.order_import_requested.connect(received.append)
        panel.orders_table.selectRow(0)
        self.assertTrue(panel.import_order_button.isEnabled())
        panel._import_selected_order()
        self.assertEqual(received, [order])
        self.assertIsNot(received[0], order)
        panel.close()
