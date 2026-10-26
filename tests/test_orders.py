import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from core.orders.service import OrderService


class OrderServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.service = OrderService(Database(self.db_path))

    def test_enqueue_persists_and_returns_created_flag(self):
        order, created = self.service.enqueue(
            source="Playerok",
            external_id="deal-123",
            item_type="steam_card_set",
            item_name="Demo set",
            quantity=2,
            payload={"chat_id": "private-local-reference"},
        )
        duplicate, duplicate_created = self.service.enqueue(
            source="Playerok",
            external_id="deal-123",
            item_type="steam_card_set",
            item_name="Changed title",
            quantity=9,
        )
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(order["id"], duplicate["id"])
        self.assertEqual(duplicate["quantity"], 2)
        self.assertEqual(duplicate["payload"]["chat_id"], "private-local-reference")
        self.assertEqual(self.service.get(order["id"])["status"], "queued")

    def test_queue_order_and_status_filter(self):
        first, _ = self.service.enqueue(
            source="demo", external_id="1", item_type="card", item_name="First"
        )
        self.service.enqueue(
            source="demo", external_id="2", item_type="card", item_name="Second"
        )
        self.assertEqual(self.service.next_queued()["id"], first["id"])
        self.service.transition(first["id"], "processing")
        self.assertEqual(len(self.service.list_orders("queued")), 1)
        self.assertEqual(len(self.service.list_orders("processing")), 1)

    def test_valid_transitions_and_error_message(self):
        order, _ = self.service.enqueue(
            source="demo", external_id="3", item_type="card", item_name="Set"
        )
        self.service.transition(order["id"], "processing")
        failed = self.service.transition(order["id"], "failed", "Temporary failure")
        self.assertEqual(failed["error_message"], "Temporary failure")
        retried = self.service.transition(order["id"], "queued")
        self.assertEqual(retried["status"], "queued")
        completed = self.service.transition(order["id"], "processing")
        completed = self.service.transition(order["id"], "completed")
        self.assertEqual(completed["status"], "completed")
        with self.assertRaises(ValueError):
            self.service.transition(order["id"], "queued")

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            self.service.enqueue(
                source="demo", external_id="bad", item_type="card",
                item_name="Set", quantity=0
            )
        with self.assertRaises(ValueError):
            self.service.enqueue(
                source="", external_id="bad", item_type="card", item_name="Set"
            )

    def test_database_survives_service_restart(self):
        order, _ = self.service.enqueue(
            source="demo", external_id="persist", item_type="card", item_name="Saved"
        )
        restarted = OrderService(Database(self.db_path))
        self.assertEqual(restarted.get(order["id"])["item_name"], "Saved")


if __name__ == "__main__":
    unittest.main()
