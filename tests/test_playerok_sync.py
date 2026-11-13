from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from integrations.playerok.sync import PlayerokDealSyncService


def deal(deal_id: str, listing_id: str, status: str = "PAID", chat_id: str | None = None):
    return {
        "platform": "playerok",
        "deal_id": deal_id,
        "order_id": deal_id,
        "chat_id": chat_id or "chat-" + deal_id,
        "listing_id": listing_id,
        "title": "Test listing",
        "buyer": "buyer",
        "buyer_id": "buyer-id",
        "status": status,
        "amount": "10",
        "created_at": "2026-11-13T10:00:00Z",
        "deal_url": "https://playerok.com/deal/" + deal_id,
        "chat_url": "https://playerok.com/chats/" + (chat_id or "chat-" + deal_id),
        "url": "https://playerok.com/deal/" + deal_id,
    }


class FakePlayerok:
    account_user_id = "seller-account-1"

    def __init__(self, baseline, active):
        self.baseline = list(baseline)
        self.active = list(active)
        self.remote = {
            row["deal_id"]: dict(row)
            for row in [*baseline, *active]
        }
        self.mutation_calls = []
        self.raise_after_mutation = False

    def fetch_sales(self, *, statuses=None, first=50, max_pages=10000):
        return [dict(row) for row in (self.baseline if statuses is None else self.active)]

    def fetch_deal(self, deal_id):
        return dict(self.remote[deal_id])

    def update_deal_status(self, deal_id, status):
        self.mutation_calls.append((deal_id, status))
        self.remote[deal_id]["status"] = status
        if self.raise_after_mutation:
            raise TimeoutError("remote response lost after server accepted mutation")
        return dict(self.remote[deal_id])


class PlayerokDealSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "playerok.sqlite3")
        self.ledger = DeliveryLedger(self.database)
        self.syncer = PlayerokDealSyncService(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def configure_listing(self, external_listing_id="listing-live"):
        return self.ledger.create_listing_config_version(
            listing_id="configured-" + external_listing_id,
            source_platform="playerok",
            external_listing_id=external_listing_id,
            delivery_quantity_per_deal=2,
            item_selectors={"game_id": "cs2", "app_id": 730, "context_id": 2},
            bot_pool=["Bot-A", "Bot-B"],
            title="Configured CS2 listing",
        )

    def normalized_deal_id(self, external_deal_id):
        with self.database.connect() as db:
            row = db.execute(
                "SELECT deal_id FROM gd_deals WHERE source_platform='playerok' AND external_deal_id=?",
                (external_deal_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        return row["deal_id"]

    def test_first_sync_is_full_historical_baseline_and_never_queues_delivery(self):
        adapter = FakePlayerok(
            baseline=[
                deal("old-paid", "listing-old", "PAID", "chat-old"),
                deal("old-sent", "listing-old", "SENT", "chat-sent"),
                deal("old-confirmed", "listing-old", "CONFIRMED", "chat-confirmed"),
            ],
            active=[],
        )
        result = self.syncer.sync(adapter)
        self.assertTrue(result["baseline"])
        self.assertEqual(result["historical_deals"], 3)
        with self.database.connect() as db:
            rows = db.execute("""
                SELECT external_deal_id,chat_id,listing_id,is_historical,playerok_account_id
                FROM gd_deals ORDER BY external_deal_id
            """).fetchall()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playerok_status_outbox").fetchone()[0], 0)
        self.assertEqual(
            [(r["external_deal_id"], r["chat_id"], r["is_historical"], r["playerok_account_id"]) for r in rows],
            [
                ("old-confirmed", "chat-confirmed", 1, "seller-account-1"),
                ("old-paid", "chat-old", 1, "seller-account-1"),
                ("old-sent", "chat-sent", 1, "seller-account-1"),
            ],
        )
        self.assertTrue(all(r["listing_id"].startswith("playerok-listing:") for r in rows))
        self.assertEqual(adapter.mutation_calls, [])

    def test_new_paid_deal_creates_order_and_delivered_transition_sends_once_then_tracks_confirmed(self):
        self.configure_listing()
        adapter = FakePlayerok(
            baseline=[deal("old-deal", "listing-live", "PAID")],
            active=[deal("new-deal", "listing-live", "PAID", "chat-new")],
        )
        first = self.syncer.sync(adapter)
        self.assertTrue(first["baseline"])
        second = self.syncer.sync(adapter)
        self.assertFalse(second["baseline"])
        self.assertEqual(second["new_deals"], 1)
        normalized_id = self.normalized_deal_id("new-deal")
        with self.database.connect() as db:
            order = db.execute(
                "SELECT order_id,status,total_quantity FROM gd_orders WHERE deal_id=?",
                (normalized_id,),
            ).fetchone()
            self.assertEqual(order["status"], "pending")
            self.assertEqual(order["total_quantity"], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playerok_status_outbox").fetchone()[0], 0)
            db.execute("UPDATE gd_orders SET status='DELIVERED' WHERE order_id=?", (order["order_id"],))
            queued = db.execute(
                "SELECT state,idempotency_key,external_deal_id FROM playerok_status_outbox WHERE order_id=?",
                (order["order_id"],),
            ).fetchone()
        self.assertEqual(queued["state"], "pending")
        self.assertEqual(queued["idempotency_key"], "playerok-send:" + order["order_id"])
        self.assertEqual(queued["external_deal_id"], "new-deal")
        with self.database.connect() as db:
            db.execute("UPDATE gd_orders SET status='PROCESSING' WHERE order_id=?", (order["order_id"],))
            db.execute("UPDATE gd_orders SET status='DELIVERED' WHERE order_id=?", (order["order_id"],))
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM playerok_status_outbox WHERE order_id=?", (order["order_id"],)).fetchone()[0],
                1,
                "Повторный переход в DELIVERED не создаёт вторую Playerok мутацию",
            )

        self.syncer.sync(adapter)
        self.assertEqual(adapter.mutation_calls, [("new-deal", "SENT")])
        with self.database.connect() as db:
            state = db.execute(
                "SELECT state,attempt_count,last_platform_status FROM playerok_status_outbox WHERE order_id=?",
                (order["order_id"],),
            ).fetchone()
        self.assertEqual(tuple(state), ("sent", 1, "SENT"))

        adapter.remote["new-deal"]["status"] = "CONFIRMED"
        self.syncer.sync(adapter)
        self.assertEqual(adapter.mutation_calls, [("new-deal", "SENT")])
        with self.database.connect() as db:
            state = db.execute(
                "SELECT state,confirmed_at,last_platform_status FROM playerok_status_outbox WHERE order_id=?",
                (order["order_id"],),
            ).fetchone()
        self.assertEqual(state["state"], "confirmed")
        self.assertTrue(state["confirmed_at"])
        self.assertEqual(state["last_platform_status"], "CONFIRMED")

    def test_ambiguous_remote_mutation_is_reconciled_read_only_without_resend(self):
        self.configure_listing()
        adapter = FakePlayerok(
            baseline=[deal("historical", "listing-live")],
            active=[deal("paid-after-baseline", "listing-live")],
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        normalized_id = self.normalized_deal_id("paid-after-baseline")
        with self.database.connect() as db:
            order = db.execute("SELECT order_id FROM gd_orders WHERE deal_id=?", (normalized_id,)).fetchone()
            db.execute("UPDATE gd_orders SET status='DELIVERED' WHERE order_id=?", (order["order_id"],))
        adapter.raise_after_mutation = True
        self.syncer.sync(adapter)
        with self.database.connect() as db:
            state = db.execute(
                "SELECT state,attempt_count,last_error_type FROM playerok_status_outbox WHERE order_id=?",
                (order["order_id"],),
            ).fetchone()
        self.assertEqual(state["state"], "unknown")
        self.assertEqual(state["attempt_count"], 1)
        self.assertEqual(state["last_error_type"], "TimeoutError")
        adapter.raise_after_mutation = False
        self.syncer.sync(adapter)
        self.assertEqual(adapter.mutation_calls, [("paid-after-baseline", "SENT")])
        with self.database.connect() as db:
            state = db.execute(
                "SELECT state,attempt_count FROM playerok_status_outbox WHERE order_id=?",
                (order["order_id"],),
            ).fetchone()
        self.assertEqual(tuple(state), ("sent", 1))

    def test_historical_deal_can_never_enqueue_playerok_status_mutation(self):
        adapter = FakePlayerok(
            baseline=[deal("old-deal", "listing-old", "PAID")],
            active=[],
        )
        self.syncer.sync(adapter)
        normalized_id = self.normalized_deal_id("old-deal")
        self.ledger.create_order(
            order_id="legacy-order-for-old-deal",
            deal_id=normalized_id,
            total_quantity=1,
        )
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_orders SET status='DELIVERED' WHERE order_id=?",
                ("legacy-order-for-old-deal",),
            )
        self.syncer.process_status_outbox(adapter, adapter.account_user_id)
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playerok_status_outbox").fetchone()[0], 0)
        self.assertEqual(adapter.mutation_calls, [])


if __name__ == "__main__":
    unittest.main()
