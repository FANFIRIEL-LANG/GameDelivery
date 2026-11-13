from __future__ import annotations

import os
import tempfile
import unittest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path

from core.database.db import Database
from integrations.playerok.listing_config import PlayerokListingConfigService
from app.playerok_browser import (
    PlayerokBrowserController, PlayerokBrowserPanel, WEBENGINE_AVAILABLE,
)
from PySide6.QtWidgets import QApplication



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

    def test_headless_panel_constructs_without_webengine(self):
        # CI may have WebEngine installed or omitted; both are valid.
        self.assertIsInstance(WEBENGINE_AVAILABLE, bool)
        panel = PlayerokBrowserPanel(database=self.db)
        self.assertIsNone(panel.web)
        self.assertIn("headless", panel.browser_status.text())
        panel.close()
