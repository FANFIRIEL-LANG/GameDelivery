import os
import sys
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QGridLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QTableWidget, QWidget,
)
from PySide6.QtGui import QIcon, QPixmap

from app.game_inventory_panel import GameInventoryPanel
from app.game_inventory_cards import CS2InventoryGroupCard, GameItemCard
from app.asf_management import ASFManagementWidget
from app.inventory_panel import InventoryPanel
from games.cs2.inventory_view import (
    CS2InventoryInstance, RestrictionStatus, RestrictionType,
    build_cs2_inventory_projection,
)
from games.inventory import InventoryItem, parse_game_inventory
from integrations.asf.client import ASFError


def response(bot="Bot A", asset="1001", amount="2"):
    return {"Success": True, "Result": {bot: {
        "Assets": [{"assetid": asset, "classid": "2001", "instanceid": "0", "amount": amount}],
        "Descriptions": [{
            "classid": "2001", "instanceid": "0", "name": "AK-47 | Redline",
            "type": "Classified Rifle", "market_hash_name": "AK-47 | Redline",
            "icon_url": "/economy/image/cs2-item-icon",
            "tradable": 1, "marketable": 0,
            "tags": [{"category": "Rarity", "localized_tag_name": "Classified"}],
        }],
    }}}


class CS2InventoryPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def bare_panel(self, game="cs2"):
        panel = GameInventoryPanel.__new__(GameInventoryPanel)
        QWidget.__init__(panel)
        panel.game_combo = Mock()
        panel.game_combo.currentData.return_value = game
        panel.game_combo.currentText.return_value = game
        panel.test_mode = False
        panel._selected_bot_names = Mock(return_value=["Bot A"])
        panel.load_button = QPushButton()
        panel.password = QLineEdit()
        panel.status = QLabel()
        panel.price_summary = QLabel()
        panel.price_summary.setMinimumHeight(76)
        panel.price_summary.setStyleSheet("font-size: 16px; font-weight: 700;")
        panel.store = Mock()
        panel.store.load_snapshot_meta.return_value = {}
        panel.price_cache_store = Mock()
        panel.price_cache_store.get.return_value = None
        panel.price_cache_store.get_fx_rate.return_value = None
        panel.price_refresh_button = QPushButton()
        panel._price_refresh_running = True
        panel.items = []
        panel.cs2_instances_by_bot = {}
        from types import SimpleNamespace
        panel.bot_selection_model = SimpleNamespace(account_names=["Bot A"])
        panel.cs2_all_instances = ()
        panel.cs2_snapshot_meta = {}
        panel.cs2_all_valuation = None
        panel.price_quotes = {}
        panel.usd_rub = None
        panel.cs2_valuation = None
        panel.load_saved = Mock()
        return panel

    def test_cs2_fetch_uses_one_response_for_adapter_and_persistence(self):
        panel = self.bare_panel()
        client = Mock()
        payload = response()
        client.get_bot_inventory.return_value = payload
        parsed = parse_game_inventory(payload, "cs2", "Bot A")
        instance = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "1001", "2001", "0", 2,
            "AK-47 | Redline", "Classified Rifle", "AK-47 | Redline",
            True, False, icon_url="/economy/image/raw-adapter-icon",
        )
        with patch("app.game_inventory_panel.ASFClient", return_value=client), \
                patch("app.game_inventory_panel.parse_game_inventory", return_value=parsed) as parser, \
                patch("app.game_inventory_panel.adapt_cs2_inventory_response", return_value=(instance,)) as adapter:
            panel.fetch_inventory()

        client.get_bot_inventory.assert_called_once_with(
            "Bot A", app_id=730, context_id=2, language="english"
        )
        parser.assert_called_once_with(payload, "cs2", "Bot A")
        adapter.assert_called_once_with(payload, "Bot A", appid=730, context_id=2)
        panel.store.replace.assert_called_once_with("cs2", "Bot A", parsed)
        saved_instance = panel.cs2_instances_by_bot["Bot A"][0]
        self.assertEqual(
            saved_instance.icon_url,
            "https://community.cloudflare.steamstatic.com/economy/image/cs2-item-icon",
        )
        self.assertEqual(saved_instance.asset_id, instance.asset_id)

    def test_other_games_keep_existing_refresh_path(self):
        panel = self.bare_panel(game="dota2")
        with patch("app.game_inventory_panel.ASFClient") as client_factory, \
                patch("app.game_inventory_panel.refresh_game_inventory") as refresh:
            panel.fetch_inventory()

        client_factory.assert_called_once()
        refresh.assert_called_once_with(client_factory.return_value, panel.store, "dota2", "Bot A")
        client_factory.return_value.get_bot_inventory.assert_not_called()

    def test_test_mode_refuses_non_cs2_inventory_requests(self):
        panel = self.bare_panel(game="dota2")
        panel.test_mode = True
        panel._selected_bot_names.return_value = ["IsolatedTestBot"]
        with patch("app.game_inventory_panel.ASFClient") as client_factory:
            panel.fetch_inventory()

        client_factory.assert_not_called()
        self.assertIn("только проверка инвентаря CS2", panel.status.text())

    def test_group_card_shows_quantity_owners_and_independent_statuses(self):
        first = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "1001", "2001", "0", 2,
            "AK-47 | Redline", "Classified Rifle", "AK-47 | Redline", True, False,
            tags=("Classified",),
            restriction_type=RestrictionType.UNKNOWN,
            restriction_status=RestrictionStatus.UNKNOWN,
        )
        second = CS2InventoryInstance(
            "cs2", 730, 2, "Bot B", "1002", "2001", "0", 1,
            "AK-47 | Redline", "Classified Rifle", "AK-47 | Redline", True, False,
            tags=("Classified",),
            restriction_type=RestrictionType.UNKNOWN,
            restriction_status=RestrictionStatus.UNKNOWN,
        )
        from games.cs2.inventory_view import build_cs2_inventory_projection
        group = build_cs2_inventory_projection([first, second]).groups[0]

        card = CS2InventoryGroupCard(group, rarity="Classified")
        labels = [label.text() for label in card.findChildren(QLabel)]
        rendered = " ".join(labels)
        self.assertIn("Количество: 2", rendered)
        self.assertIn("Цена неизвестна", rendered)
        self.assertNotIn("Цена: 0", rendered)
        self.assertNotIn("Передаваемый", rendered)
        self.assertIn("#d32ce6", card.styleSheet())
        self.assertIn("color: #d32ce6", card.findChild(QLabel, "itemTitle").styleSheet())
        self.assertIn("background: #20382f", card.styleSheet())
        self.assertIn("AK-47 | Redline", card.toolTip())
        self.assertNotIn("Передаваемый", card.toolTip())
        self.assertNotIn("Можно продать", card.toolTip())
        self.assertNotIn("asset", card.toolTip())
        self.assertNotIn("ASF amount", card.toolTip())
        self.assertNotIn("market_tradable_restriction", card.toolTip())
        self.assertNotIn("Характеристики:", card.toolTip())

    def test_group_card_shows_stattrak_only_when_present_and_is_compact(self):
        from games.cs2.inventory_view import build_cs2_inventory_projection

        normal = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "1001", "2001", "0", 1,
            "AK-47 | Redline", "Classified Rifle", "AK-47 | Redline",
            True, True, tags=("Classified",), exterior="Field-Tested",
        )
        stattrak = replace(
            normal, asset_id="1002", name="StatTrak™ AK-47 | Redline",
            market_hash_name="StatTrak™ AK-47 | Redline",
            tags=("Classified", "StatTrak"),
        )
        normal_card = CS2InventoryGroupCard(build_cs2_inventory_projection([normal]).groups[0])
        stattrak_card = CS2InventoryGroupCard(build_cs2_inventory_projection([stattrak]).groups[0])

        normal_visible = " ".join(label.text() for label in normal_card.findChildren(QLabel))
        stattrak_visible = " ".join(label.text() for label in stattrak_card.findChildren(QLabel))
        self.assertNotIn("StatTrak", normal_visible)
        self.assertIn("StatTrak", stattrak_visible)
        self.assertIn("Field-Tested", normal_visible)
        self.assertIn("Количество: 1", normal_visible)
        self.assertEqual(normal_card.height(), 220)

    def test_group_card_uses_only_typed_rarity_source(self):
        from games.cs2.inventory_view import build_cs2_inventory_projection

        item = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "1001", "2001", "0", 1,
            "AK-47 | Redline", "Classified Rifle", "AK-47 | Redline",
            True, True, tags=("Classified",),
        )
        card = CS2InventoryGroupCard(build_cs2_inventory_projection([item]).groups[0])

        self.assertIn("border: 2px solid #596273", card.styleSheet())

    def test_game_item_card_shows_known_quality_and_keeps_it_in_tooltip(self):
        item = InventoryItem(
            "cs2", "Bot A", "asset-1", "class-1", "0", "AK-47 | Redline",
            market_hash_name="AK-47 | Redline (Factory New)",
            rarity="Classified", exterior="Factory New", tags=("Classified",),
        )
        card = GameItemCard(item)
        visible = " ".join(label.text() for label in card.findChildren(QLabel))
        self.assertIn("Factory New", visible)
        self.assertIn("Factory New", card.toolTip())
        self.assertIn("#d32ce6", card.styleSheet())

    def test_game_item_card_does_not_invent_missing_quality(self):
        item = InventoryItem("cs2", "Bot A", "asset-1", "class-1", "0", "AK-47")

        card = GameItemCard(item)

        visible = " ".join(label.text() for label in card.findChildren(QLabel))
        self.assertNotIn("Качество:", visible)
        self.assertIn("Качество / износ", card.toolTip())

    def test_cs2_group_card_shows_mixed_transfer_status_and_keeps_physical_details(self):
        from games.cs2.inventory_view import build_cs2_inventory_projection

        base = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "1001", "2001", "0", 1,
            "Kilowatt Case", "Container", "Kilowatt Case", True, True,
            exterior="Minimal Wear",
            restriction_type=RestrictionType.NONE,
            restriction_status=RestrictionStatus.AVAILABLE,
        )
        blocked = replace(
            base, asset_id="1002", bot_name="Bot B", is_tradable=False,
            is_marketable=False, market_tradable_restriction=7,
            restriction_type=RestrictionType.UNKNOWN,
            restriction_status=RestrictionStatus.RESTRICTED,
        )
        unknown = replace(base, asset_id="1003", bot_name="Bot C", is_tradable=None,
                          is_marketable=None)
        group, = build_cs2_inventory_projection([base, blocked, unknown]).groups

        card = CS2InventoryGroupCard(group)
        visible = " ".join(label.text() for label in card.findChildren(QLabel))

        self.assertNotIn("Доступно:", visible)
        self.assertNotIn("Ограничено:", visible)
        self.assertNotIn("Статус неизвестен:", visible)
        self.assertIn("Minimal Wear", visible)
        self.assertNotIn("Количество ASF", visible)
        self.assertIn("Kilowatt Case", card.toolTip())
        self.assertNotIn("asset", card.toolTip())
        self.assertNotIn("market_tradable_restriction", card.toolTip())
        self.assertNotIn("Передаваемый", card.toolTip())
        self.assertIn("border: 2px solid #596273", card.styleSheet())
        self.assertIn("background: #302b2c", card.styleSheet())

    def test_nontradable_group_card_is_muted_and_keeps_rarity_border(self):
        from games.cs2.inventory_view import build_cs2_inventory_projection

        item = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "1001", "2001", "0", 1,
            "Kilowatt Case", "Container", "Kilowatt Case", False, True,
            tags=("Classified",),
        )
        group, = build_cs2_inventory_projection([item]).groups

        card = CS2InventoryGroupCard(group, rarity="Classified")

        self.assertIsNotNone(card.image.graphicsEffect())
        self.assertEqual(card.image.graphicsEffect().opacity(), 0.55)
        self.assertIn("background: #303036", card.styleSheet())
        self.assertNotIn("Непередаваемые:", " ".join(l.text() for l in card.findChildren(QLabel)))
        self.assertIn("border: 2px solid #d32ce6", card.styleSheet())
        self.assertIn("color: #d32ce6", card.findChild(QLabel, "itemTitle").styleSheet())

    def test_permanent_restriction_has_no_countdown(self):
        from datetime import datetime, timezone

        item = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "1001", "2001", "0", 1,
            "Kilowatt Case", "Container", "Kilowatt Case", False, True,
            restriction_type=RestrictionType.PERMANENTLY_NON_TRADABLE,
            restriction_status=RestrictionStatus.RESTRICTED,
            tradable_after=datetime(2026, 11, 10, tzinfo=timezone.utc),
        )
        card = CS2InventoryGroupCard(build_cs2_inventory_projection([item]).groups[0])

        self.assertNotIn("Постоянно непередаваемый", card.toolTip())
        self.assertNotIn("Снятие ограничения", card.toolTip())

    def test_unknown_release_deadline_never_treats_expiry_as_tradable(self):
        from datetime import datetime, timezone
        from app.game_inventory_cards import format_restriction_countdown

        base = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "1001", "2001", "0", 1,
            "Kilowatt Case", "Container", "Kilowatt Case", False, True,
            restriction_type=RestrictionType.MARKET_PURCHASE_COOLDOWN,
            restriction_status=RestrictionStatus.RESTRICTED,
        )
        self.assertEqual(format_restriction_countdown(None), "Срок неизвестен")
        self.assertEqual(format_restriction_countdown(datetime(2026, 11, 10)), "Срок неизвестен")

        deadline = datetime(2026, 11, 10, tzinfo=timezone.utc)
        self.assertEqual(
            format_restriction_countdown(deadline, now=datetime(2026, 11, 8, tzinfo=timezone.utc)),
            "Обмен через 2 д.",
        )
        self.assertEqual(
            format_restriction_countdown(deadline, now=datetime(2026, 11, 10, tzinfo=timezone.utc)),
            "Срок истёк · обновите снимок ASF",
        )
        expired = replace(base, tradable_after=deadline)
        card = CS2InventoryGroupCard(build_cs2_inventory_projection([expired]).groups[0])
        self.assertIs(card.group.instances[0].is_tradable, False)
        self.assertNotIn("Срок истёк", card.toolTip())
        self.assertNotIn("обновите снимок ASF", card.toolTip())

    def test_price_summary_shows_selected_and_all_bot_totals_prominently(self):
        from decimal import Decimal
        from types import SimpleNamespace

        panel = self.bare_panel()
        panel._selected_bot_names = Mock(return_value=["Bot A", "Bot B"])
        panel.bot_selection_model = SimpleNamespace(
            account_names=["Bot A", "Bot B", "Bot C"]
        )
        panel.cs2_all_instances = (
            CS2InventoryInstance(
                "cs2", 730, 2, "Bot A", "a1", "class", "0", 1,
                "AK-47", "Rifle", "AK-47", True, True,
            ),
            CS2InventoryInstance(
                "cs2", 730, 2, "Bot B", "b1", "class", "0", 1,
                "M4A1-S", "Rifle", "M4A1-S", True, True,
            ),
            CS2InventoryInstance(
                "cs2", 730, 2, "Bot C", "c1", "class", "0", 1,
                "AWP", "Rifle", "AWP", True, True,
            ),
        )
        panel.cs2_snapshot_meta = {"Bot A": 1, "Bot B": 1, "Bot C": 1}
        panel.price_quotes = {}
        panel.usd_rub = None
        panel.cs2_valuation = SimpleNamespace(
            selected_total_rub=Decimal("152.18"), is_partial=True,
            unknown_price_instances=1, awaiting_rate_instances=0,
            below_threshold_instances=0,
        )
        panel.cs2_all_valuation = SimpleNamespace(
            selected_total_rub=Decimal("300.00"), is_partial=False,
            unknown_price_instances=0, awaiting_rate_instances=0,
            below_threshold_instances=0,
        )

        panel._update_price_summary()

        summary = panel.price_summary.text()
        self.assertIn("ВЫБРАНО: 2 из 3 ботов · 2 предмета · ₽152.18 · частичная оценка", summary)
        self.assertIn("ВСЕ БОТЫ: 3 бота · 3 предмета · ₽300.00", summary)
        self.assertIn("Снимки инвентаря: выбрано 2/2, всего 3/3", summary)
        self.assertNotIn("Bot A: ₽", summary)
        self.assertIn("font-size: 16px", panel.price_summary.styleSheet())


    def test_summary_does_not_show_zero_rubles_when_only_fx_conversion_is_pending(self):
        from types import SimpleNamespace

        panel = self.bare_panel()
        panel.cs2_all_instances = ()
        panel.cs2_snapshot_meta = {}
        panel.cs2_valuation = SimpleNamespace(
            selected_total_rub=0, is_partial=True,
            unknown_price_instances=0, awaiting_rate_instances=1,
            priced_instances=0, below_threshold_instances=0,
        )
        panel.cs2_all_valuation = panel.cs2_valuation

        self.assertEqual(
            panel._valuation_total_text(panel.cs2_valuation),
            "ожидает курс USD/RUB",
        )

    def test_existing_panel_render_uses_group_cards_for_cs2_projection(self):
        from games.cs2.inventory_view import build_cs2_inventory_projection

        panel = self.bare_panel()
        panel.game_combo.currentData.return_value = "cs2"
        panel.search = QLineEdit()
        panel.transfer_filter = QComboBox()
        panel.transfer_filter.addItem("Любая", "all")
        panel.rarity_filter = QComboBox()
        panel.rarity_filter.addItem("Любая", "")
        panel.quality_filter = QComboBox()
        panel.quality_filter.addItem("Любое", "")
        panel.item_filter = QComboBox()
        panel.item_filter.addItem("Все предметы", "all")
        panel.cards_host = QWidget(panel)
        panel.cards_layout = QGridLayout(panel.cards_host)
        panel.cards_by_image = {}
        panel.image_loader = Mock(cache={})
        panel.width = Mock(return_value=620)
        panel.grouping_mode = QComboBox()
        panel.grouping_mode.addItem("Общий вид", "shared")
        panel.grouping_mode.addItem("Раздельно по ботам", "separate")
        panel.view_mode = QComboBox()
        panel.view_mode.addItem("Карточки предметов", "cards")
        panel.view_mode.addItem("Таблица подробностей", "table")
        panel.table = QTableWidget()
        panel.scroll = QScrollArea()
        instances = [
            CS2InventoryInstance(
                "cs2", 730, 2, bot, asset, "2001", "0", 1,
                "AK-47 | Redline", "Classified Rifle", "AK-47 | Redline", True, True,
            )
            for bot, asset in (("Bot A", "1001"), ("Bot B", "1002"))
        ]
        panel.cs2_projection = build_cs2_inventory_projection(instances)
        panel.items = [
            InventoryItem("cs2", item.bot_name, item.asset_id, "2001", "0", item.name,
                          item_type=item.item_type, market_hash_name=item.market_hash_name,
                          is_tradable=True, is_marketable=True, rarity="Classified")
            for item in instances
        ]

        panel.render()

        cards = [panel.cards_layout.itemAt(i).widget()
                 for i in range(panel.cards_layout.count())]
        self.assertEqual(len(cards), 1)
        self.assertIsInstance(cards[0], CS2InventoryGroupCard)
        self.assertIn("border: 2px solid #d32ce6", cards[0].styleSheet())
        self.assertEqual(panel.status.text(), "")

        panel.grouping_mode.setCurrentIndex(1)
        panel.render()
        self.assertEqual(panel.cards_layout.count(), 2)
        owner_details = " ".join(
            panel.cards_layout.itemAt(index).widget().toolTip()
            for index in range(panel.cards_layout.count())
        )
        self.assertNotIn("Bot A", owner_details)
        self.assertNotIn("Bot B", owner_details)

    def test_transfer_filter_selects_instances_inside_a_mixed_variant_group(self):
        panel = self.bare_panel()
        panel.search = QLineEdit()
        panel.transfer_filter = QComboBox()
        panel.transfer_filter.addItem("Любая", "all")
        panel.transfer_filter.addItem("Передаваемые", "yes")
        panel.transfer_filter.addItem("Непередаваемые", "no")
        panel.transfer_filter.addItem("Неизвестно", "unknown")
        panel.rarity_filter = QComboBox()
        panel.rarity_filter.addItem("Любая редкость", "")
        panel.quality_filter = QComboBox()
        panel.quality_filter.addItem("Любое качество", "")
        panel.item_filter = QComboBox()
        panel.item_filter.addItem("Все предметы", "all")
        panel.cards_host = QWidget(panel)
        panel.cards_layout = QGridLayout(panel.cards_host)
        panel.cards_by_image = {}
        panel.image_loader = Mock(cache={})
        panel.width = Mock(return_value=620)
        panel.grouping_mode = QComboBox()
        panel.grouping_mode.addItem("Общий вид", "shared")
        panel.view_mode = QComboBox()
        panel.view_mode.addItem("Карточки", "cards")
        panel.view_mode.addItem("Таблица", "table")
        panel.table = QTableWidget()
        panel.scroll = QScrollArea()
        base = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "1001", "2001", "0", 1,
            "Kilowatt Case", "Container", "Kilowatt Case", True, True,
        )
        restricted = replace(base, bot_name="Bot B", asset_id="1002", is_tradable=False)
        unknown = replace(base, bot_name="Bot C", asset_id="1003", is_tradable=None,
                          is_marketable=None)
        panel.cs2_projection = build_cs2_inventory_projection([base, restricted, unknown])
        panel.items = [
            InventoryItem("cs2", item.bot_name, item.asset_id, "2001", "0", item.name,
                          item_type="Container", market_hash_name=item.market_hash_name,
                          is_tradable=item.is_tradable, is_marketable=item.is_marketable)
            for item in (base, restricted, unknown)
        ]

        panel.render()
        self.assertEqual(panel.cards_layout.count(), 1)
        all_card = panel.cards_layout.itemAt(0).widget()
        self.assertNotIn("Статус неизвестен:", " ".join(l.text() for l in all_card.findChildren(QLabel)))

        panel.view_mode.setCurrentIndex(1)
        panel.render()
        self.assertEqual(panel.table.item(0, 5).text(), "3")
        self.assertEqual(panel.table.item(0, 6).text(), "1")
        self.assertEqual(panel.table.item(0, 8).text(), "1")
        self.assertEqual(panel.table.item(0, 9).text(), "1")
        self.assertIn("Неизвестно: 1", panel.table.item(0, 3).text())
        panel.view_mode.setCurrentIndex(0)
        panel.render()

        panel.transfer_filter.setCurrentIndex(2)
        panel.render()
        self.assertEqual(panel.cards_layout.count(), 1)
        restricted_card = panel.cards_layout.itemAt(0).widget()
        self.assertEqual(restricted_card.group.instance_count, 1)
        self.assertEqual(restricted_card.group.instances[0].asset_id, "1002")
        self.assertNotIn("Непередаваемые:", " ".join(l.text() for l in restricted_card.findChildren(QLabel)))

        panel.transfer_filter.setCurrentIndex(3)
        panel.render()
        unknown_card = panel.cards_layout.itemAt(0).widget()
        self.assertEqual(unknown_card.group.instances[0].asset_id, "1003")

        panel.transfer_filter.setCurrentIndex(1)
        panel.render()
        available_card = panel.cards_layout.itemAt(0).widget()
        self.assertEqual(available_card.group.instances[0].asset_id, "1001")

    def test_game_items_support_the_same_grouping_and_view_switches(self):
        store = Mock()
        store.load_all.return_value = []
        with patch("app.game_inventory_panel.GameInventoryStore", return_value=store), \
                patch("app.game_inventory_panel.configured_bot_names", return_value=[]):
            panel = GameInventoryPanel()

        self.assertEqual(panel.grouping_mode.currentData(), "shared")
        self.assertEqual(panel.view_mode.currentData(), "cards")
        self.assertIsNotNone(panel.table)

    def test_other_game_shared_and_separate_cards_preserve_physical_assets(self):
        panel = self.bare_panel(game="dota2")
        panel.grouping_mode = QComboBox()
        panel.grouping_mode.addItem("Общий вид", "shared")
        panel.view_mode = QComboBox()
        panel.view_mode.addItem("Карточки предметов", "cards")
        panel.table = QTableWidget()
        panel.scroll = QScrollArea()
        panel.search = QLineEdit()
        panel.transfer_filter = QComboBox()
        panel.transfer_filter.addItem("Любая", "all")
        panel.rarity_filter = QComboBox()
        panel.rarity_filter.addItem("Любая", "")
        panel.quality_filter = QComboBox()
        panel.quality_filter.addItem("Любое", "")
        panel.cards_host = QWidget(panel)
        panel.cards_layout = QGridLayout(panel.cards_host)
        panel.cards_by_image = {}
        panel.image_loader = Mock(cache={})
        panel.width = Mock(return_value=620)
        panel.items = [
            InventoryItem("dota2", "Bot A", "asset-a", "class", "0", "Item",
                          item_type="Treasure", is_tradable=True, is_marketable=False),
            InventoryItem("dota2", "Bot B", "asset-b", "class", "0", "Item",
                          item_type="Treasure", is_tradable=True, is_marketable=False),
        ]

        panel.render()
        self.assertEqual(panel.cards_layout.count(), 1)
        self.assertIn("Bot A", panel.cards_layout.itemAt(0).widget().toolTip())
        self.assertIn("asset-b", panel.cards_layout.itemAt(0).widget().toolTip())

        panel.grouping_mode.addItem("Раздельно по ботам", "separate")
        panel.grouping_mode.setCurrentIndex(1)
        panel.render()
        self.assertEqual(panel.cards_layout.count(), 2)

    def test_game_item_table_uses_thumbnail_and_colored_physical_metrics(self):
        panel = self.bare_panel(game="dota2")
        panel.grouping_mode = QComboBox()
        panel.grouping_mode.addItem("Общий вид", "shared")
        panel.view_mode = QComboBox()
        panel.view_mode.addItem("Таблица подробностей", "table")
        panel.table = QTableWidget()
        panel.scroll = QScrollArea()
        panel.search = QLineEdit()
        panel.transfer_filter = QComboBox()
        panel.transfer_filter.addItem("Любая", "all")
        panel.rarity_filter = QComboBox()
        panel.rarity_filter.addItem("Любая", "")
        panel.quality_filter = QComboBox()
        panel.quality_filter.addItem("Любое", "")
        panel.cards_host = QWidget(panel)
        panel.cards_layout = QGridLayout(panel.cards_host)
        panel.cards_by_image = {}
        url = "https://community.cloudflare.steamstatic.com/economy/image/test"
        icon = QPixmap(24, 24)
        panel.image_loader = Mock(cache={url: icon})
        panel.items = [InventoryItem(
            "dota2", "Bot A", "asset-a", "class", "0", "Item",
            item_type="Treasure", icon_url=url, is_tradable=True,
        )]

        panel.render()

        name_column = panel.table.findItems("Item", Qt.MatchFlag.MatchExactly)[0]
        self.assertFalse(name_column.icon().isNull())
        headers = [panel.table.horizontalHeaderItem(i).text() for i in range(panel.table.columnCount())]
        self.assertIn("Передаваемые", headers)
        self.assertIn("Непередаваемые", headers)
        self.assertIn("Статус неизвестен", headers)
        self.assertIn("Цена", headers)

    def test_cs2_table_keeps_stack_amount_separate_from_physical_instances(self):
        from games.cs2.inventory_view import build_cs2_inventory_projection

        panel = self.bare_panel()
        panel.grouping_mode = QComboBox()
        panel.grouping_mode.addItem("Общий вид", "shared")
        panel.view_mode = QComboBox()
        panel.view_mode.addItem("Таблица подробностей", "table")
        panel.table = QTableWidget()
        panel.scroll = QScrollArea()
        panel.search = QLineEdit()
        panel.transfer_filter = QComboBox()
        panel.transfer_filter.addItem("Любая", "all")
        panel.rarity_filter = QComboBox()
        panel.rarity_filter.addItem("Любая", "")
        panel.quality_filter = QComboBox()
        panel.quality_filter.addItem("Любое", "")
        panel.item_filter = QComboBox()
        panel.item_filter.addItem("Все предметы", "all")
        panel.cards_host = QWidget(panel)
        panel.cards_layout = QGridLayout(panel.cards_host)
        panel.cards_by_image = {}
        panel.image_loader = Mock(cache={})
        panel.width = Mock(return_value=620)
        first = CS2InventoryInstance(
            "cs2", 730, 2, "Bot A", "1001", "2001", "0", 2,
            "AK-47 | Redline", "Rifle", "AK-47 | Redline", True, True,
        )
        second = CS2InventoryInstance(
            "cs2", 730, 2, "Bot B", "1002", "2001", "0", None,
            "AK-47 | Redline", "Rifle", "AK-47 | Redline", False, None,
            restriction_type=RestrictionType.TRADE_PROTECTION,
            restriction_status=RestrictionStatus.RESTRICTED,
        )
        panel.cs2_projection = build_cs2_inventory_projection([first, second])
        panel.items = [
            InventoryItem("cs2", item.bot_name, item.asset_id, "2001", "0", item.name,
                          item_type=item.item_type, market_hash_name=item.market_hash_name,
                          is_tradable=True, is_marketable=True)
            for item in (first, second)
        ]

        panel.render()

        self.assertEqual(panel.table.item(0, 4).text(), "Неизвестно (известно: 2)")
        self.assertEqual(panel.table.item(0, 5).text(), "2")
        self.assertEqual(panel.table.item(0, 6).text(), "1")
        self.assertEqual(panel.table.item(0, 7).text(), "2")
        self.assertEqual(panel.table.item(0, 9).text(), "0")
        self.assertEqual(panel.table.item(0, 3).text(), "Да: 1 · Неизвестно: 1")
        self.assertIn("Защита обмена", panel.table.item(0, 10).text())
        self.assertIn("Bot B · asset 1002", panel.table.item(0, 10).toolTip())

    def test_saved_projection_keeps_selected_bot_and_unknown_amount(self):
        panel = self.bare_panel()
        panel.store.load_all.return_value = [
            InventoryItem("cs2", "Bot A", "1001", "2001", "0", "AK-47"),
            InventoryItem("cs2", "Bot B", "1002", "2001", "0", "AK-47"),
        ]
        panel.load_saved = GameInventoryPanel.load_saved.__get__(panel)
        panel._refresh_filter_values = Mock()
        panel.render = Mock()

        panel.load_saved()

        self.assertEqual([item.bot_name for item in panel.cs2_projection.instances], ["Bot A"])
        self.assertIsNone(panel.cs2_projection.total_amount)
        self.assertEqual(panel.cs2_projection.unknown_amount_instances, 1)

    def test_default_client_keeps_standard_endpoint_and_existing_password_field(self):
        panel = self.bare_panel()
        panel.password.setText("in-memory-default")
        with patch.dict(os.environ, {"GAMEDELIVERY_TEST_ASF_IPC_URL": ""}, clear=False), \
                patch("app.game_inventory_panel.ASFClient") as client_factory:
            panel._create_asf_client()

        client_factory.assert_called_once_with(
            ipc_password="in-memory-default", timeout=45.0
        )

    def test_test_endpoint_uses_hidden_prompt_and_explicit_loopback_opt_in(self):
        panel = self.bare_panel()
        with patch.dict(os.environ, {"GAMEDELIVERY_TEST_ASF_IPC_URL": "http://127.0.0.1:1243"}), \
                patch("app.game_inventory_panel.QInputDialog.getText", return_value=("test-only-password", True)) as prompt, \
                patch("app.game_inventory_panel.ASFClient") as client_factory:
            panel._create_asf_client()

        self.assertEqual(prompt.call_args.args[3], QLineEdit.EchoMode.Password)
        client_factory.assert_called_once_with(
            base_url="http://127.0.0.1:1243", ipc_password="test-only-password",
            timeout=45.0, allow_alternate_loopback_port=True,
        )
        self.assertEqual(panel.password.text(), "")

    def test_test_endpoint_cancel_does_not_construct_client_or_request(self):
        panel = self.bare_panel()
        with patch.dict(os.environ, {"GAMEDELIVERY_TEST_ASF_IPC_URL": "http://127.0.0.1:1243"}), \
                patch("app.game_inventory_panel.QInputDialog.getText", return_value=("", False)), \
                patch("app.game_inventory_panel.ASFClient") as client_factory:
            panel.fetch_inventory()

        client_factory.assert_not_called()
        panel.store.replace.assert_not_called()
        self.assertIn("отмен", panel.status.text().casefold())

    def test_test_endpoint_never_accepts_default_production_port(self):
        panel = self.bare_panel()
        with patch.dict(os.environ, {"GAMEDELIVERY_TEST_ASF_IPC_URL": "http://127.0.0.1:1242"}), \
                patch("app.game_inventory_panel.QInputDialog.getText") as prompt, \
                patch("app.game_inventory_panel.ASFClient") as client_factory:
            panel.fetch_inventory()

        prompt.assert_not_called()
        client_factory.assert_not_called()
        self.assertIn("1242", panel.status.text())

    def test_test_password_is_not_left_in_ui_persisted_or_shown_on_error(self):
        panel = self.bare_panel()
        client = Mock()
        secret = "test-only-password"
        client.get_bot_inventory.side_effect = ValueError(secret)
        with patch.dict(os.environ, {"GAMEDELIVERY_TEST_ASF_IPC_URL": "http://127.0.0.1:1243"}), \
                patch("app.game_inventory_panel.QInputDialog.getText", return_value=(secret, True)), \
                patch("app.game_inventory_panel.ASFClient", return_value=client):
            panel.fetch_inventory()

        self.assertEqual(panel.password.text(), "")
        self.assertNotIn(secret, panel.status.text())
        self.assertNotIn(secret, repr(panel.store.mock_calls))
        panel.store.replace.assert_not_called()

    def test_test_mode_shows_safe_http_status_without_exposing_response_body(self):
        panel = self.bare_panel()
        client = Mock()
        client.get_bot_inventory.side_effect = ASFError("ASF IPC вернул HTTP 400")
        with patch.dict(os.environ, {"GAMEDELIVERY_TEST_ASF_IPC_URL": "http://127.0.0.1:1243"}), \
                patch("app.game_inventory_panel.QInputDialog.getText",
                      return_value=("test-only-password", True)), \
                patch("app.game_inventory_panel.ASFClient", return_value=client):
            panel.fetch_inventory()

        self.assertIn("HTTP 400", panel.status.text())
        self.assertNotIn("test-only-password", panel.status.text())

    def test_test_mode_constructor_does_not_load_saved_password(self):
        store = Mock()
        store.load_all.return_value = []
        with patch.dict(os.environ, {"GAMEDELIVERY_TEST_ASF_IPC_URL": "http://127.0.0.1:1243"}), \
                patch("app.game_inventory_panel.InventoryPanel._load_saved_ipc_password",
                      side_effect=AssertionError("test mode must not read saved IPCPassword")), \
                patch("app.game_inventory_panel.GameInventoryStore", return_value=store), \
                patch("app.game_inventory_panel.configured_bot_names",
                      return_value=["ProductionBot"]) as configured:
            panel = GameInventoryPanel()

        configured.assert_not_called()
        self.assertEqual(panel.password.text(), "")
        self.assertEqual(panel.bot_list.count(), 0)

    def test_test_mode_suppresses_saved_password_loaders_across_main_window_panels(self):
        with patch.dict(os.environ, {"GAMEDELIVERY_TEST_ASF_IPC_URL": "http://127.0.0.1:1243"}):
            keyring = Mock()
            with patch.dict(sys.modules, {"keyring": keyring}), \
                    patch("app.inventory_panel.QSettings",
                          side_effect=AssertionError("must not read existing settings")):
                self.assertEqual(InventoryPanel._load_saved_ipc_password(), "")
            keyring.get_keyring.assert_not_called()

            widget = ASFManagementWidget.__new__(ASFManagementWidget)
            QWidget.__init__(widget)
            widget.ipc_password = QLineEdit()
            widget.remember_ipc_password = QCheckBox()
            widget._keyring_backend = Mock(
                side_effect=AssertionError("must not read existing keyring credential")
            )
            widget._load_saved_ipc_password()
            widget._keyring_backend.assert_not_called()
            self.assertEqual(widget.ipc_password.text(), "")

    def test_test_mode_does_not_autostart_or_enable_asf_management(self):
        with patch.dict(os.environ, {"GAMEDELIVERY_TEST_ASF_IPC_URL": "http://127.0.0.1:1243"}), \
                patch("app.asf_management.discover_asf_roots", return_value=[]), \
                patch("app.asf_management.QTimer.singleShot") as timer:
            widget = ASFManagementWidget()

        timer.assert_not_called()
        self.assertFalse(widget.isEnabled())
        self.assertIn("автозапуск ASF отключены", widget.status.text())

    def test_test_mode_bot_selection_uses_shared_test_asf_bot_list(self):
        from app.bot_selector import BotSelectionModel

        panel = GameInventoryPanel.__new__(GameInventoryPanel)
        panel.test_mode = True
        panel.bot_selection_model = BotSelectionModel()
        panel.bot_selection_model.set_accounts(["IsolatedTestBot", "SecondTestBot"])
        panel.bot_selection_model.set_selected(["SecondTestBot"])

        self.assertEqual(panel._selected_bot_names(), ["SecondTestBot"])


if __name__ == "__main__":
    unittest.main()
