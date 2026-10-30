from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox
from app.main import MainWindow


class DatabaseMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_old_delivery_orders_schema_migrates_and_preserves_existing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "legacy.sqlite3"
            with sqlite3.connect(database) as db:
                db.execute("""
                    CREATE TABLE delivery_orders (
                        order_id TEXT PRIMARY KEY,
                        recipient_trade_url TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        asset_count INTEGER NOT NULL,
                        created_at TEXT NOT NULL DEFAULT ''
                    )
                """)
                db.execute(
                    "INSERT INTO delivery_orders "
                    "(order_id, recipient_trade_url, status, asset_count, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("GD-LEGACY", "", "reserved — не отправлен", 2, "2026-10-01T10:00:00Z"),
                )

            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database), \
                 patch("app.main.QMessageBox.critical") as critical:
                window = MainWindow()
                try:
                    critical.assert_not_called()
                    with sqlite3.connect(database) as db:
                        row = db.execute(
                            "SELECT order_id, status, asset_count, created_at, source_platform, "
                            "source_order_id, trade_confirmation_status FROM delivery_orders "
                            "WHERE order_id = ?",
                            ("GD-LEGACY",),
                        ).fetchone()
                        columns = {r[1] for r in db.execute("PRAGMA table_info(delivery_orders)")}
                        indexes = {r[1] for r in db.execute("PRAGMA index_list(delivery_orders)")}
                    self.assertEqual(
                        row,
                        ("GD-LEGACY", "reserved — не отправлен", 2, "2026-10-01T10:00:00Z", "manual", None, ""),
                    )
                    self.assertTrue({
                        "source_platform", "source_order_id", "trade_offer_id",
                        "trade_confirmation_status",
                    }.issubset(columns))
                    self.assertIn("idx_delivery_external_order", indexes)
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_duplicate_external_order_prevents_migration_from_dropping_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "legacy-duplicate.sqlite3"
            with sqlite3.connect(database) as db:
                db.execute("""
                    CREATE TABLE delivery_orders (
                        order_id TEXT PRIMARY KEY,
                        recipient_trade_url TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        asset_count INTEGER NOT NULL,
                        created_at TEXT NOT NULL DEFAULT '',
                        source_platform TEXT NOT NULL DEFAULT 'manual',
                        source_order_id TEXT
                    )
                """)
                # Existing rows that collide on the new uniqueness index.
                db.executemany(
                    "INSERT INTO delivery_orders "
                    "(order_id, status, asset_count, source_platform, source_order_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        ("GD-ONE", "imported", 0, "playerok", "deal-duplicate"),
                        ("GD-TWO", "imported", 0, "playerok", "deal-duplicate"),
                    ],
                )
            original_size = database.stat().st_size
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database), \
                 patch("app.main.QMessageBox.critical") as critical:
                window = MainWindow()
                try:
                    self.assertTrue(critical.called)
                    self.assertIn("Ошибка локальной базы данных", critical.call_args.args[1])
                    self.assertTrue(window.startup_error_label.text())
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()
            self.assertGreaterEqual(database.stat().st_size, original_size)
            with sqlite3.connect(database) as db:
                ids = [r[0] for r in db.execute("SELECT order_id FROM delivery_orders ORDER BY order_id")]
            self.assertEqual(ids, ["GD-ONE", "GD-TWO"])


if __name__ == "__main__":
    unittest.main()
