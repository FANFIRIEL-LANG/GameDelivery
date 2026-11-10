import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication, QComboBox, QGridLayout, QLabel, QLineEdit, QScrollArea,
    QPushButton, QTableWidget, QWidget,
)

from app.game_inventory_cards import CS2InventoryGroupCard
from app.game_inventory_panel import GameInventoryPanel
from games.cs2.inventory_view import CS2InventoryInstance, build_cs2_inventory_projection
from games.cs2.pricing import CachedPrice, PriceState
from games.cs2.price_store import PriceCacheStore
from games.inventory import InventoryItem
from integrations.openskin.client import PricePosition


class CS2PricePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def panel_with_items(self, items):
        panel = GameInventoryPanel.__new__(GameInventoryPanel)
        QWidget.__init__(panel)
        panel.game_combo = QComboBox()
        panel.game_combo.addItem("CS2", "cs2")
        panel.search = QLineEdit()
        panel.item_filter = QComboBox()
        panel.item_filter.addItem("Все предметы", "all")
        panel.rarity_filter = QComboBox()
        panel.rarity_filter.addItem("Любая редкость", "")
        panel.quality_filter = QComboBox()
        panel.quality_filter.addItem("Любое качество", "")
        panel.transfer_filter = QComboBox()
        panel.transfer_filter.addItem("Любая передаваемость", "all")
        panel.grouping_mode = QComboBox()
        panel.grouping_mode.addItem("Общий вид", "shared")
        panel.grouping_mode.addItem("Раздельно по Asset", "separate")
        panel.view_mode = QComboBox()
        panel.view_mode.addItem("Карточки", "cards")
        panel.view_mode.addItem("Таблица", "table")
        panel.table = QTableWidget()
        panel.scroll = QScrollArea()
        panel.cards_host = QWidget()
        panel.cards_layout = QGridLayout(panel.cards_host)
        panel.cards_by_image = {}
        panel.image_loader = Mock(cache={})
        panel.width = Mock(return_value=620)
        panel.items = [InventoryItem(
            "cs2", item.bot_name, item.asset_id, item.class_id, item.instance_id,
            item.name, item_type=item.item_type, market_hash_name=item.market_hash_name,
            is_tradable=item.is_tradable, is_marketable=item.is_marketable,
        ) for item in items]
        panel.cs2_projection = build_cs2_inventory_projection(items)
        panel.cs2_all_instances = tuple(items)
        panel.cs2_snapshot_meta = {name: len([x for x in items if x.bot_name == name])
                                   for name in {x.bot_name for x in items}}
        from types import SimpleNamespace
        bot_names = sorted({x.bot_name for x in items})
        panel.bot_selection_model = SimpleNamespace(
            account_names=bot_names, selected_names=bot_names
        )
        panel.cs2_all_valuation = None
        panel.price_quotes = {}
        panel.usd_rub = None
        panel.cs2_valuation = None
        panel.price_cache_store = None
        panel.price_refresh_button = QPushButton()
        panel._price_refresh_running = False
        panel.database_path = ":memory:"
        panel.status = Mock()
        panel.price_summary = Mock()
        return panel

    def test_separate_cs2_card_mode_displays_each_physical_asset(self):
        first = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "asset-1", "class", "0", 4,
            "AK-47 | Redline", "Rifle", "AK-47 | Redline", True, True,
        )
        second = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "asset-2", "class", "0", 4,
            "AK-47 | Redline", "Rifle", "AK-47 | Redline", True, True,
        )
        panel = self.panel_with_items([first, second])
        panel.grouping_mode.setCurrentIndex(1)

        panel.render()

        self.assertEqual(panel.cards_layout.count(), 2)
        asset_ids = {
            panel.cards_layout.itemAt(index).widget().group.instances[0].asset_id
            for index in range(panel.cards_layout.count())
        }
        self.assertEqual(asset_ids, {"asset-1", "asset-2"})

    def test_group_card_displays_ruble_unit_and_group_value(self):
        item = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "asset-1", "class", "0", 5,
            "AK-47 | Redline", "Rifle", "AK-47 | Redline", False, True,
        )
        group = build_cs2_inventory_projection([item]).groups[0]

        card = CS2InventoryGroupCard(
            group, price_text="Цена единицы: ₽90.00 · Стоимость группы: ₽180.00"
        )

        visible = " ".join(label.text() for label in card.findChildren(QLabel))
        self.assertIn("Цена: ₽90.00", visible)
        self.assertNotIn("₽180.00", visible)
        self.assertNotIn("есть устаревшие данные", visible)

    def test_card_price_text_is_compact_and_hides_stale_marker(self):
        from app.game_inventory_cards import compact_cs2_card_price_text

        self.assertEqual(
            compact_cs2_card_price_text(
                "Цена единицы: ₽152.18 · Стоимость группы: ₽152.18 · есть устаревшие данные",
                1,
            ),
            "Цена: ₽152.18",
        )
        self.assertEqual(
            compact_cs2_card_price_text(
                "Цена единицы: ₽90.00 · Стоимость группы: ₽180.00 · есть устаревшие данные",
                2,
            ),
            "Цена: ₽90.00/шт. · всего: ₽180.00",
        )
        self.assertEqual(
            compact_cs2_card_price_text(
                "Цена: $0.03 · Не учитывается в оценке — цена не выше $0.05",
                1,
            ),
            "Цена: $0.03 (не учитывается)",
        )

    def test_below_threshold_card_keeps_usd_and_omits_ruble_price(self):
        item = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "asset-1", "class", "0", 1,
            "Kilowatt Case", "Container", "Kilowatt Case", True, True,
        )
        group = build_cs2_inventory_projection([item]).groups[0]

        card = CS2InventoryGroupCard(
            group, price_text="Цена: $0.03 · Не учитывается в оценке — цена не выше $0.05"
        )

        visible = " ".join(label.text() for label in card.findChildren(QLabel))
        self.assertIn("$0.03", visible)
        self.assertIn("не учитывается", visible)
        self.assertNotIn("не выше $0.05", visible)
        self.assertNotIn("₽", visible)

    def test_separate_table_rows_show_individual_asset_price(self):
        items = [CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", f"asset-{index}", "class", "0", 9,
            "AK-47 | Redline", "Rifle", "AK-47 | Redline", True, True,
        ) for index in (1, 2)]
        panel = self.panel_with_items(items)
        position = PricePosition(730, "AK-47 | Redline")
        panel.price_quotes = {position: CachedPrice(
            position, Decimal("1.00"), "USD", None, None, PriceState.CURRENT,
        )}
        panel.usd_rub = Decimal("90")
        panel.grouping_mode.setCurrentIndex(1)
        panel.view_mode.setCurrentIndex(1)

        panel.render()

        self.assertEqual(panel.table.rowCount(), 2)
        for row in range(2):
            self.assertIn("Цена актива: ₽90.00", panel.table.item(row, 11).text())

    def test_shared_card_and_summary_use_physical_assets_and_selected_bot_totals(self):
        items = [CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", f"asset-{index}", "class", "0", 6,
            "AK-47 | Redline", "Rifle", "AK-47 | Redline", False, True,
        ) for index in (1, 2)]
        panel = self.panel_with_items(items)
        position = PricePosition(730, "AK-47 | Redline")
        panel.price_quotes = {position: CachedPrice(
            position, Decimal("1.00"), "USD", "2026-11-09T10:00:00Z",
            "2026-11-09T10:01:00+00:00", PriceState.CURRENT,
        )}
        panel.usd_rub = Decimal("90")

        panel.render()

        self.assertEqual(panel.cards_layout.count(), 1)
        card = panel.cards_layout.itemAt(0).widget()
        visible = " ".join(label.text() for label in card.findChildren(QLabel))
        self.assertIn("Цена: ₽90.00/шт. · всего: ₽180.00", visible)
        self.assertNotIn("есть устаревшие данные", visible)
        summary = panel.price_summary.setText.call_args.args[0]
        self.assertIn("ВЫБРАНО: 1 из 1 бота · 2 предмета · ₽180.00", summary)
        self.assertIn("ВСЕ БОТЫ: 1 бот · 2 предмета · ₽180.00", summary)

    def test_price_cache_and_totals_include_all_saved_bots_not_only_selected(self):
        from types import SimpleNamespace

        items = [
            CS2InventoryInstance(
                "cs2", 730, 2, "Bot A", "a1", "class", "0", 1,
                "AK-47 | Redline", "Rifle", "AK-47 | Redline", True, True,
            ),
            CS2InventoryInstance(
                "cs2", 730, 2, "Bot B", "b1", "class", "0", 1,
                "M4A1-S | Decimator", "Rifle", "M4A1-S | Decimator", True, True,
            ),
        ]
        panel = self.panel_with_items(items)
        panel._selected_bot_names = Mock(return_value=["Bot A"])
        panel.bot_selection_model.account_names = ["Bot A", "Bot B", "Bot C"]
        panel.cs2_snapshot_meta = {"Bot A": 1, "Bot B": 1}
        quote_a = CachedPrice(
            PricePosition(730, "AK-47 | Redline"), Decimal("1.00"), "USD",
            None, None, PriceState.CURRENT,
        )
        quote_b = CachedPrice(
            PricePosition(730, "M4A1-S | Decimator"), Decimal("2.00"), "USD",
            None, None, PriceState.CURRENT,
        )
        panel.price_cache_store = Mock()
        panel.price_cache_store.get.side_effect = lambda position: {
            quote_a.position: quote_a, quote_b.position: quote_b,
        }.get(position)
        panel.price_cache_store.get_fx_rate.return_value = SimpleNamespace(
            rate=Decimal("100"), source_date="2026-11-10",
            fetched_at="2026-11-10T10:00:00+00:00", is_stale=False,
        )

        panel._load_price_cache()

        self.assertEqual(panel.cs2_valuation.selected_total_rub, Decimal("100.00"))
        self.assertEqual(panel.cs2_all_valuation.selected_total_rub, Decimal("300.00"))
        summary = panel.price_summary.setText.call_args.args[0]
        self.assertIn("ВЫБРАНО: 1 из 3 ботов · 1 предмет · ₽100.00", summary)
        self.assertIn("ВСЕ БОТЫ: 3 бота · 2 предмета · ₽300.00", summary)
        self.assertIn("всего 2/3 аккаунтов", summary)

    def test_price_network_work_is_submitted_to_worker_pool(self):
        instance = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "asset-1", "class", "0", 1,
            "AK-47 | Redline", "Rifle", "AK-47 | Redline", True, True,
        )
        panel = self.panel_with_items([instance])
        position = PricePosition(730, "AK-47 | Redline")
        store = Mock()
        store.due_positions.return_value = (position,)
        store.fx_rate_due.return_value = False
        panel.price_cache_store = store
        pool = Mock()

        from unittest.mock import patch
        with patch("app.game_inventory_panel.QThreadPool.globalInstance", return_value=pool):
            panel._start_price_refresh()

        pool.start.assert_called_once()
        self.assertTrue(panel._price_refresh_running)
        self.assertIs(panel._price_worker, pool.start.call_args.args[0])

    def test_worker_does_not_fetch_or_convert_fx_for_below_threshold_price(self):
        position = PricePosition(730, "Kilowatt Case")
        with tempfile.TemporaryDirectory() as temp:
            database = os.path.join(temp, "prices.sqlite3")
            store = PriceCacheStore(database)
            now = datetime.now(timezone.utc)
            store.save_quote(CachedPrice(
                position, Decimal("0.03"), "USD", now.isoformat(),
                now.isoformat(), PriceState.BELOW_THRESHOLD,
            ), now=now)

            from unittest.mock import patch
            with patch("app.game_inventory_panel.OpenSkinClient") as openskin, \
                    patch("app.game_inventory_panel.FrankfurterClient") as frankfurter:
                from app.game_inventory_panel import _PriceRefreshTask
                _PriceRefreshTask(database, [position]).run()

        openskin.assert_not_called()
        frankfurter.assert_not_called()

    def test_worker_applies_saved_proxy_only_to_openskin(self):
        from unittest.mock import patch
        from integrations.openskin.client import OpenSkinPrice
        from integrations.openskin.proxy import OpenSkinProxy
        from integrations.frankfurter.client import ExchangeRate

        position = PricePosition(730, "AK-47 | Redline")
        proxy = OpenSkinProxy("socks5", "proxy.example", 1080, "proxy-user", "hidden")
        with tempfile.TemporaryDirectory() as temp:
            database = os.path.join(temp, "prices.sqlite3")
            with patch("app.game_inventory_panel.OpenSkinProxySettingsStore") as settings_store, \
                    patch("app.game_inventory_panel.OpenSkinClient") as openskin, \
                    patch("app.game_inventory_panel.FrankfurterClient") as frankfurter:
                settings_store.return_value.load_enabled_proxy.return_value = proxy
                openskin.return_value.fetch_prices.return_value = (
                    OpenSkinPrice(position, Decimal("1.00"), "USD", "2026-11-09T10:00:00Z"),
                )
                frankfurter.return_value.fetch_usd_rub.return_value = ExchangeRate(
                    "USD", "RUB", Decimal("90"), "2026-11-09",
                    datetime(2026, 11, 9, 10, 1, tzinfo=timezone.utc),
                )

                from app.game_inventory_panel import _PriceRefreshTask
                _PriceRefreshTask(database, [position]).run()

        settings_store.return_value.load_enabled_proxy.assert_called_once_with()
        openskin.assert_called_once_with(proxy=proxy)
        openskin.return_value.fetch_prices.assert_called_once_with((position,))
        frankfurter.assert_called_once_with()
        frankfurter.return_value.fetch_usd_rub.assert_called_once_with()

    def test_disabled_proxy_routes_openskin_directly_and_keeps_frankfurter_separate(self):
        from unittest.mock import patch
        from integrations.openskin.client import OpenSkinPrice

        position = PricePosition(730, "Kilowatt Case")
        with tempfile.TemporaryDirectory() as temp:
            database = os.path.join(temp, "prices.sqlite3")
            with patch("app.game_inventory_panel.OpenSkinProxySettingsStore") as settings_store, \
                    patch("app.game_inventory_panel.OpenSkinClient") as openskin, \
                    patch("app.game_inventory_panel.FrankfurterClient") as frankfurter:
                settings_store.return_value.load_enabled_proxy.return_value = None
                openskin.return_value.fetch_prices.return_value = (
                    OpenSkinPrice(position, Decimal("0.03"), "USD", None),
                )

                from app.game_inventory_panel import _PriceRefreshTask
                _PriceRefreshTask(database, [position]).run()

        settings_store.return_value.load_enabled_proxy.assert_called_once_with()
        openskin.assert_called_once_with(proxy=None)
        openskin.return_value.fetch_prices.assert_called_once_with((position,))
        frankfurter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
