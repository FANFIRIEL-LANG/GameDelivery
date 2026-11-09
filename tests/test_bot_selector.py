import os
import unittest
from unittest.mock import Mock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QListWidget, QPushButton, QWidget

from app.bot_selector import (
    BotSelectionModel, checked_bot_names, connect_account_search, populate_bot_list,
)
from app.game_inventory_panel import GameInventoryPanel
from app.inventory_panel import InventoryPanel


class BotSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_shared_model_selects_all_then_keeps_selection_case_insensitively(self):
        model = BotSelectionModel()
        model.set_accounts(["Ewaylariell", "Bot B", "ewaylariell"])

        self.assertEqual(model.account_names, ["Bot B", "Ewaylariell"])
        self.assertEqual(model.selected_names, ["Bot B", "Ewaylariell"])

        model.set_selected(["ewaylariell"])
        model.set_accounts(["Bot B", "EWAYLARIELL", "Bot C"])

        self.assertEqual(model.selected_names, ["EWAYLARIELL"])

    def test_clear_selection_stays_cleared_when_accounts_refresh(self):
        model = BotSelectionModel()
        model.set_accounts(["Bot A", "Bot B"])
        model.set_selected([])
        model.set_accounts(["Bot A", "Bot B", "Bot C"])

        self.assertEqual(model.selected_names, [])

    def test_checklist_supports_multi_select_and_case_insensitive_search(self):
        accounts = QListWidget()
        search = QLineEdit()
        connect_account_search(search, accounts)
        populate_bot_list(accounts, ["Ewaylariell", "OtherBot"], ["Ewaylariell"])
        second = accounts.item(1)
        second.setCheckState(Qt.CheckState.Checked)

        self.assertEqual(checked_bot_names(accounts), ["Ewaylariell", "OtherBot"])

        search.setText("EWAY")

        self.assertFalse(accounts.item(0).isHidden())
        self.assertTrue(accounts.item(1).isHidden())

        search.setText("ewaylariell")
        self.assertFalse(accounts.item(0).isHidden())

    def test_inventory_tabs_share_visible_multi_select_and_refresh_controls_in_test_mode(self):
        model = BotSelectionModel()
        game_panel = GameInventoryPanel.__new__(GameInventoryPanel)
        QWidget.__init__(game_panel)
        game_panel.bot_selection_model = model
        game_panel.test_mode = True
        game_panel.bot_list = QListWidget()
        game_panel.bot_list.show()
        game_panel.account_search = QLineEdit()
        game_panel.game_combo = Mock()
        game_panel.load_saved = Mock()
        game_panel.refresh_bot_list_button = QPushButton()
        connect_account_search(game_panel.account_search, game_panel.bot_list)
        game_panel.bot_list.itemChanged.connect(game_panel._bot_list_changed)
        model.accounts_changed.connect(game_panel._render_bot_selection)
        model.selection_changed.connect(game_panel._bot_selection_updated)
        game_panel.refresh_bot_list_button.clicked.connect(model.refresh_requested.emit)

        inventory_panel = InventoryPanel.__new__(InventoryPanel)
        QWidget.__init__(inventory_panel)
        inventory_panel.bot_selection_model = model
        inventory_panel._test_mode = True
        inventory_panel.bot_list = QListWidget()
        inventory_panel.bot_list.show()
        inventory_panel.account_search = QLineEdit()
        inventory_panel.statuses = {}
        inventory_panel.table = object()
        inventory_panel.render_inventory = Mock()
        inventory_panel.refresh_bots_button = QPushButton()
        connect_account_search(inventory_panel.account_search, inventory_panel.bot_list)
        inventory_panel.bot_list.itemChanged.connect(inventory_panel._bot_list_changed)
        model.accounts_changed.connect(inventory_panel._render_bot_selection)
        model.selection_changed.connect(inventory_panel._bot_selection_updated)
        inventory_panel.refresh_bots_button.clicked.connect(model.refresh_requested.emit)

        model.set_accounts(["Ewaylariell", "OtherBot"])

        self.assertFalse(game_panel.bot_list.isHidden())
        self.assertFalse(inventory_panel.bot_list.isHidden())
        self.assertEqual(game_panel._selected_bot_names(), ["Ewaylariell", "OtherBot"])
        self.assertEqual(inventory_panel._checked_bot_names(), ["Ewaylariell", "OtherBot"])

        game_panel.bot_list.item(0).setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(model.selected_names, ["OtherBot"])
        self.assertEqual(inventory_panel._checked_bot_names(), ["OtherBot"])

        refreshed = Mock()
        model.refresh_requested.connect(refreshed)
        inventory_panel.refresh_bots_button.click()
        game_panel.refresh_bot_list_button.click()
        refreshed.assert_has_calls([call(), call()])

        inventory_panel.select_all_bots()
        self.assertEqual(model.selected_names, ["Ewaylariell", "OtherBot"])
        game_panel.clear_bot_selection()
        self.assertEqual(model.selected_names, [])

    def test_test_asf_bot_list_worker_reads_only_read_only_bot_endpoint(self):
        from app.game_inventory_panel import _BotListRefreshTask

        client = Mock()
        client.get_bots.return_value = {"bot z": {}, "Bot A": {}}
        results = []
        task = _BotListRefreshTask(client)
        task.signals.finished.connect(lambda names, error: results.append((names, error)))

        task.run()

        client.get_bots.assert_called_once_with()
        self.assertEqual(results, [(["Bot A", "bot z"], "")])
