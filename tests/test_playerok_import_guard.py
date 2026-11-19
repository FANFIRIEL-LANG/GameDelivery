from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.main import MainWindow
from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from integrations.playerok.sync import PlayerokDealSyncService
from tests.playerok_freshness_fixtures import reader_completed_snapshot


class _StatusBar:
    def __init__(self):
        self.messages = []

    def showMessage(self, message):
        self.messages.append(str(message))


class _ListWidget:
    def rowCount(self):
        return 0


class _Tabs:
    def setCurrentIndex(self, _index):
        pass


class _WindowStub:
    def __init__(self, path):
        self._database_path = path
        self._status_bar = _StatusBar()
        self.orders = _ListWidget()
        self.tabs = _Tabs()

    def statusBar(self):
        return self._status_bar

    def _load_saved_orders(self):
        pass


class PlayerokImportGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "orders.sqlite3"
        self.database = Database(self.path)
        self.ledger = DeliveryLedger(self.database)
        self.sync = PlayerokDealSyncService(self.database)
        self.window = _WindowStub(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_account_or_missing_canonical_order_never_creates_legacy_row(self):
        MainWindow._import_marketplace_order(self.window, {
            "platform": "playerok", "deal_id": "not-found", "status": "PAID",
        })
        MainWindow._import_marketplace_order(self.window, {
            "platform": "playerok", "deal_id": "not-found", "account_user_id": "seller-1",
            "status": "PAID",
        })
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='delivery_orders'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)

    def test_unresolved_baseline_and_other_account_cannot_import_canonical_order(self):
        self.sync.sync_browser_snapshot(reader_completed_snapshot("seller-1", [
            {"deal_id": "unresolved", "status": "PAID", "listing_id": None,
             "source_listing_ref": "slug-is-not-canonical"},
        ]))
        for account in ("seller-1", "seller-2"):
            MainWindow._import_marketplace_order(self.window, {
                "platform": "playerok", "deal_id": "unresolved", "account_user_id": account,
                "status": "PAID",
            })
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='delivery_orders'"
            ).fetchone()[0], 0)

    def test_paid_deal_without_proven_config_is_not_imported_as_canonical_or_legacy_order(self):
        config = self.ledger.create_listing_config_version(
            listing_id="configured-listing", source_platform="playerok",
            external_listing_id="canonical-item-id", delivery_quantity_per_deal=3,
            item_selectors={"app_id": 730, "context_id": 2}, bot_pool=["Bot-A"],
        )
        self.sync.sync_browser_snapshot(reader_completed_snapshot("seller-1", [
            {"deal_id": "baseline", "status": "SENT", "listing_id": "canonical-item-id"},
        ]))
        self.sync.sync_browser_snapshot(reader_completed_snapshot("seller-1", [
            {"deal_id": "baseline", "status": "SENT", "listing_id": "canonical-item-id"},
            {"deal_id": "new-paid", "status": "PAID", "listing_id": "canonical-item-id"},
        ]))
        MainWindow._import_marketplace_order(self.window, {
            "platform": "playerok", "deal_id": "new-paid", "account_user_id": "seller-1",
            "status": "PAID",
        })
        with self.database.connect() as db:
            deal_row = db.execute(
                "SELECT d.config_version_id FROM gd_deals d "
                "WHERE d.external_deal_id='new-paid' AND d.playerok_account_id='seller-1'"
            ).fetchone()
            self.assertIsNone(deal_row["config_version_id"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_order_lines").fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='delivery_orders'"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
