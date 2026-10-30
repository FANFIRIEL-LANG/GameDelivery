import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QListWidgetItem
from app.main import MainWindow
from games.steam_cards.selection import CardCopy
from games.steam_cards.snapshot import InventorySnapshotStore


class AppStartupSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_trade_url_validation_accepts_steam_url_and_rejects_other_hosts(self):
        valid = "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123"
        self.assertEqual(MainWindow._validate_trade_url(valid), valid)
        with self.assertRaises(ValueError):
            MainWindow._validate_trade_url("https://evil.example/tradeoffer/new/?partner=1&token=abcdef")

    def test_selected_snapshot_asset_is_reserved_and_order_persists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            InventorySnapshotStore(database).replace(
                [CardCopy("123", "Example Game", "Trading Card")],
                bot_name="Bot A",
            )
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database), \
                 patch("app.main.QMessageBox.information") as info, \
                 patch("app.main.QMessageBox.warning") as warning:
                window = MainWindow()
                try:
                    self.assertEqual(window.order_assets.count(), 1)
                    window.order_assets.item(0).setSelected(True)
                    window.order_recipient.setText(
                        "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123"
                    )
                    window._prepare_delivery_order()
                    warning.assert_not_called()
                    info.assert_called_once()
                    with sqlite3.connect(database) as db:
                        order = db.execute(
                            "SELECT asset_count, status FROM delivery_orders"
                        ).fetchone()
                        reservation = db.execute(
                            "SELECT bot_name, asset_id FROM steam_asset_reservations"
                        ).fetchone()
                    self.assertEqual(order[0], 1)
                    self.assertIn("не отправлен", order[1])
                    self.assertEqual(reservation, ("Bot A", "123"))
                    with sqlite3.connect(database) as db:
                        order_asset = db.execute(
                            "SELECT order_id, bot_name, asset_id FROM delivery_order_assets"
                        ).fetchone()
                    self.assertEqual(order_asset[1:], ("Bot A", "123"))
                    window._load_saved_orders()
                    self.assertEqual(window.orders.rowCount(), 1)
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_asf_trade_preview_uses_saved_order_assets_and_does_not_change_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            InventorySnapshotStore(database).replace(
                [CardCopy("234", "Example Game", "Trading Card")],
                bot_name="Bot A",
            )
            trade_url = (
                "https://steamcommunity.com/tradeoffer/new/"
                "?partner=123456&token=private-preview-token"
            )
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database), \
                 patch("app.main.QMessageBox.information") as info, \
                 patch("app.main.QMessageBox.warning") as warning, \
                 patch("app.main.ASFClient.preview_trade_offer") as preview:
                window = MainWindow()
                try:
                    window.order_recipient.setText(trade_url)
                    window.order_assets.item(0).setSelected(True)
                    window._prepare_delivery_order()
                    order_id = window.orders.item(0, 0).text()
                    window.orders.selectRow(0)
                    preview.return_value = {
                        "success": True,
                        "mode": "preview-only",
                        "sendEnabled": False,
                        "orderId": order_id,
                        "botName": "Bot A",
                        "targetSteamId64": 76561197960389184,
                        "assetCount": 1,
                        "assets": [{
                            "assetId": "234",
                            "name": "Trading Card",
                            "tradable": True,
                        }],
                    }

                    window._preview_selected_order_with_asf()

                    preview.assert_called_once_with(
                        order_id=order_id,
                        bot_name="Bot A",
                        trade_url=trade_url,
                        asset_ids=["234"],
                    )
                    warning.assert_not_called()
                    displayed = info.call_args.args[2]
                    self.assertIn("НЕ создавалось и НЕ отправлялось", displayed)
                    self.assertNotIn("private-preview-token", displayed)
                    with sqlite3.connect(database) as db:
                        status = db.execute(
                            "SELECT status FROM delivery_orders WHERE order_id = ?",
                            (order_id,),
                        ).fetchone()[0]
                        assets = db.execute(
                            "SELECT bot_name, asset_id FROM delivery_order_assets WHERE order_id = ?",
                            (order_id,),
                        ).fetchall()
                    self.assertEqual(status, "reserved — не отправлен")
                    self.assertEqual(assets, [("Bot A", "234")])
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_untradable_asset_cannot_be_reserved_even_if_ui_selection_is_forced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            InventorySnapshotStore(database).replace(
                [
                    CardCopy("345", "Example Game", "Trading Card"),
                    CardCopy(
                        "456", "Other Game", "Trading Card",
                        is_tradable=False,
                    ),
                ],
                bot_name="Bot A",
            )
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database), \
                 patch("app.main.QMessageBox.warning") as warning:
                window = MainWindow()
                try:
                    blocked_item = next(
                        window.order_assets.item(index)
                        for index in range(window.order_assets.count())
                        if window.order_assets.item(index).data(Qt.ItemDataRole.UserRole)
                        == ("Bot A", "456")
                    )
                    self.assertFalse(
                        bool(blocked_item.flags() & Qt.ItemFlag.ItemIsSelectable)
                    )
                    # Bypass the visual guard to verify preparation rechecks the database.
                    blocked_item.setFlags(
                        blocked_item.flags() | Qt.ItemFlag.ItemIsSelectable
                    )
                    blocked_item.setSelected(True)
                    window.order_recipient.setText(
                        "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123"
                    )
                    window._prepare_delivery_order()
                    warning.assert_called_once()
                    with sqlite3.connect(database) as db:
                        rows = db.execute(
                            "SELECT bot_name, asset_id FROM steam_asset_reservations"
                        ).fetchall()
                        orders = db.execute("SELECT COUNT(*) FROM delivery_orders").fetchone()[0]
                    self.assertEqual(rows, [])
                    self.assertEqual(orders, 0)
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_stale_snapshot_asset_cannot_be_reserved_even_if_ui_selection_is_forced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            store = InventorySnapshotStore(database)
            store.replace([CardCopy("789", "Example Game", "Trading Card")], bot_name="Bot A")
            with store._connect() as db:
                db.execute(
                    "UPDATE steam_inventory_snapshot_meta SET updated_at = ? WHERE bot_name = ?",
                    ("2020-01-01T00:00:00+00:00", "Bot A"),
                )

            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database), \
                 patch("app.main.QMessageBox.warning") as warning, \
                 patch("app.main.QMessageBox.information") as information:
                window = MainWindow()
                try:
                    self.assertEqual(window.order_assets.count(), 1)
                    self.assertIn("Нет подтверждённого снимка", window.order_assets.item(0).text())
                    forced_item = QListWidgetItem("Forced stale card")
                    forced_item.setData(Qt.ItemDataRole.UserRole, ("Bot A", "789"))
                    window.order_assets.addItem(forced_item)
                    forced_item.setSelected(True)
                    window.order_recipient.setText(
                        "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123"
                    )

                    window._prepare_delivery_order()

                    warning.assert_called_once()
                    information.assert_not_called()
                    with sqlite3.connect(database) as db:
                        reservation_count = db.execute(
                            "SELECT COUNT(*) FROM steam_asset_reservations"
                        ).fetchone()[0]
                        order_count = db.execute(
                            "SELECT COUNT(*) FROM delivery_orders"
                        ).fetchone()[0]
                    self.assertEqual(reservation_count, 0)
                    self.assertEqual(order_count, 0)
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_cancel_order_releases_only_its_reservations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            reservations = __import__(
                "games.steam_cards.reservations", fromlist=["AssetReservationStore"]
            ).AssetReservationStore(database)
            reservations.reserve("GD-TEST", [("Bot A", "123")])
            with sqlite3.connect(database) as db:
                db.execute("""
                    CREATE TABLE delivery_orders (
                        order_id TEXT PRIMARY KEY, recipient_trade_url TEXT NOT NULL,
                        status TEXT NOT NULL, asset_count INTEGER NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                db.execute("""
                    INSERT INTO delivery_orders (order_id, recipient_trade_url, status, asset_count)
                    VALUES ('GD-TEST', 'https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc',
                            'reserved — не отправлен', 1)
                """)
                db.execute("""
                    CREATE TABLE delivery_order_assets (
                        order_id TEXT NOT NULL, bot_name TEXT NOT NULL, asset_id TEXT NOT NULL
                    )
                """)
                db.execute(
                    "INSERT INTO delivery_order_assets VALUES ('GD-TEST', 'Bot A', '123')"
                )
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database), \
                 patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window = MainWindow()
                try:
                    window._load_saved_orders()
                    window.orders.selectRow(0)
                    window._cancel_selected_order()
                    with sqlite3.connect(database) as db:
                        status = db.execute(
                            "SELECT status FROM delivery_orders WHERE order_id = 'GD-TEST'"
                        ).fetchone()[0]
                        remaining = db.execute(
                            "SELECT COUNT(*) FROM steam_asset_reservations WHERE order_id = 'GD-TEST'"
                        ).fetchone()[0]
                    self.assertEqual(status, "cancelled — резерв снят")
                    self.assertEqual(remaining, 0)
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_cancel_order_does_not_release_another_orders_assets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            from games.steam_cards.reservations import AssetReservationStore
            reservations = AssetReservationStore(database)
            reservations.reserve("GD-ONE", [("Bot A", "789")])
            reservations.reserve("GD-TWO", [("Bot A", "890")])
            with sqlite3.connect(database) as db:
                db.execute("""
                    CREATE TABLE delivery_orders (
                        order_id TEXT PRIMARY KEY, recipient_trade_url TEXT NOT NULL,
                        status TEXT NOT NULL, asset_count INTEGER NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                db.executemany(
                    "INSERT INTO delivery_orders (order_id, recipient_trade_url, status, asset_count) "
                    "VALUES (?, 'https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc', "
                    "'reserved — не отправлен', 1)",
                    [("GD-ONE",), ("GD-TWO",)],
                )
                db.execute("""
                    CREATE TABLE delivery_order_assets (
                        order_id TEXT NOT NULL, bot_name TEXT NOT NULL, asset_id TEXT NOT NULL
                    )
                """)
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database), \
                 patch("app.main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window = MainWindow()
                try:
                    window._load_saved_orders()
                    target_row = next(
                        row for row in range(window.orders.rowCount())
                        if window.orders.item(row, 0).text() == "GD-ONE"
                    )
                    window.orders.selectRow(target_row)
                    window._cancel_selected_order()
                    self.assertEqual(reservations.list_for_order("GD-ONE"), [])
                    self.assertEqual(
                        reservations.list_for_order("GD-TWO"),
                        [{"bot_name": "Bot A", "asset_id": "890"}],
                    )
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_playerok_order_imports_once_and_prepares_under_same_local_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            InventorySnapshotStore(database).replace(
                [
                    CardCopy("567", "Example Game", "Trading Card"),
                    CardCopy("678", "Other Game", "Another Card"),
                ],
                bot_name="Bot A",
            )
            source_order = {
                "platform": "playerok",
                "order_id": "deal-123",
                "title": "Steam Card Set",
                "buyer": "buyer-a",
                "status": "PAID",
                "amount": "250",
                "created_at": "2026-10-01T00:00:00+00:00",
                "url": "https://playerok.com/deal/deal-123",
            }
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database), \
                 patch("app.main.QMessageBox.information") as info, \
                 patch("app.main.QMessageBox.warning") as warning:
                window = MainWindow()
                try:
                    window._import_marketplace_order(source_order)
                    self.assertEqual(window.orders.rowCount(), 1)
                    local_order_id = window.orders.item(0, 0).text()
                    self.assertEqual(window.orders.item(0, 1).text(), "Playerok · buyer-a")
                    self.assertEqual(window.orders.item(0, 2).text(), "Steam Card Set")
                    window._import_marketplace_order(source_order)
                    self.assertEqual(window.orders.rowCount(), 1)

                    window.order_recipient.setText(
                        "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123"
                    )
                    window.order_assets.item(0).setSelected(True)
                    window._prepare_delivery_order()
                    warning.assert_not_called()
                    info.assert_called_once()
                    with sqlite3.connect(database) as db:
                        order = db.execute(
                            "SELECT source_platform, source_order_id, source_title, source_buyer, "
                            "recipient_trade_url, status, asset_count, source_status, source_amount, source_url "
                            "FROM delivery_orders WHERE order_id = ?",
                            (local_order_id,),
                        ).fetchone()
                        reserved_assets = db.execute(
                            "SELECT bot_name, asset_id FROM steam_asset_reservations WHERE order_id = ?",
                            (local_order_id,),
                        ).fetchall()
                    self.assertEqual(
                        order,
                        (
                            "playerok", "deal-123", "Steam Card Set", "buyer-a",
                            "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=Abc_123",
                            "reserved — не отправлен", 1, "PAID", "250",
                            "https://playerok.com/deal/deal-123",
                        ),
                    )
                    self.assertEqual(reserved_assets, [("Bot A", "567")])
                    with sqlite3.connect(database) as db:
                        order_assets = db.execute(
                            "SELECT order_id, bot_name, asset_id FROM delivery_order_assets"
                        ).fetchall()
                        local_order_count = db.execute(
                            "SELECT COUNT(*) FROM delivery_orders"
                        ).fetchone()[0]
                    self.assertEqual(order_assets, [(local_order_id, "Bot A", "567")])
                    self.assertEqual(local_order_count, 1)
                    self.assertEqual(window.orders.rowCount(), 1)

                    # Re-importing a prepared Playerok sale must not create a separate manual order.
                    window._import_marketplace_order(source_order)
                    self.assertEqual(window.orders.rowCount(), 1)
                    window.order_assets.item(1).setSelected(True)
                    window._prepare_delivery_order()
                    warning.assert_called_once()
                    with sqlite3.connect(database) as db:
                        local_order_count = db.execute(
                            "SELECT COUNT(*) FROM delivery_orders"
                        ).fetchone()[0]
                        reserved_assets_after_repeat = db.execute(
                            "SELECT bot_name, asset_id FROM steam_asset_reservations WHERE order_id = ?",
                            (local_order_id,),
                        ).fetchall()
                    self.assertEqual(local_order_count, 1)
                    self.assertEqual(reserved_assets_after_repeat, [("Bot A", "567")])
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_main_window_constructs_with_expected_tabs(self):
        window = MainWindow()
        try:
            self.assertEqual(window.windowTitle(), "GameDelivery — локальная панель")
            self.assertEqual(
                [window.tabs.tabText(i) for i in range(window.tabs.count())],
                ["Заказы", "Площадки", "Инвентарь", "Модули игр", "Журнал", "Настройки", "ASF-боты"],
            )
            self.assertGreaterEqual(window.width(), 900)
            self.assertGreaterEqual(window.height(), 600)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()


    def test_startup_marks_interrupted_sending_order_as_unknown_and_locks_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "orders.sqlite3"
            with sqlite3.connect(database) as db:
                db.execute("""
                    CREATE TABLE delivery_orders (
                        order_id TEXT PRIMARY KEY,
                        recipient_trade_url TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        asset_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT '',
                        source_platform TEXT NOT NULL DEFAULT 'manual',
                        source_order_id TEXT,
                        source_title TEXT NOT NULL DEFAULT '',
                        source_buyer TEXT NOT NULL DEFAULT '',
                        source_status TEXT NOT NULL DEFAULT '',
                        source_amount TEXT NOT NULL DEFAULT '',
                        source_url TEXT NOT NULL DEFAULT '',
                        trade_offer_id TEXT NOT NULL DEFAULT '',
                        trade_confirmation_status TEXT NOT NULL DEFAULT ''
                    )
                """)
                db.execute(
                    "INSERT INTO delivery_orders (order_id, status, asset_count) VALUES (?, ?, ?)",
                    ("GD-INTERRUPTED", "sending — запрос ASF", 1),
                )

            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database):
                window = MainWindow()
                try:
                    with sqlite3.connect(database) as db:
                        status = db.execute(
                            "SELECT status, trade_confirmation_status FROM delivery_orders WHERE order_id = ?",
                            ("GD-INTERRUPTED",),
                        ).fetchone()
                    self.assertEqual(
                        status,
                        ("неизвестен результат отправки — проверь исходящие Steam", "unknown"),
                    )
                    window.orders.selectRow(0)
                    self.assertFalse(window.send_offer_button.isEnabled())
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()


    def test_game_catalog_contains_requested_games_and_filters_search(self):
        window = MainWindow()
        try:
            expected = ["cs2", "dota2", "steam_cards", "tf2", "rust"]
            self.assertEqual(window.game_list.count(), len(expected))
            self.assertEqual(
                [
                    window.game_list.item(index).data(Qt.ItemDataRole.UserRole)
                    for index in range(window.game_list.count())
                ],
                expected,
            )
            # The first catalog entry is CS2 (not yet available). Select Steam
            # explicitly before checking that its inventory action is enabled.
            window.game_list.setCurrentRow(expected.index("steam_cards"))
            self.assertTrue(window.open_game_inventory_button.isEnabled())

            window.game_search.setText("dota")
            self.assertEqual(window.game_list.count(), 1)
            self.assertEqual(
                window.game_list.item(0).data(Qt.ItemDataRole.UserRole),
                "dota2",
            )
            self.assertFalse(window.open_game_inventory_button.isEnabled())

            window.game_search.setText("нет такой игры")
            self.assertEqual(window.game_list.count(), 0)
            self.assertFalse(window.open_game_inventory_button.isEnabled())
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
