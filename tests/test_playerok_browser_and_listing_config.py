from __future__ import annotations

import os
import tempfile
import unittest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path

from core.database.db import Database
from integrations.playerok.listing_config import PlayerokListingConfigService
import app.playerok_browser as playerok_browser
from PySide6.QtCore import Signal
from app.playerok_browser import (
    PLAYEROK_ORIGIN, PlayerokBrowserController, PlayerokBrowserPanel, WEBENGINE_AVAILABLE,
)
from PySide6.QtWidgets import QApplication, QWidget



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

    def test_profile_failure_or_missing_username_does_not_navigate(self):
        class ProfileAdapter:
            def __init__(self, profile=None, error=None):
                self.profile = profile
                self.error = error

            def restore_session(self):
                if self.error:
                    raise RuntimeError(self.error)
                return self.profile

        for adapter in (
            ProfileAdapter(error="profile unavailable"),
            ProfileAdapter(profile={"connected": True, "username": ""}),
            ProfileAdapter(profile={"connected": True, "username": "Аккаунт Playerok"}),
        ):
            with self.subTest(adapter=adapter):
                parent = QWidget()
                parent.playerok = adapter
                panel = PlayerokBrowserPanel(database=self.db, parent=parent, enable_webengine=False)
                panel._profile_route_buttons["Мои объявления"].click()
                worker = panel._profile_lookup_thread
                self.assertIsNotNone(worker)
                self.assertTrue(worker.wait(3000))
                self.qt_app.processEvents()
                self.assertEqual(panel.address.text(), PLAYEROK_ORIGIN)
                self.assertIn("username", panel.browser_status.text().lower())
                panel.close()
                parent.close()

    def test_profile_button_uses_username_from_existing_adapter_profile(self):
        class ProfileAdapter:
            def restore_session(self):
                return {"connected": True, "username": "seller"}

        parent = QWidget()
        parent.playerok = ProfileAdapter()
        panel = PlayerokBrowserPanel(database=self.db, parent=parent, enable_webengine=False)
        panel._profile_route_buttons["Продажи: Оплаченные (PAID)"].click()
        worker = panel._profile_lookup_thread
        self.assertIsNotNone(worker)
        self.assertTrue(worker.wait(3000))
        self.qt_app.processEvents()
        self.assertEqual(
            panel.address.text(),
            PLAYEROK_ORIGIN + "/profile/seller/sales?status=paid",
        )
        panel.close()
        parent.close()

    def test_chat_quick_route_remains_available_without_profile(self):
        self.assertEqual(playerok_browser.PLAYEROK_FIXED_ROUTES, {"Чаты": "/chats"})

    def test_playerok_connection_change_invalidates_cached_username(self):
        class ProfileParent(QWidget):
            connection_status_changed = Signal(str)

        parent = ProfileParent()
        panel = PlayerokBrowserPanel(database=self.db, parent=parent, enable_webengine=False)
        panel._seller_username = "old-seller"
        parent.connection_status_changed.emit("Playerok: не подключён")
        self.assertIsNone(panel._seller_username)
        panel.close()
        parent.close()

    def test_headless_panel_constructs_without_webengine(self):
        # CI may have WebEngine installed or omitted; both are valid.
        self.assertIsInstance(WEBENGINE_AVAILABLE, bool)
        panel = PlayerokBrowserPanel(database=self.db)
        self.assertIsNone(panel.web)
        self.assertIn("headless", panel.browser_status.text())
        panel.close()
