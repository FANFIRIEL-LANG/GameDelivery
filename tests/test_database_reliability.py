from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from core.orders.service import OrderService


class DatabaseReliabilityTests(unittest.TestCase):
    def test_initialize_is_idempotent_and_preserves_existing_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            db = Database(path)
            service = OrderService(db)
            created, inserted = service.enqueue(
                source="test",
                external_id="same-order",
                item_type="card",
                item_name="Trading Card",
            )
            self.assertTrue(inserted)

            db.initialize()
            db.initialize()
            same, inserted_again = service.enqueue(
                source="test",
                external_id="same-order",
                item_type="card",
                item_name="Different payload",
            )

            self.assertFalse(inserted_again)
            self.assertEqual(same["id"], created["id"])
            self.assertEqual(same["item_name"], "Trading Card")
            self.assertEqual(len(service.list_orders()), 1)

    def test_initialize_does_not_overwrite_corrupt_existing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "corrupt.sqlite3"
            original = b"not a sqlite database; keep original bytes"
            path.write_bytes(original)
            db = Database(path)

            with self.assertRaises(sqlite3.DatabaseError):
                db.initialize()

            self.assertEqual(path.read_bytes(), original)

    def test_initialize_creates_parent_directory_and_schema_for_new_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.sqlite3"
            self.assertFalse(path.parent.exists())
            db = Database(path)

            db.initialize()

            self.assertTrue(path.is_file())
            with db.connect() as connection:
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                indexes = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
            self.assertIn("orders", tables)
            self.assertIn("idx_orders_status_created", indexes)


if __name__ == "__main__":
    unittest.main()
