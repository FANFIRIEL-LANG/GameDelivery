from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from integrations.playerok.sales_polling import PlayerokSalesPoller
from integrations.playerok.sync import PlayerokDealSyncService
from tests.playerok_freshness_fixtures import reader_completed_snapshot


def deal(deal_id: str, status: str, *, listing_id: str | None = "listing-one", chat_id: str = "shared-chat"):
    return {
        "platform": "playerok",
        "deal_id": deal_id,
        "order_id": deal_id,
        "chat_id": chat_id,
        "listing_id": listing_id,
        "title": "Test listing",
        "buyer": "buyer",
        "buyer_id": "buyer-id",
        "status": status,
        "amount": "10",
        "created_at": "2099-01-01T00:00:00Z",
        "deal_url": "https://playerok.com/deal/" + deal_id,
        "chat_url": "https://playerok.com/chats/" + chat_id,
    }


class FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, callback):
        self._slots.append(callback)

    def emit(self):
        for callback in list(self._slots):
            callback()


class FakeTimer:
    def __init__(self):
        self.timeout = FakeSignal()
        self.single_shot = False
        self.active = False
        self.interval_ms = None

    def setSingleShot(self, value):
        self.single_shot = bool(value)

    def start(self, interval_ms):
        self.interval_ms = interval_ms
        self.active = True

    def stop(self):
        self.active = False

    def fire(self):
        if self.active:
            self.active = False
            self.timeout.emit()


class FakeReader:
    def __init__(self, finished):
        self.finished = finished
        self.session_lost = False
        self.cancelled = False

    def complete(self, snapshot=None, error=None, *, session_lost=False):
        self.session_lost = session_lost
        self.finished(snapshot, error)

    def cancel(self, *, stage="cancelled"):
        self.cancelled = True


class PlayerokSalesPollerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "polling.sqlite3")
        self.syncer = PlayerokDealSyncService(self.database)
        self.timer = FakeTimer()
        self.readers = []
        self.states = []
        self.session_available = True

        def reader_factory(finished):
            reader = FakeReader(finished)
            self.readers.append(reader)
            return reader

        self.poller = PlayerokSalesPoller(
            timer=self.timer,
            reader_factory=reader_factory,
            sync_snapshot=lambda snapshot, done: done(
                self.syncer.sync_browser_snapshot(snapshot), None
            ),
            session_available=lambda: self.session_available,
            status_changed=lambda state, detail: self.states.append((state, detail)),
            interval_ms=60_000,
        )

    def tearDown(self):
        self.poller.stop()
        self.temp.cleanup()

    def complete(self, reader_index, account, deals):
        self.readers[reader_index].complete(reader_completed_snapshot(account, deals))

    def test_poll_discovers_new_paid_deal_and_task_52_1_keeps_it_unbound(self):
        self.assertTrue(self.poller.start())
        self.complete(0, "seller-a", [deal("old-deal", "SENT")])
        self.assertTrue(self.timer.active)

        self.timer.fire()
        self.complete(1, "seller-a", [
            deal("old-deal", "SENT"), deal("new-paid", "PAID"),
        ])
        with self.database.connect() as db:
            stored = db.execute(
                "SELECT external_deal_id,platform_status,config_version_id FROM gd_deals "
                "WHERE playerok_account_id='seller-a' ORDER BY external_deal_id"
            ).fetchall()
            self.assertEqual([(row[0], row[1]) for row in stored], [
                ("new-paid", "PAID"), ("old-deal", "SENT"),
            ])
            self.assertIsNone(stored[0][2])
            for table in ("gd_orders", "gd_order_lines", "gd_delivery_legs", "gd_resource_reservations"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(self.poller.last_successful_result["new_deals"], 1)

    def test_repeat_snapshot_is_idempotent_and_known_status_is_updated(self):
        self.poller.start()
        self.complete(0, "seller-a", [deal("same-deal", "SENT")])
        self.timer.fire()
        changed = [deal("same-deal", "CONFIRMED")]
        self.complete(1, "seller-a", changed)
        self.assertEqual(self.poller.last_successful_result["new_deals"], 0)

        self.timer.fire()
        self.complete(2, "seller-a", changed)
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT platform_status FROM gd_deals").fetchone()[0], "CONFIRMED")
        self.assertEqual(self.poller.last_successful_result["new_deals"], 0)

    def test_incomplete_read_does_not_start_sync_or_complete_baseline(self):
        self.poller.start()
        self.readers[0].complete(None, "second page failed")
        self.assertEqual(self.states[-1][0], "read_error")
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playerok_sync_state").fetchone()[0], 0)
        self.assertTrue(self.timer.active)

    def test_transient_error_preserves_last_good_state_and_next_poll_recovers(self):
        self.poller.start()
        self.complete(0, "seller-a", [deal("stable-deal", "SENT")])
        good_result = self.poller.last_successful_result
        self.timer.fire()
        self.readers[1].complete(None, "temporary read failure")
        self.assertIs(self.poller.last_successful_result, good_result)
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 1)

        self.timer.fire()
        self.complete(2, "seller-a", [deal("stable-deal", "PAID")])
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT platform_status FROM gd_deals").fetchone()[0], "PAID")

    def test_overlapping_timer_ticks_are_ignored_until_current_cycle_finishes(self):
        self.poller.start()
        self.assertTrue(self.poller.busy)
        self.assertFalse(self.poller.poll_now())
        self.assertEqual(len(self.readers), 1)
        self.complete(0, "seller-a", [])
        self.assertFalse(self.poller.busy)
        self.assertTrue(self.timer.active)

    def test_next_cycle_waits_for_async_snapshot_persistence(self):
        pending_sync = []
        poller = PlayerokSalesPoller(
            timer=self.timer,
            reader_factory=lambda done: self.readers.append(FakeReader(done)) or self.readers[-1],
            sync_snapshot=lambda snapshot, done: pending_sync.append(done),
            session_available=lambda: True,
            interval_ms=60_000,
        )
        poller.start()
        self.readers[0].complete(reader_completed_snapshot("seller-a", [deal("d1", "SENT")]))
        self.assertTrue(poller.busy)
        self.assertFalse(poller.poll_now())
        self.assertEqual(len(self.readers), 1)
        pending_sync[0]({"baseline": True}, None)
        self.assertFalse(poller.busy)
        self.assertTrue(self.timer.active)
        poller.stop()

    def test_stop_cancels_reader_timer_and_ignores_late_result(self):
        self.poller.start()
        reader = self.readers[0]
        self.poller.stop()
        self.assertTrue(reader.cancelled)
        self.assertFalse(self.timer.active)
        reader.complete(reader_completed_snapshot("seller-a", [deal("late", "PAID")]))
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 0)

    def test_lost_browser_session_stops_polling(self):
        self.poller.start()
        self.readers[0].complete(None, "HTTP 401", session_lost=True)
        self.assertFalse(self.poller.active)
        self.assertFalse(self.timer.active)
        self.assertEqual(self.states[-1][0], "session_lost")

    def test_different_accounts_remain_scoped_and_cannot_mix_mid_poll(self):
        self.poller.start()
        self.complete(0, "seller-a", [deal("account-deal", "SENT")])
        self.timer.fire()
        self.complete(1, "seller-b", [deal("account-deal", "PAID")])
        self.assertFalse(self.poller.active)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT playerok_account_id,external_deal_id FROM gd_deals"
            ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("seller-a", "account-deal")])

        other_timer = FakeTimer()
        other_readers = []
        other = PlayerokSalesPoller(
            timer=other_timer,
            reader_factory=lambda done: other_readers.append(FakeReader(done)) or other_readers[-1],
            sync_snapshot=lambda snapshot, done: done(self.syncer.sync_browser_snapshot(snapshot), None),
            session_available=lambda: True,
        )
        other.start()
        other_readers[0].complete(reader_completed_snapshot("seller-b", [deal("account-deal", "PAID")]))
        other.stop()
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT playerok_account_id,external_deal_id FROM gd_deals "
                "ORDER BY playerok_account_id"
            ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("seller-a", "account-deal"), ("seller-b", "account-deal"),
        ])


if __name__ == "__main__":
    unittest.main()
