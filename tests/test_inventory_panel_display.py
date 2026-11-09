import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QLabel, QLineEdit, QListWidget, QPushButton,
    QTableWidget, QWidget,
)

from app.inventory_display import InventoryCardDelegate
from app.inventory_panel import InventoryPanel
from app.inventory_display import METRIC_COLORS
from games.steam_cards.selection import CardCopy
from games.inventory_display import InventoryMetrics


class InventoryPanelDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def panel(self):
        panel = InventoryPanel.__new__(InventoryPanel)
        QWidget.__init__(panel)
        panel.inventories = {
            "Bot A": [CardCopy(
                "asset-1", "Game", "Trading Card", is_tradable=True,
                icon_url="https://community.cloudflare.steamstatic.com/economy/image/card",
            ), CardCopy(
                "asset-2", "Game", "Trading Card", is_tradable=True,
                icon_url="https://community.cloudflare.steamstatic.com/economy/image/card",
            )],
            "Bot B": [CardCopy(
                "asset-3", "Game", "Trading Card", is_tradable=True,
                icon_url="https://community.cloudflare.steamstatic.com/economy/image/card",
            )],
        }
        panel._checked_bot_names = Mock(return_value=["Bot A", "Bot B"])
        panel.mode = QComboBox()
        panel.mode.addItem("Общий вид", "shared")
        panel.mode.addItem("Раздельно по ботам", "separate")
        panel.search_input = QLineEdit()
        panel.item_filter = QComboBox()
        panel.item_filter.addItem("Все предметы", "all")
        panel.view_mode = QComboBox()
        panel.view_mode.addItem("Карточки предметов", "cards")
        panel.view_mode.addItem("Таблица подробностей", "table")
        panel.table = QTableWidget()
        panel.item_grid = QListWidget()
        panel.summary = QLabel()
        panel.statuses = {}
        panel.reservation_rows = {}
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.red)
        panel._image_cache = {
            "https://community.cloudflare.steamstatic.com/economy/image/card": QIcon(pixmap),
        }
        panel._request_item_image = Mock()
        panel._selected_asset_keys = {("Bot A", "asset-1")}
        panel._render_instance_choices = Mock()
        panel.item_grid.setItemDelegate(InventoryCardDelegate(panel.item_grid))
        return panel

    def test_steam_card_table_has_real_thumbnail_and_separate_metrics(self):
        panel = self.panel()

        panel.render_inventory()

        name = panel.table.findItems("Trading Card", Qt.MatchFlag.MatchExactly)[0]
        self.assertFalse(name.icon().isNull())
        headers = [panel.table.horizontalHeaderItem(i).text() for i in range(panel.table.columnCount())]
        self.assertIn("Передаваемые", headers)
        self.assertIn("Непередаваемые", headers)
        self.assertIn("Аккаунтов", headers)
        self.assertIn("Статус неизвестен", headers)
        self.assertIn("Цена", headers)
        self.assertEqual(panel.table.item(0, 3).text(), "3")
        self.assertEqual(panel.table.item(0, 5).text(), "2")
        self.assertEqual(panel.table.item(0, 6).text(), "3")
        self.assertEqual(panel.table.item(0, 3).foreground().color().name(), METRIC_COLORS["total"])
        self.assertEqual(panel.table.item(0, 6).foreground().color().name(), METRIC_COLORS["tradable"])

    def test_test_mode_inventory_panel_skips_production_bot_configs_and_ipc(self):
        from app.bot_selector import BotSelectionModel

        panel = InventoryPanel.__new__(InventoryPanel)
        QWidget.__init__(panel)
        panel._test_mode = True
        panel.bot_selection_model = BotSelectionModel()
        panel.bot_selection_model.set_accounts(["IsolatedTestBot"])
        panel.snapshots = Mock()
        panel.snapshots.load_all.return_value = {}
        panel.database_path = "unused-in-test"
        panel.bot_list = QListWidget()
        panel.bot_name_input = QComboBox()
        panel.bot_name_input.addItem("ProductionBot", "ProductionBot")
        panel.ipc_password_input = QLineEdit()
        panel.summary = QLabel()
        panel._load_reservations = Mock(return_value={})
        panel.render_inventory = Mock()
        panel._checked_bot_names = Mock(return_value=["IsolatedTestBot"])
        panel._batch_thread = None
        panel.asf_controller = Mock()
        panel.fetch_selected_button = QPushButton()
        panel.fetch_button = QPushButton()
        panel.select_all_bots_button = QPushButton()
        panel.clear_bots_button = QPushButton()
        panel.refresh_button = QPushButton()
        panel.table = QTableWidget()
        panel.item_grid = QListWidget()
        panel.instance_list = QListWidget()

        with patch.dict(os.environ, {"GAMEDELIVERY_TEST_ASF_IPC_URL": "http://127.0.0.1:1243"}), \
                patch("app.inventory_panel.configured_bot_names",
                      return_value=["ProductionBot"]) as configured, \
                patch("app.inventory_panel._InventoryBatchWorker") as worker:
            panel.refresh()
            panel.fetch_from_asf()
            panel.fetch_selected_from_asf()

        configured.assert_not_called()
        worker.assert_not_called()
        panel.snapshots.load_all.assert_not_called()
        panel._load_reservations.assert_not_called()
        panel.asf_controller.refresh.assert_not_called()
        self.assertEqual(panel.bot_list.count(), 1)
        self.assertIn("IsolatedTestBot", panel.bot_list.item(0).text())
        self.assertNotIn("ProductionBot", panel.bot_list.item(0).text())

    def test_steam_card_tiles_use_compact_inventory_delegate(self):
        panel = self.panel()

        panel.render_inventory()

        self.assertIsInstance(panel.item_grid.itemDelegate(), InventoryCardDelegate)
        metrics = panel.item_grid.item(0).data(Qt.ItemDataRole.UserRole + 3)
        self.assertEqual(metrics, InventoryMetrics(3, 0, 2, 3, 0))
        self.assertTrue(panel.item_grid.item(0).isSelected())

    def test_grouping_switch_keeps_exact_selected_asset_and_owner(self):
        panel = self.panel()

        panel.render_inventory()
        panel.mode.setCurrentIndex(1)
        panel.render_inventory()

        self.assertEqual(panel._selected_asset_keys, {("Bot A", "asset-1")})
        self.assertEqual(panel.item_grid.count(), 2)
        selected = [panel.item_grid.item(i) for i in range(panel.item_grid.count())
                    if panel.item_grid.item(i).isSelected()]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].data(Qt.ItemDataRole.UserRole), (("Bot A", ("asset-1", "asset-2")),))

    def test_steam_card_delegate_renders_without_losing_image_tile(self):
        panel = self.panel()
        panel.render_inventory()
        panel.item_grid.resize(400, 240)
        panel.item_grid.show()
        self.app.processEvents()

        rendered = panel.item_grid.viewport().grab()

        self.assertFalse(rendered.isNull())
        self.assertFalse(panel.item_grid.item(0).icon().isNull())


if __name__ == "__main__":
    unittest.main()
