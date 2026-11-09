import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtCore import Qt

from games.inventory import InventoryItem
from games.inventory_display import (
    PRICE_UNAVAILABLE,
    InventoryMetrics,
    build_inventory_groups,
    count_physical_metrics,
)
from app.inventory_display import InventoryMetricsLabel
from app.inventory_display import SteamInventoryImageLoader, thumbnail_table_item, update_table_thumbnails
from app.game_inventory_cards import GameItemImageLoader
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QTableWidget


class InventoryDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_physical_metrics_count_each_status_and_distinct_owner(self):
        metrics = count_physical_metrics([
            ("Bot A", True),
            ("Bot A", False),
            ("Bot B", None),
        ])

        self.assertEqual(metrics, InventoryMetrics(
            total=3, non_tradable=1, owner_accounts=2, tradable=1,
            unknown_status=1,
        ))

    def test_unknown_status_is_not_counted_as_tradable_or_non_tradable(self):
        metrics = count_physical_metrics([("Bot A", None)])

        self.assertEqual(metrics.total, 1)
        self.assertEqual(metrics.tradable, 0)
        self.assertEqual(metrics.non_tradable, 0)
        self.assertEqual(metrics.unknown_status, 1)

    def test_shared_and_separate_game_item_groups_preserve_assets_and_statuses(self):
        source = [
            InventoryItem("cs2", "Bot A", "asset-1", "class", "0", "AK-47",
                          item_type="Rifle", is_tradable=True, is_marketable=False),
            InventoryItem("cs2", "Bot B", "asset-2", "class", "0", "AK-47",
                          item_type="Rifle", is_tradable=True, is_marketable=False),
            InventoryItem("cs2", "Bot B", "asset-3", "class", "0", "AK-47",
                          item_type="Rifle", is_tradable=False, is_marketable=False),
        ]

        shared = build_inventory_groups(source, mode="shared")
        separate = build_inventory_groups(source, mode="separate")

        self.assertEqual([len(group.items) for group in shared], [1, 2])
        shared_assets = {item.asset_id for group in shared for item in group.items}
        self.assertEqual(shared_assets, {"asset-1", "asset-2", "asset-3"})
        self.assertEqual(len(separate), 3)
        self.assertEqual({item.bot_name for group in separate for item in group.items}, {"Bot A", "Bot B"})

    def test_grouping_keeps_marketable_and_tradable_independent(self):
        source = [
            InventoryItem("cs2", "Bot A", "asset-1", "class", "0", "Item",
                          is_tradable=True, is_marketable=False),
            InventoryItem("cs2", "Bot B", "asset-2", "class", "0", "Item",
                          is_tradable=False, is_marketable=True),
        ]

        groups = build_inventory_groups(source)

        self.assertEqual(len(groups), 2)
        self.assertEqual({(g.items[0].is_tradable, g.items[0].is_marketable) for g in groups},
                         {(True, False), (False, True)})

    def test_missing_artwork_does_not_split_same_game_item_group(self):
        source = [
            InventoryItem("cs2", "Bot A", "asset-1", "class", "0", "Item"),
            InventoryItem("cs2", "Bot B", "asset-2", "class", "0", "Item",
                          icon_url="https://community.cloudflare.steamstatic.com/economy/image/icon"),
        ]

        groups = build_inventory_groups(source)

        self.assertEqual(len(groups), 1)
        self.assertEqual({item.asset_id for item in groups[0].items}, {"asset-1", "asset-2"})

    def test_metrics_label_uses_distinct_colors_and_unavailable_price(self):
        label = InventoryMetricsLabel()
        label.set_metrics(InventoryMetrics(4, 1, 2, 2, 1))

        rendered = label.text()
        self.assertIn("Всего: 4", rendered)
        self.assertIn("Непередаваемые: 1", rendered)
        self.assertIn("Аккаунтов: 2", rendered)
        self.assertIn("Передаваемые: 2", rendered)
        self.assertIn("Неизвестный статус: 1", rendered)
        self.assertIn("#f87171", rendered)
        self.assertIn("#4ade80", rendered)
        self.assertIn(PRICE_UNAVAILABLE, rendered)

    def test_both_panels_reuse_one_steam_image_loader(self):
        self.assertIs(GameItemImageLoader, SteamInventoryImageLoader)

    def test_table_thumbnail_refresh_uses_url_metadata_not_display_name(self):
        table = QTableWidget(2, 2)
        table.setItem(0, 1, thumbnail_table_item("url-a"))
        table.setItem(1, 1, thumbnail_table_item("url-b"))
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.green)
        icon = QIcon(pixmap)

        update_table_thumbnails(table, column=1, url="url-b", icon=icon)

        self.assertTrue(table.item(0, 1).icon().isNull())
        self.assertFalse(table.item(1, 1).icon().isNull())


if __name__ == "__main__":
    unittest.main()
