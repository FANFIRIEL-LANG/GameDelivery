import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox
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
                [CardCopy("asset-123", "Example Game", "Trading Card")],
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
                    self.assertEqual(reservation, ("Bot A", "asset-123"))
                    with sqlite3.connect(database) as db:
                        order_asset = db.execute(
                            "SELECT order_id, bot_name, asset_id FROM delivery_order_assets"
                        ).fetchone()
                    self.assertEqual(order_asset[1:], ("Bot A", "asset-123"))
                    window._load_saved_orders()
                    self.assertEqual(window.orders.rowCount(), 1)
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
            reservations.reserve("GD-TEST", [("Bot A", "asset-123")])
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
                    "INSERT INTO delivery_order_assets VALUES ('GD-TEST', 'Bot A', 'asset-123')"
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
            reservations.reserve("GD-ONE", [("Bot A", "asset-1")])
            reservations.reserve("GD-TWO", [("Bot A", "asset-2")])
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
                        [{"bot_name": "Bot A", "asset_id": "asset-2"}],
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
                    CardCopy("asset-playerok-1", "Example Game", "Trading Card"),
                    CardCopy("asset-playerok-2", "Other Game", "Another Card"),
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
                    self.assertEqual(reserved_assets, [("Bot A", "asset-playerok-1")])
                    with sqlite3.connect(database) as db:
                        order_assets = db.execute(
                            "SELECT order_id, bot_name, asset_id FROM delivery_order_assets"
                        ).fetchall()
                        local_order_count = db.execute(
                            "SELECT COUNT(*) FROM delivery_orders"
                        ).fetchone()[0]
                    self.assertEqual(order_assets, [(local_order_id, "Bot A", "asset-playerok-1")])
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
                    self.assertEqual(reserved_assets_after_repeat, [("Bot A", "asset-playerok-1")])
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


if __name__ == "__main__":
    unittest.main()
