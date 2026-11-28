from __future__ import annotations

import tempfile
import unittest

from core.database.db import Database
from core.delivery.digital_codes import (
    DigitalCodeConflict,
    DigitalCodeError,
    DigitalCodeStore,
    preview_import,
)


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def get_password(self, service, account):
        return self.values.get((service, account))

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class DigitalCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(self.temp.name + "/codes.sqlite3")
        self.keyring = FakeKeyring()
        self.store = DigitalCodeStore(self.db, self.keyring)

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_rejects_duplicates_and_invalid_lines_without_writing(self):
        report = preview_import("A-1\nA-1\n" + chr(1) + "bad\n")
        self.assertFalse(report["ready"])
        self.assertEqual(report["duplicate_count"], 1)
        self.assertEqual(report["invalid_count"], 1)
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM gd_digital_codes").fetchone()[0], 0)

    def test_long_code_text_is_not_treated_as_a_path(self):
        text = "X" * 4000
        report = preview_import(text)
        self.assertEqual(report["valid_count"], 1)
        self.assertTrue(report["ready"])

    def test_import_is_protected_and_duplicate_import_does_not_add_stock(self):
        result = self.store.import_codes(product_id="game", batch_id="batch-1", source="A-1\nB-2\n")
        self.assertEqual(result["imported"], 2)
        with self.db.connect() as db:
            self.assertNotIn("A-1", db.execute("SELECT * FROM gd_digital_codes").fetchone())
        with self.assertRaises(DigitalCodeConflict):
            self.store.import_codes(product_id="game", batch_id="batch-2", source="A-1\n")
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM gd_digital_codes").fetchone()[0], 2)

    def test_reservation_is_exactly_once_and_reveal_is_explicit(self):
        self.store.import_codes(product_id="game", batch_id="batch-1", source="A-1\n")
        reservation = self.store.reserve(product_id="game", order_id="order-1")
        self.assertEqual(reservation["state"], "reserved")
        with self.assertRaises(DigitalCodeError):
            self.store.reserve(product_id="game", order_id="order-2")
        self.assertEqual(self.store.reveal(code_id=reservation["code_id"], order_id="order-1"), "A-1")
        with self.assertRaises(DigitalCodeError):
            self.store.reveal(code_id=reservation["code_id"], order_id="order-1")
        with self.db.connect() as db:
            audit = db.execute("SELECT action FROM gd_digital_code_audit ORDER BY created_at").fetchall()
            self.assertEqual([row[0] for row in audit], ["imported", "reserved", "revealed"])
    def test_reserve_normalizes_identifiers_at_store_boundary(self):
        self.store.import_codes(product_id="game", batch_id="batch-1", source="A-1\n")
        result = self.store.reserve(product_id=" game ", order_id=" order-1 ")
        self.assertEqual(result["product_id"], "game")
        self.assertEqual(result["order_id"], "order-1")

    def test_unknown_blocks_until_explicit_reconcile(self):
        self.store.import_codes(product_id="game", batch_id="batch-1", source="A-1\n")
        reservation = self.store.reserve(product_id="game", order_id="order-1")
        self.store.mark_unknown(code_id=reservation["code_id"], order_id="order-1")
        with self.assertRaises(DigitalCodeError):
            self.store.reveal(code_id=reservation["code_id"], order_id="order-1")
        self.store.reconcile(code_id=reservation["code_id"], order_id="order-1", outcome="released", actor="operator")
        with self.assertRaises(DigitalCodeError):
            self.store.reserve(product_id="game", order_id="order-2")
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM gd_digital_codes").fetchone()[0], "released")


if __name__ == "__main__":
    unittest.main()

