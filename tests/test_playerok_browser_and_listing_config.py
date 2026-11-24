from __future__ import annotations

import os
import json
import re
import tempfile
import unittest
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path

from core.database.db import Database
from core.delivery.dispatcher import DeliveryDispatcher
from core.delivery.order_pipeline import DeliveryOrderPipeline
from core.delivery.playerok_freshness import PlayerokFreshnessGate
from core.delivery.recipes import normalize_legacy_cs2_selector_options
from core.delivery.stock_ledger import QuantityStockLedger, StockResource
from core.delivery.stock_identity import StockItemIdentity
from integrations.playerok import browser_sales
from integrations.playerok.browser_sales import BrowserSalesSnapshot
from integrations.playerok.listing_config import PlayerokListingConfigService
import app.playerok_browser as playerok_browser
from app.playerok_browser import (
    PLAYEROK_ORIGIN, PlayerokBrowserController, PlayerokBrowserPanel, WEBENGINE_AVAILABLE,
)
from app.platforms_panel import MarketplacePanel
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QUrl, Qt
from integrations.playerok.sync import PlayerokDealSyncService
from integrations.playerok.graphql_http import (
    _issue_graphql_sales_snapshot,
    is_fresh_trusted_graphql_sales_snapshot,
)


def trusted_graphql_snapshot(account_user_id, deals):
    now = monotonic()
    return _issue_graphql_sales_snapshot(account_user_id, deals, now, now)


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

    def test_chat_api_diagnostic_accepts_opaque_chat_path_ids(self):
        self.assertEqual(MarketplacePanel._chat_id_from_path("/chats/chat-id_opaque-123"), "chat-id_opaque-123")
        self.assertEqual(
            MarketplacePanel._chat_id_from_path("/chats/1f1e3494-1403-6240-6353-b8243fa6d62d/"),
            "1f1e3494-1403-6240-6353-b8243fa6d62d",
        )
        self.assertIsNone(MarketplacePanel._chat_id_from_path("/chats"))
        self.assertIsNone(MarketplacePanel._chat_id_from_path("/chats/a/b"))
        self.assertIsNone(MarketplacePanel._chat_id_from_path("/chats/   "))

    def test_graphql_sales_reader_is_created_from_current_browser_session(self):
        service = PlayerokDealSyncService(self.db)
        panel = MarketplacePanel(database=self.db, sync_service=service)
        expected_snapshot = SimpleNamespace(account_user_id="seller-from-browser")
        fake_reader = SimpleNamespace(cancel=lambda **_kwargs: None)
        calls = []
        outcomes = []

        def read_from_browser(callback, *, expected_account_user_id=None):
            calls.append(("browser-page", expected_account_user_id))
            callback(expected_snapshot, None)
            return fake_reader

        panel.browser_panel.read_graphql_sales_snapshot = read_from_browser
        with patch(
            "integrations.playerok.graphql_http.PlayerokReadOnlyGraphQLClient.from_keyring",
            side_effect=AssertionError("browser sales sync must not read keyring"),
        ):
            reader = panel._start_graphql_poll_reader(
                lambda snapshot, error: outcomes.append((snapshot, error))
            )

        self.assertIs(reader, fake_reader)
        self.assertEqual(calls, [("browser-page", None)])
        self.assertEqual(outcomes, [(expected_snapshot, None)])
        self.assertIsNone(panel._graphql_read_worker)
        panel.close()

    def test_chat_api_diagnostic_runs_chat_and_message_reads_when_chat_list_fails(self):
        panel = MarketplacePanel(database=self.db)
        calls = []

        class FakeAPI:
            def get_chats(self, user_id, callback, *, page_observer=None):
                calls.append(("chats", user_id))
                page_observer(False, "http_400")
                callback(None, "http_400")

            def get_chat(self, chat_id, callback):
                calls.append(("chat", chat_id))
                callback(None, "http_400")

            def get_chat_messages(self, chat_id, callback, *, page_observer=None):
                calls.append(("messages", chat_id))
                page_observer(False, None)
                callback((), None)

        panel._chat_api = FakeAPI()
        ws_calls = []
        panel._chat_listener = SimpleNamespace(
            account_user_id="account-safe", active=False,
            start_for_read_only_diagnostic=lambda user_id, chat_id: ws_calls.append((user_id, chat_id)),
        )
        original_read_busy = panel._sales_read_busy
        panel._sales_read_busy = lambda: False
        original_update = panel._update_playerok_read_controls
        panel._update_playerok_read_controls = lambda: None
        try:
            with patch.object(
                panel.browser_panel,
                "web",
                SimpleNamespace(url=lambda: SimpleNamespace(
                    toString=lambda: "https://playerok.com/chats/chat-id_opaque-123?private=value"
                )),
            ):
                panel._diagnose_chat_api()
            report = panel.chat_api_diagnostic_report.text()
            self.assertEqual([item[0] for item in calls], ["chats", "chat", "messages"])
            self.assertIn("chats=FAIL(http_400)", report)
            self.assertIn("chat=FAIL(http_400)", report)
            self.assertIn("messages=PASS(0)", report)
            self.assertEqual(ws_calls, [("account-safe", "chat-id_opaque-123")])
            self.assertNotIn("private", report)
            self.assertNotIn("chat-id_opaque-123", report)
        finally:
            panel._sales_read_busy = original_read_busy
            panel._update_playerok_read_controls = original_update
            panel.close()

    def test_chat_api_diagnostic_uses_confirmed_page_without_listener(self):
        panel = MarketplacePanel(database=self.db)
        calls = []

        class FakeAPI:
            def get_viewer(self, callback):
                calls.append("viewer")
                callback("account-safe", None)

            def get_chats(self, user_id, callback, *, page_observer=None):
                calls.append("chats")
                page_observer(False, None)
                callback((), None)

            def get_chat(self, chat_id, callback):
                calls.append("chat")
                callback(SimpleNamespace(id=chat_id), None)

            def get_chat_messages(self, chat_id, callback, *, page_observer=None):
                calls.append("messages")
                page_observer(False, None)
                callback((), None)

        original_read_busy = panel._sales_read_busy
        original_update = panel._update_playerok_read_controls
        panel._sales_read_busy = lambda: False
        panel._update_playerok_read_controls = lambda: None
        panel._playerok_auth_confirmed = True
        try:
            with (
                patch.object(panel.browser_panel, "sales_session_available", return_value=True),
                patch.object(
                    panel.browser_panel,
                    "web",
                    SimpleNamespace(url=lambda: SimpleNamespace(
                        toString=lambda: "https://playerok.com/chats/chat-id_opaque-123"
                    )),
                ),
                patch.object(
                    panel.browser_panel,
                    "_run_current_page_javascript",
                    side_effect=lambda _script, callback: callback(True),
                ),
                patch("app.platforms_panel.PlayerokBrowserChatAPI", return_value=FakeAPI()),
            ):
                panel._diagnose_chat_api()
            self.assertEqual(calls, ["viewer", "chats", "chat", "messages"])
            self.assertIn("chats=PASS (0)", panel.chat_api_diagnostic_report.text())
            self.assertIn("messages=PASS(0)", panel.chat_api_diagnostic_report.text())
            self.assertTrue(panel._chat_listener.active)
        finally:
            panel._sales_read_busy = original_read_busy
            panel._update_playerok_read_controls = original_update
            panel.close()

    def test_steam_cards_config_creates_immutable_versions(self):
        service = PlayerokListingConfigService(self.db)
        first = service.save(external_listing_id="cards-1", title="Cards", url="https://playerok.com/products/cards",
                             mode="steam_cards", quantity=3, listing_quantity=10, bot_pool=["BotA"])
        second = service.save(external_listing_id="cards-1", title="Cards v2", url="", mode="steam_cards",
                              quantity=5, listing_quantity=12, bot_pool=["BotB"])
        rows = self.versions(first["listing_id"])
        self.assertEqual([r["version_number"] for r in rows], [1, 2])
        self.assertEqual([r["delivery_quantity_per_deal"] for r in rows], [3, 5])
        self.assertEqual(first["config_version_id"], "playerok-listing:" + first["listing_id"].split(":")[-1] + ":config:1")
        self.assertNotEqual(first["config_version_id"], second["config_version_id"])
        self.assertEqual(first["listing_quantity"], 10)
        self.assertEqual(second["listing_quantity"], 12)
        with self.db.connect() as conn:
            recipe_row = conn.execute(
                "SELECT recipe_items_json FROM gd_config_versions WHERE config_version_id=?",
                (first["config_version_id"],)
            ).fetchone()
        stored_recipe = json.loads(recipe_row[0])
        self.assertEqual(stored_recipe[0]["kind"], "steam_card_set")
        self.assertEqual(stored_recipe[0]["quantity_per_order"], 3)

    def test_steam_cards_listing_quantity_is_required_and_not_inferred_from_order_quantity(self):
        service = PlayerokListingConfigService(self.db)
        with self.assertRaisesRegex(ValueError, "отдельно от количества за покупку"):
            service.save(
                external_listing_id="cards-missing-listing-limit",
                title="Cards",
                url="",
                mode="steam_cards",
                quantity=3,
                bot_pool=["BotA"],
            )
        with self.db.connect() as conn:
            listing = conn.execute(
                "SELECT COUNT(*) FROM gd_listings WHERE listing_id=?",
                (service._listing_key("cards-missing-listing-limit"),),
            ).fetchone()[0]
        self.assertEqual(listing, 0)

    def test_steam_cards_browser_form_exposes_only_total_listing_sets(self):
        panel = PlayerokBrowserPanel(database=self.db, enable_webengine=False)
        try:
            panel.mode.setCurrentIndex(panel.mode.findData("steam_cards"))
            self.assertFalse(panel.listing_stock_quantity.isHidden())
            self.assertEqual(panel.listing_stock_quantity.prefix(), "Наборов в объявлении: ")
            self.assertTrue(panel.bots.isHidden())

            panel.listing_id.setText("steam-cards-ui-limit")
            panel.listing_title.setText("Steam card sets")
            panel.listing_url.setText("https://playerok.com/products/steam-cards-ui-limit")
            panel.quantity.setValue(2)
            panel.listing_stock_quantity.setValue(10)
            panel._save_config()

            self.assertIn("Лимит объявления: 10 наборов.", panel.browser_status.text())
            with self.db.connect() as db:
                row = db.execute(
                    "SELECT c.delivery_quantity_per_deal,c.bot_pool_json,c.recipe_schema_version,"
                    "c.recipe_items_json,l.total_sets "
                    "FROM gd_config_versions c JOIN gd_steam_card_listing_limits l "
                    "ON l.listing_id=c.listing_id WHERE c.config_version_id=( "
                    "SELECT config_version_id FROM gd_config_versions "
                    "WHERE listing_id=? ORDER BY version_number DESC LIMIT 1)",
                    (PlayerokListingConfigService._listing_key("steam-cards-ui-limit"),),
                ).fetchone()
            self.assertEqual(int(row["delivery_quantity_per_deal"]), 2)
            self.assertEqual(json.loads(row["bot_pool_json"]), [])
            self.assertEqual(row["recipe_schema_version"], 1)
            self.assertEqual(json.loads(row["recipe_items_json"])[0]["spec"]["allowed_bot_pool"], [])
            self.assertEqual(int(row["total_sets"]), 10)
        finally:
            panel.close()

    def test_cs2_config_stores_selectors_and_730_2(self):
        result = PlayerokListingConfigService(self.db).save(
            external_listing_id="cs2-1", title="CS2", url="", mode="cs2_items",
            quantity=2, bot_pool=["BotA"], selectors=[{"market_hash_name": "Case"}],
        )
        self.assertEqual(result["item_selectors"], [{
            "game_id": "cs2", "app_id": 730, "context_id": "2",
            "market_hash_name": "Case",
        }])
        self.assertTrue(DeliveryOrderPipeline._is_cs2_config(
            {"selectors": result["item_selectors"]}
        ))
        with self.db.connect() as conn:
            recipe = conn.execute(
                "SELECT recipe_items_json,recipe_schema_version FROM gd_config_versions WHERE config_version_id=?",
                (result["config_version_id"],)
            ).fetchone()
        recipe_item = json.loads(recipe["recipe_items_json"])[0]
        self.assertEqual(recipe_item["kind"], "steam_asset")
        self.assertEqual(recipe_item["quantity_per_order"], 2)
        self.assertEqual(recipe["recipe_schema_version"], 1)

    def test_playerok_browser_form_saves_and_allocates_listing_stock_quantity(self):
        PlayerokListingConfigService(self.db)
        ledger = QuantityStockLedger(self.db)
        resource = StockResource("cs2", 730, "2", "BotA", "22334455")
        ledger.observe(
            resource,
            10,
            identity=StockItemIdentity(
                "cs2", 730, "2", market_hash_name="AK-47 | Redline",
                name="AK-47 | Redline", class_id="class-redline",
            ),
            is_tradable=True,
        )
        panel = PlayerokBrowserPanel(database=self.db, enable_webengine=False)
        try:
            panel.mode.setCurrentIndex(panel.mode.findData("cs2_items"))
            panel.listing_id.setText("cs2-browser-stock")
            panel.listing_title.setText("AK-47 | Redline")
            panel.listing_url.setText("https://playerok.com/products/cs2-browser-stock")
            panel.quantity.setValue(2)
            panel.listing_stock_quantity.setValue(3)
            panel.bots.setText("BotA")
            panel.selectors.setPlainText('[{"market_hash_name":"AK-47 | Redline"}]')

            panel._save_config()

            self.assertIn("Общий склад: зарезервировано 6 шт.", panel.browser_status.text())
            with self.db.connect() as db:
                rows = db.execute(
                    "SELECT units FROM gd_stock_allocations WHERE owner_type='listing' "
                    "AND listing_id=? AND state='active'",
                    (PlayerokListingConfigService._listing_key("cs2-browser-stock"),),
                ).fetchall()
            self.assertEqual(sum(int(row["units"]) for row in rows), 6)
        finally:
            panel.close()

    def test_playerok_listing_quantity_allocates_recipe_units_from_shared_stock(self):
        service = PlayerokListingConfigService(self.db)
        result = service.save(
            external_listing_id="cs2-stock-quantity",
            title="CS2 stock quantity",
            url="",
            mode="cs2_items",
            quantity=2,
            bot_pool=["BotA"],
            selectors=[{"market_hash_name": "AK-47 | Redline"}],
        )
        recipe_item_id = result["recipe_items"][0]["item_id"]
        ledger = QuantityStockLedger(self.db)
        resource = StockResource("cs2", 730, "2", "BotA", "1234567890")
        ledger.observe(
            resource,
            10,
            identity=StockItemIdentity(
                "cs2", 730, "2", market_hash_name="AK-47 | Redline",
                name="AK-47 | Redline", class_id="class-redline",
            ),
            is_tradable=True,
        )

        allocated = service.allocate_listing_stock(
            config_version_id=result["config_version_id"],
            listing_quantity=3,
        )
        repeated = service.allocate_listing_stock(
            config_version_id=result["config_version_id"],
            listing_quantity=3,
        )
        self.assertEqual(
            allocated["units_by_recipe_item"],
            {recipe_item_id: 6},
        )
        self.assertEqual(allocated["reserved_units"], 6)
        self.assertEqual(sum(int(row["units"]) for row in allocated["allocations"]), 6)
        self.assertEqual(
            {row["allocation_id"] for row in allocated["allocations"]},
            {row["allocation_id"] for row in repeated["allocations"]},
        )
        self.assertEqual(ledger.free_units(resource), 4)

    def test_playerok_listing_stock_supports_alternatives_and_rejects_superseded_configs(self):
        service = PlayerokListingConfigService(self.db)
        alternatives = service.save(
            external_listing_id="cs2-stock-alternatives",
            title="CS2 alternatives",
            url="",
            mode="cs2_items",
            quantity=1,
            bot_pool=["BotA"],
            selectors=[
                {"market_hash_name": "AK-47 | Redline"},
                {"market_hash_name": "AWP | Dragon Lore"},
            ],
        )
        ledger = QuantityStockLedger(self.db)
        for asset_id, market_name in (
            ("alt-redline", "AK-47 | Redline"),
            ("alt-dragon", "AWP | Dragon Lore"),
        ):
            ledger.observe(
                StockResource("cs2", 730, "2", "BotA", asset_id), 1,
                identity=StockItemIdentity(
                    "cs2", 730, "2", market_hash_name=market_name,
                    name=market_name, class_id="class-" + asset_id,
                ),
                is_tradable=True,
            )
        allocated = service.allocate_listing_stock(
            config_version_id=alternatives["config_version_id"],
            listing_quantity=2,
        )
        self.assertEqual(allocated["reserved_units"], 2)
        self.assertEqual(sum(int(row["units"]) for row in allocated["allocations"]), 2)

        first = service.save(
            external_listing_id="cs2-stock-versioned",
            title="CS2 first version",
            url="",
            mode="cs2_items",
            quantity=1,
            bot_pool=["BotA"],
            selectors=[{"market_hash_name": "AK-47 | Redline"}],
        )
        second = service.save(
            external_listing_id="cs2-stock-versioned",
            title="CS2 second version",
            url="",
            mode="cs2_items",
            quantity=1,
            bot_pool=["BotA"],
            selectors=[{"market_hash_name": "AWP | Dragon Lore"}],
        )
        with self.assertRaisesRegex(ValueError, "устаревшей версии"):
            service.allocate_listing_stock(
                config_version_id=first["config_version_id"],
                listing_quantity=2,
            )
        self.assertNotEqual(first["config_version_id"], second["config_version_id"])

    def test_cs2_multi_selectors_remain_alternatives_and_old_wrapper_stays_readable(self):
        result = PlayerokListingConfigService(self.db).save(
            external_listing_id="cs2-many", title="CS2 alternatives", url="", mode="cs2_items",
            quantity=2, bot_pool=["BotA"],
            selectors=[{"market_hash_name": "Case A"}, {"name": "Case B"}],
        )
        self.assertEqual(len(result["item_selectors"]), 2)
        self.assertTrue(DeliveryOrderPipeline._is_cs2_config(
            {"selectors": result["item_selectors"]}
        ))
        with self.db.connect() as conn:
            version = conn.execute(
                "SELECT recipe_schema_version FROM gd_config_versions WHERE config_version_id=?",
                (result["config_version_id"],),
            ).fetchone()
        self.assertEqual(version[0], 1, "alternative selectors form one typed Steam Asset obligation")
        with self.db.connect() as conn:
            recipe_row = conn.execute(
                "SELECT recipe_items_json FROM gd_config_versions WHERE config_version_id=?",
                (result["config_version_id"],),
            ).fetchone()
        stored_spec = json.loads(recipe_row[0])[0]["spec"]
        self.assertEqual(stored_spec["selector"]["market_hash_name"], "Case A")
        self.assertEqual(stored_spec["selector_options"], [{"name": "Case B"}])

        old_wrapper = {
            "delivery_mode": "cs2_items", "app_id": 730, "context_id": 2,
            "items": [{"market_hash_name": "Legacy case"}, {"name": "Legacy fallback"}],
        }
        self.assertTrue(DeliveryOrderPipeline._is_cs2_config({"selectors": old_wrapper}))
        self.assertEqual(
            normalize_legacy_cs2_selector_options(old_wrapper),
            [
                {"game_id": "cs2", "app_id": 730, "context_id": "2", "market_hash_name": "Legacy case"},
                {"game_id": "cs2", "app_id": 730, "context_id": "2", "name": "Legacy fallback"},
            ],
        )

    def test_manual_mode_stores_messages_without_steam_bots(self):
        result = PlayerokListingConfigService(self.db).save(
            external_listing_id="manual-1", title="Text", url="", mode="manual",
            auto_messages={"paid": "Hello"},
        )
        self.assertEqual(result["item_selectors"]["delivery_mode"], "manual")
        self.assertEqual(result["item_selectors"]["auto_messages"]["paid"], "Hello")
        self.assertEqual(result["bot_pool"], [])

    def test_save_recipe_creates_new_typed_version_without_url_resolution(self):
        service = PlayerokListingConfigService(self.db)
        result = service.save_recipe(
            external_listing_id="verified-item-id",
            title="Mixed listing",
            url="https://playerok.com/products/display-slug",
            items=[
                {"item_id": "codes", "position": 1, "kind": "digital_text_unit",
                 "spec_version": 1, "quantity_per_order": 1, "required": True,
                 "stock_source_id": "code-stock", "executor_key": "text_delivery",
                 "spec": {"allocation": "one_unique_unit"}},
            ],
        )
        self.assertEqual(result["version_number"], 1)
        self.assertEqual(result["recipe_items"][0]["kind"], "digital_text_unit")
        self.assertIsNone(result["delivery_quantity_per_deal"])
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT external_listing_id FROM gd_listings WHERE listing_id=?",
                (result["listing_id"],),
            ).fetchone()
        self.assertEqual(row[0], "verified-item-id")

    def test_capture_listing_url_never_populates_canonical_id_from_slug_or_query(self):
        panel = PlayerokBrowserPanel(database=self.db, enable_webengine=False)
        try:
            for url in (
                "https://playerok.com/products/pretty-product-slug",
                "https://playerok.com/products/another-slug?id=possible-id",
            ):
                panel.listing_id.setText("stale-canonical-value")
                panel.address.setText(url)
                panel._capture_current_listing()
                self.assertEqual(panel.listing_url.text(), url)
                self.assertEqual(panel.listing_id.text(), "")
                self.assertIn("canonical item.id", panel.browser_status.text())
        finally:
            panel.close()

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
        self.assertEqual(panel.devtools_button.text(), "Открыть DevTools")
        panel.close()

    def _make_devtools_panel(self):
        class PageStub(QObject):
            def __init__(self, profile=None, parent=None):
                super().__init__(parent)
                self.profile = profile
                self._devtools_page = None

            def url(self):
                return QUrl("https://playerok.com/sales")

            def devToolsPage(self):
                return self._devtools_page

            def setDevToolsPage(self, page):
                self._devtools_page = page

        class WebStub:
            def __init__(self, page):
                self.current_page = page
                self.set_url_calls = []

            def page(self):
                return self.current_page

            def setUrl(self, url):
                self.set_url_calls.append(url)

        class ProfileStub:
            def __init__(self):
                self.cookie_delete_calls = 0
                self.cache_clear_calls = 0

            def cookieStore(self):
                return self

            def deleteAllCookies(self):
                self.cookie_delete_calls += 1

            def clearHttpCache(self):
                self.cache_clear_calls += 1

        class DevtoolsView(QWidget):
            created = []

            def __init__(self, parent=None):
                super().__init__(parent)
                self.page_value = None
                self.__class__.created.append(self)

            def setPage(self, page):
                self.page_value = page

        panel = PlayerokBrowserPanel(database=self.db, enable_webengine=False)
        target_page = PageStub()
        web = WebStub(target_page)
        profile = ProfileStub()
        panel.web = web
        panel._profile = profile
        return panel, target_page, web, profile, PageStub, DevtoolsView

    def test_devtools_attaches_to_existing_page_and_reuses_profile_without_navigation(self):
        panel, target_page, web, profile, PageStub, DevtoolsView = self._make_devtools_panel()
        with (
            patch.object(playerok_browser, "QWebEnginePage", PageStub),
            patch.object(playerok_browser, "QWebEngineView", DevtoolsView),
        ):
            self.assertTrue(panel.open_devtools())

        self.assertIs(web.page(), target_page)
        self.assertIs(target_page.devToolsPage(), panel._devtools_page)
        self.assertIs(panel._devtools_page.profile, profile)
        self.assertIs(panel._devtools_view.page_value, panel._devtools_page)
        self.assertEqual(web.set_url_calls, [])
        self.assertEqual(profile.cookie_delete_calls, 0)
        self.assertEqual(profile.cache_clear_calls, 0)
        panel.close()

    def test_devtools_repeated_open_reuses_window_and_never_attaches_a_second_inspector(self):
        panel, target_page, web, _profile, PageStub, DevtoolsView = self._make_devtools_panel()
        devtools_pages = []

        class CountingPage(PageStub):
            def __init__(self, profile=None, parent=None):
                super().__init__(profile, parent)
                if profile is not None:
                    devtools_pages.append(self)

        with (
            patch.object(playerok_browser, "QWebEnginePage", CountingPage),
            patch.object(playerok_browser, "QWebEngineView", DevtoolsView),
        ):
            panel.devtools_button.click()
            window = panel._devtools_window
            panel.devtools_button.click()
            self.assertIs(panel._devtools_window, window)
            self.assertEqual(len(devtools_pages), 1)
            self.assertIs(target_page.devToolsPage(), devtools_pages[0])
            self.assertIs(web.page(), target_page)
            window.close()
            self.assertFalse(window.isVisible())
            self.assertTrue(panel.devtools_button.isEnabled())
            self.assertIsNone(panel._devtools_window)
            self.assertIsNone(target_page.devToolsPage())
            panel.devtools_button.click()
            self.assertIsNot(panel._devtools_window, window)
            self.assertTrue(panel._devtools_window.isVisible())
            self.assertEqual(len(devtools_pages), 2)
            self.assertIs(target_page.devToolsPage(), devtools_pages[1])
        panel.close()

    def test_destroyed_playerok_page_closes_its_devtools_window(self):
        panel, target_page, _web, _profile, PageStub, DevtoolsView = self._make_devtools_panel()
        with (
            patch.object(playerok_browser, "QWebEnginePage", PageStub),
            patch.object(playerok_browser, "QWebEngineView", DevtoolsView),
        ):
            self.assertTrue(panel.open_devtools())
            window = panel._devtools_window
            target_page.deleteLater()
            QCoreApplication.sendPostedEvents(target_page, QEvent.Type.DeferredDelete)
            self.qt_app.processEvents()

        self.assertFalse(playerok_browser.shiboken6.isValid(window))
        self.assertIsNone(panel._devtools_window)
        self.assertIsNone(panel._devtools_page)
        self.assertIn("Страница Playerok закрыта", panel.browser_status.text())
        panel.close()

    def test_devtools_unavailable_page_reports_clear_status_without_creating_browser(self):
        panel = PlayerokBrowserPanel(database=self.db, enable_webengine=False)
        self.assertFalse(panel.open_devtools())
        self.assertIn("страница Playerok сейчас недоступна", panel.browser_status.text())
        self.assertIsNone(panel.web)
        panel.close()

    def test_devtools_refuses_a_second_inspector_and_non_playerok_pages(self):
        panel, target_page, _web, _profile, PageStub, DevtoolsView = self._make_devtools_panel()
        target_page.setDevToolsPage(PageStub())
        with (
            patch.object(playerok_browser, "QWebEnginePage", side_effect=AssertionError("duplicate inspector")),
            patch.object(playerok_browser, "QWebEngineView", side_effect=AssertionError("duplicate view")),
        ):
            self.assertFalse(panel.open_devtools())
        self.assertIn("уже подключён другой инспектор", panel.browser_status.text())
        self.assertIsNotNone(target_page.devToolsPage())

        target_page.setDevToolsPage(None)
        target_page.url = lambda: QUrl("https://example.invalid/")
        with (
            patch.object(playerok_browser, "QWebEnginePage", side_effect=AssertionError("must not create page")),
            patch.object(playerok_browser, "QWebEngineView", side_effect=AssertionError("must not create view")),
        ):
            self.assertFalse(panel.open_devtools())
        self.assertIn("откройте Playerok", panel.browser_status.text())
        panel.close()

    def test_marketplace_panel_has_browser_status_and_managed_polling(self):
        panel = MarketplacePanel(database=self.db)
        visible_text = " ".join(widget.text() for widget in panel.findChildren(QLabel))
        for legacy_label in ("Получить код", "Подтвердить код", "Подтвердить 2FA", "E-mail аккаунта"):
            self.assertNotIn(legacy_label, visible_text)
        self.assertNotIn("не подключён", visible_text.lower())
        self.assertIn("read-only", panel.status_label.text().lower())
        self.assertTrue(hasattr(panel, "_sales_poller"))
        self.assertTrue(60_000 <= panel._sales_poller.interval_ms <= 60 * 60_000)
        self.assertTrue(panel.check_browser_auth_button.isEnabled())
        self.assertFalse(hasattr(panel, "_sync_timer"))
        self.assertFalse(hasattr(panel, "_pending_initial_sync"))
        self.assertFalse(hasattr(panel, "_fetch_orders"))
        self.assertFalse(hasattr(panel, "playerok"))
        self.assertFalse(panel.import_order_button.isEnabled())
        panel.close()

    def test_auth_check_button_does_not_start_sales_sync_or_persist(self):
        class SyncService:
            def __init__(self):
                self.calls = []

            def sync_browser_snapshot(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                raise AssertionError("viewer-only check must not sync sales")

        service = SyncService()
        panel = MarketplacePanel(database=self.db, sync_service=service)
        results = []
        panel.browser_panel.check_authentication = lambda callback: callback(True, None)
        panel.browser_auth_status_changed.connect(results.append)
        with patch.object(
            self.db, "connect", side_effect=AssertionError("viewer-only check must not access SQLite")
        ):
            panel.check_browser_auth_button.click()
        self.assertIn("подтверждена", panel.status_label.text().lower())
        self.assertEqual(results, [True])
        self.assertEqual(service.calls, [])
        self.assertIsNone(panel._sales_reader)
        panel.close()

    def test_auth_check_uses_existing_page_and_only_viewer_endpoint(self):
        class SignalStub:
            def __init__(self):
                self.callback = None

            def connect(self, callback):
                self.callback = callback

            def disconnect(self, callback):
                if self.callback == callback:
                    self.callback = None

            def emit(self):
                if self.callback:
                    self.callback(object())

        class PageStub:
            def __init__(self):
                self.destroyed = SignalStub()
                self.requests = []

            def url(self):
                return QUrl("https://playerok.com/")

            def runJavaScript(self, script, callback):
                self.requests.append((script, callback))

        class WebStub:
            def __init__(self, page):
                self.existing_page = page

            def page(self):
                return self.existing_page

        panel = PlayerokBrowserPanel(database=self.db, enable_webengine=False)
        page = PageStub()
        web = WebStub(page)
        profile = object()
        panel.web = web
        panel._profile = profile
        panel._page_ready = True
        completed = []
        checker = panel.check_authentication(lambda ok, error: completed.append((ok, error)))
        script, started = page.requests.pop(0)
        self.assertIs(panel.web.page(), page)
        self.assertIs(panel._profile, profile)
        self.assertIn("https://bff.playerok.com/rest-api/public/viewer", script)
        self.assertIn('"method":"GET"', script)
        self.assertIn('"credentials":"include"', script)
        self.assertNotIn("/graphql", script)
        key = json.loads(re.search(r"const key=(\"[^\"]+\")", script).group(1))
        started(key)
        poll_script, polled = page.requests.pop(0)
        self.assertIn("JSON.stringify(envelope)", poll_script)
        polled(json.dumps({"state": "done", "payload": {"id": "mock-viewer-id"}}))

        self.assertEqual(completed, [(True, None)])
        self.assertFalse(checker.active)
        self.assertEqual(page.requests, [])
        self.assertIsNone(page.destroyed.callback)
        self.assertIn("подтверждена", panel.browser_status.text().lower())
        panel.close()

    def test_page_hostname_reads_only_location_hostname_from_existing_page(self):
        class SignalStub:
            def __init__(self):
                self.callback = None

            def connect(self, callback):
                self.callback = callback

            def disconnect(self, callback):
                if self.callback == callback:
                    self.callback = None

        class PageStub:
            def __init__(self, hostname):
                self.hostname = hostname
                self.destroyed = SignalStub()
                self.scripts = []

            def runJavaScript(self, script, callback):
                self.scripts.append(script)
                callback(self.hostname)

        class WebStub:
            def __init__(self, page):
                self.existing_page = page

            def page(self):
                return self.existing_page

        panel = PlayerokBrowserPanel(database=self.db, enable_webengine=False)
        for hostname in ("playerok.com", "www.playerok.com", None):
            with self.subTest(hostname=hostname):
                page = PageStub(hostname)
                panel.web = WebStub(page)
                panel._page_ready = True
                values = []
                panel.read_page_hostname(values.append)
                self.assertEqual(values, [hostname])
                self.assertEqual(page.scripts, ["location.hostname"])
                self.assertIs(panel.web.page(), page)
        panel.close()

    def test_diagnostic_report_shows_only_sanitized_hostnames(self):
        cases = (
            ("playerok.com", "playerok.com"),
            ("www.playerok.com", "www.playerok.com"),
            ("https://playerok.com/path?token=private", "unknown"),
            (None, "unknown"),
        )
        for page_hostname, expected in cases:
            with self.subTest(page_hostname=page_hostname):
                panel = MarketplacePanel(database=self.db)
                reader = SimpleNamespace(
                    pages_read=0, deals_read=0, initial_viewer_received=False,
                    pagination_complete=False, viewer_matched=None,
                    last_graphql_http_status=None, cancel=lambda **_kwargs: None,
                )
                callbacks = []
                panel.browser_panel.read_page_hostname = lambda callback, value=page_hostname: callback(value)
                panel.browser_panel.read_graphql_sales_snapshot = lambda callback, **_kwargs: (callbacks.append(callback) or reader)
                panel.diagnostic_sales_button.click()
                callbacks[0](None, "http_error: private response detail")
                report = panel.diagnostic_sales_report.text()
                self.assertIn(f"Page hostname: {expected}", report)
                self.assertNotIn("/path", report)
                self.assertNotIn("token=private", report)
                self.assertNotIn("private response detail", report)
                panel.close()

    def test_graphql_hostname_comes_from_reader_endpoint_and_hides_url_parts(self):
        panel = MarketplacePanel(database=self.db)
        endpoint = "https://graphql.example.test/private/path?token=private"
        reader = SimpleNamespace(
            pages_read=0, deals_read=0, initial_viewer_received=False,
            pagination_complete=False, viewer_matched=None,
            last_graphql_http_status=None, cancel=lambda **_kwargs: None,
        )
        callbacks = []
        panel.browser_panel.read_page_hostname = lambda callback: callback("playerok.com")
        panel.browser_panel.read_graphql_sales_snapshot = lambda callback, **_kwargs: (callbacks.append(callback) or reader)
        with patch.object(browser_sales, "_GRAPHQL_URL", endpoint):
            panel.diagnostic_sales_button.click()
            callbacks[0](None, "fetch_error: safe failure")
            report = panel.diagnostic_sales_report.text()
            self.assertEqual(panel._graphql_hostname(), "graphql.example.test")
            self.assertIn("GraphQL hostname: graphql.example.test", report)
            self.assertNotIn(endpoint, report)
            self.assertNotIn("/private/path", report)
            self.assertNotIn("token=private", report)
        panel.close()

    def test_diagnostic_report_shows_only_validated_graphql_http_status(self):
        cases = (
            (403, "403"), (429, "429"), (500, "500"),
            (None, "unknown"), (True, "unknown"), (99, "unknown"),
            (600, "unknown"), ("403", "unknown"),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                panel = MarketplacePanel(database=self.db)
                reader = SimpleNamespace(
                    pages_read=0, deals_read=0, initial_viewer_received=True,
                    pagination_complete=False, viewer_matched=None,
                    last_graphql_http_status=status, cancel=lambda **_kwargs: None,
                )
                callbacks = []
                panel.browser_panel.read_page_hostname = lambda callback: callback("playerok.com")
                panel.browser_panel.read_graphql_sales_snapshot = lambda callback, **_kwargs: (callbacks.append(callback) or reader)
                panel.diagnostic_sales_button.click()
                callbacks[0](None, "http_error: private response detail token=secret")
                report = panel.diagnostic_sales_report.text()
                self.assertIn(f"HTTP status: {expected}", report)
                self.assertNotIn("private response detail", report)
                self.assertNotIn("token=secret", report)
                panel.close()

    def test_diagnostic_sales_read_reports_trusted_result_without_side_effects(self):
        class SyncService:
            def __init__(self):
                self.calls = []

            def sync_browser_snapshot(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                raise AssertionError("diagnostic read must not invoke sync_service")

        service = SyncService()
        panel = MarketplacePanel(database=self.db, sync_service=service)
        snapshot = trusted_graphql_snapshot("private-account-id", [{
            "deal_id": "private-deal-id", "listing_id": "private-listing-id",
            "chat_id": "private-chat-id", "status": "PAID", "buyer": "private-buyer",
        }])
        reader = SimpleNamespace(
            pages_read=1, deals_read=1, initial_viewer_received=True,
            pagination_complete=True, viewer_matched=True,
            cancel=lambda **_kwargs: None,
        )
        callbacks = []
        calls = []

        def read_graphql_sales(callback, **_kwargs):
            calls.append(1)
            callbacks.append(callback)
            return reader

        panel.browser_panel.read_graphql_sales_snapshot = read_graphql_sales
        with (
            patch.object(self.db, "connect", side_effect=AssertionError("diagnostic must not open SQLite")),
            patch.object(panel, "_start_sales_sync", side_effect=AssertionError("diagnostic must bypass normal sync")) as normal_sync,
            patch.object(PlayerokFreshnessGate, "issue_from_complete_snapshot", side_effect=AssertionError("no proof")) as issue,
            patch.object(PlayerokFreshnessGate, "consume_for_claim", side_effect=AssertionError("no proof claim")) as consume,
            patch.object(DeliveryDispatcher, "dispatch_leg", side_effect=AssertionError("no dispatch")) as dispatch,
        ):
            panel.diagnostic_sales_button.click()
            self.assertFalse(panel.diagnostic_sales_button.isEnabled())
            self.assertFalse(panel.sync_sales_button.isEnabled())
            callbacks[0](snapshot, None)
            report = panel.diagnostic_sales_report.text()
            self.assertIn("Диагностическое чтение: успешно", report)
            self.assertIn("Страниц прочитано: 1", report)
            self.assertIn("Сделок: 1", report)
            self.assertIn("Полная пагинация: да", report)
            self.assertIn("Viewer совпал: да", report)
            self.assertIn("Trusted snapshot: зарегистрирован", report)
            for private_value in (
                "private-account-id", "private-deal-id", "private-listing-id",
                "private-chat-id", "private-buyer",
            ):
                self.assertNotIn(private_value, report)
            self.assertTrue(is_fresh_trusted_graphql_sales_snapshot(snapshot))
            self.assertEqual(service.calls, [])
            self.assertEqual(calls, [1])
            normal_sync.assert_not_called()
            issue.assert_not_called()
            consume.assert_not_called()
            dispatch.assert_not_called()
        self.assertIsNone(panel._diagnostic_sales_reader)
        panel.close()

    def test_saved_sales_rows_carry_browser_account_for_scoped_canonical_import(self):
        panel = MarketplacePanel(database=self.db, sync_service=PlayerokDealSyncService(self.db))
        snapshot = trusted_graphql_snapshot("seller-account", [
            {"deal_id": "deal-account-scoped", "listing_id": None, "status": "PAID"}
        ])
        panel._sales_read_completed(snapshot, None)
        order = panel.orders_table.item(0, 0).data(Qt.ItemDataRole.UserRole)
        self.assertEqual(order["account_user_id"], "seller-account")
        self.assertIsNone(order["listing_id"])
        with self.db.connect() as db:
            row = db.execute(
                "SELECT playerok_account_id,external_deal_id,listing_id,listing_resolution_status "
                "FROM gd_deals"
            ).fetchone()
            self.assertEqual(tuple(row), ("seller-account", "deal-account-scoped", None, "unresolved"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)
        panel.close()

    def test_diagnostic_sales_read_failure_shows_only_safe_error_category(self):
        panel = MarketplacePanel(database=self.db)
        reader = SimpleNamespace(
            pages_read=0, deals_read=0, initial_viewer_received=True,
            pagination_complete=False, viewer_matched=None,
            cancel=lambda **_kwargs: None,
        )
        callbacks = []
        panel.browser_panel.read_graphql_sales_snapshot = lambda callback, **_kwargs: (callbacks.append(callback) or reader)
        with patch.object(self.db, "connect", side_effect=AssertionError("diagnostic must not open SQLite")):
            panel.diagnostic_sales_button.click()
            callbacks[0](None, "http_error: private response details")
        report = panel.diagnostic_sales_report.text()
        self.assertIn("Диагностическое чтение: не подтверждено", report)
        self.assertIn("Ошибка: http_error", report)
        self.assertNotIn("private response details", report)
        self.assertIn("Trusted snapshot: нет", report)
        panel.close()

    def test_diagnostic_sales_report_includes_only_validated_http_status(self):
        cases = (
            ("http_error: Playerok ответил HTTP 401.", "Ошибка: http_error (HTTP 401)"),
            ("http_error: Playerok ответил HTTP 403.", "Ошибка: http_error (HTTP 403)"),
            ("http_error: Playerok ответил HTTP 429.", "Ошибка: http_error (HTTP 429)"),
            ("http_error: Playerok ответил HTTP 500.", "Ошибка: http_error (HTTP 500)"),
            ("http_error: private response detail", "Ошибка: http_error"),
            ("http_error: Playerok ответил HTTP 099.", "Ошибка: http_error"),
            ("http_error: Playerok ответил HTTP 600.", "Ошибка: http_error"),
            (
                "http_error: Playerok ответил HTTP 403. token=secret Cookie=private",
                "Ошибка: http_error",
            ),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                panel = MarketplacePanel(database=self.db)
                reader = SimpleNamespace(
                    pages_read=0, deals_read=0, initial_viewer_received=True,
                    pagination_complete=False, viewer_matched=None,
                    cancel=lambda **_kwargs: None,
                )
                callbacks = []
                panel.browser_panel.read_graphql_sales_snapshot = lambda callback, **_kwargs: (
                    callbacks.append(callback) or reader
                )
                panel.diagnostic_sales_button.click()
                callbacks[0](None, error)
                report = panel.diagnostic_sales_report.text()
                self.assertIn(expected, report)
                for sensitive in ("secret", "private", "token", "Cookie"):
                    self.assertNotIn(sensitive, report)
                panel.close()

    def test_diagnostic_sales_read_rejects_incomplete_pagination_and_viewer_mismatch(self):
        scenarios = (
            (
                SimpleNamespace(
                    pages_read=1, deals_read=1, initial_viewer_received=True,
                    pagination_complete=False, viewer_matched=None,
                    cancel=lambda **_kwargs: None,
                ),
                "Playerok вернул повторный курсор пагинации.", "pagination_error",
            ),
            (
                SimpleNamespace(
                    pages_read=1, deals_read=1, initial_viewer_received=True,
                    pagination_complete=True, viewer_matched=False,
                    cancel=lambda **_kwargs: None,
                ),
                "Аккаунт Playerok изменился во время чтения продаж; снимок отклонён.", "viewer_mismatch",
            ),
        )
        for reader, error, category in scenarios:
            with self.subTest(category=category):
                panel = MarketplacePanel(database=self.db)
                callbacks = []
                panel.browser_panel.read_graphql_sales_snapshot = lambda callback, **_kwargs: (callbacks.append(callback) or reader)
                panel.diagnostic_sales_button.click()
                callbacks[0](None, error)
                report = panel.diagnostic_sales_report.text()
                self.assertIn("Диагностическое чтение: не подтверждено", report)
                self.assertIn(f"Ошибка: {category}", report)
                self.assertIn("Trusted snapshot: нет", report)
                panel.close()

    def test_diagnostic_sales_read_rejects_snapshot_without_trusted_provenance(self):
        panel = MarketplacePanel(database=self.db)
        untrusted_snapshot = BrowserSalesSnapshot("private-account", [{"deal_id": "private-deal"}])
        reader = SimpleNamespace(
            pages_read=1, deals_read=1, initial_viewer_received=True,
            pagination_complete=True, viewer_matched=True,
            cancel=lambda **_kwargs: None,
        )
        callbacks = []
        panel.browser_panel.read_graphql_sales_snapshot = lambda callback, **_kwargs: (callbacks.append(callback) or reader)
        panel.diagnostic_sales_button.click()
        callbacks[0](untrusted_snapshot, None)
        report = panel.diagnostic_sales_report.text()
        self.assertIn("Диагностическое чтение: не подтверждено", report)
        self.assertIn("Ошибка: provenance_error", report)
        self.assertIn("Trusted snapshot: нет", report)
        self.assertNotIn("private-account", report)
        self.assertNotIn("private-deal", report)
        panel.close()

    def test_repeated_diagnostic_click_is_blocked_while_reader_is_active(self):
        panel = MarketplacePanel(database=self.db)
        reader = SimpleNamespace(
            pages_read=0, deals_read=0, initial_viewer_received=False,
            pagination_complete=False, viewer_matched=None,
            cancel=lambda **_kwargs: None,
        )
        callbacks = []
        calls = []

        def read_graphql_sales(callback, **_kwargs):
            calls.append(1)
            callbacks.append(callback)
            return reader

        panel.browser_panel.read_graphql_sales_snapshot = read_graphql_sales
        panel.diagnostic_sales_button.click()
        panel.diagnostic_sales_button.click()
        self.assertEqual(calls, [1])
        self.assertFalse(panel.diagnostic_sales_button.isEnabled())
        callbacks[0](None, "poll_state_error: cancelled")
        self.assertTrue(panel.diagnostic_sales_button.isEnabled())
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

    def test_delivery_browser_read_rejects_wrong_viewer_before_persisting_snapshot(self):
        service = PlayerokDealSyncService(self.db)
        panel = MarketplacePanel(database=self.db, sync_service=service)
        pending = []

        class Reader:
            def cancel(self, _reason):
                pass

        panel.browser_panel.read_graphql_sales_snapshot = lambda callback, *, expected_account_user_id=None: (
            pending.append(callback) or Reader()
        )
        completed = []
        panel.read_sales_for_delivery("expected-account", lambda snapshot, error: completed.append((snapshot, error)))
        snapshot = trusted_graphql_snapshot("other-account", [
            {"deal_id": "deal-1", "listing_id": "listing-1", "status": "PAID"}
        ])
        pending[0](snapshot, None)

        self.assertIsNone(completed[0][0])
        self.assertIn("не совпадает", completed[0][1])
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playerok_sync_state").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 0)
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
