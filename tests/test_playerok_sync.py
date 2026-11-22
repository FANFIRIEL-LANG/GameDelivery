from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.recipes import UnsupportedLegacyRecipeError
from integrations.playerok.sync import PlayerokDealSyncService
from tests.playerok_freshness_fixtures import reader_completed_snapshot


def deal(deal_id: str, listing_id: str | None, status: str = "PAID", chat_id: str | None = None):
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
        "created_at": "2099-01-01T00:00:00Z",
        "deal_url": "https://playerok.com/deal/" + deal_id,
        "chat_url": "https://playerok.com/chats/" + (chat_id or "chat-" + deal_id),
        "url": "https://playerok.com/deal/" + deal_id,
    }


class MutableSendPolicy:
    def __init__(self):
        self.enabled = False

    def auto_send_enabled(self):
        return self.enabled

    def run_auto_dispatch_claim(self, claim):
        if not self.enabled:
            return False, None
        return True, claim()


class AutoClaimClosedPolicy:
    def __init__(self):
        self.claim_called = False

    def auto_send_enabled(self):
        return True

    def run_auto_dispatch_claim(self, claim):
        self.claim_called = True
        return False, None


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
        self.send_policy = MutableSendPolicy()
        self.syncer = PlayerokDealSyncService(self.database, send_policy=self.send_policy)

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

    def configure_recipe(self, items, external_listing_id="listing-live"):
        return self.ledger.create_recipe_config_version(
            listing_id="typed-" + external_listing_id,
            source_platform="playerok",
            external_listing_id=external_listing_id,
            recipe_items=items,
            title="Typed recipe",
        )

    def normalized_deal_id(self, external_deal_id, account="seller-account-1"):
        with self.database.connect() as db:
            row = db.execute(
                "SELECT deal_id FROM gd_deals WHERE source_platform='playerok' "
                "AND playerok_account_id=? AND external_deal_id=?",
                (account, external_deal_id),
            ).fetchone()
        self.assertIsNotNone(row)
        return row["deal_id"]

    def seed_existing_order_snapshot(self, external_deal_id, config):
        """Seed a previously resolved order for downstream status/re-sync tests."""
        account = "seller-account-1"
        deal_id = self.normalized_deal_id(external_deal_id, account)
        config_id = config["config_version_id"]
        order_id = self.syncer._stable("playerok-order", account + "\x1f" + external_deal_id)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE gd_deals SET config_version_id=? WHERE deal_id=? AND listing_id=?",
                (config_id, deal_id, config["listing_id"]),
            )
            version = db.execute(
                "SELECT recipe_schema_version,delivery_quantity_per_deal "
                "FROM gd_config_versions WHERE config_version_id=?", (config_id,),
            ).fetchone()
            typed = version["recipe_schema_version"] is not None
            if typed:
                recipe = DeliveryLedger.recipe_for_config_version(db, config_id, allow_legacy=False)
                quantity = sum(item.quantity_per_order for item in recipe.items)
            else:
                quantity = int(version["delivery_quantity_per_deal"])
                try:
                    recipe = DeliveryLedger.recipe_for_config_version(db, config_id, allow_legacy=True)
                except UnsupportedLegacyRecipeError:
                    recipe = None
            db.execute(
                "INSERT INTO gd_orders(order_id,deal_id,status,total_quantity) VALUES(?,?,'pending',?)",
                (order_id, deal_id, quantity),
            )
            if recipe is not None:
                DeliveryLedger.materialize_recipe_order_lines_in_transaction(
                    db, order_id, config_id, allow_legacy=not typed
                )
        return order_id

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

    def test_existing_resolved_order_transition_sends_once_then_tracks_confirmed(self):
        config = self.configure_listing()
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
        self.seed_existing_order_snapshot("new-deal", config)
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

        self.syncer.sync(adapter, allow_status_mutation=True)
        self.assertEqual(adapter.mutation_calls, [])
        self.assertEqual(adapter.remote["new-deal"]["status"], "PAID")
        self.send_policy.enabled = True
        self.syncer.sync(adapter, allow_status_mutation=True)
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

    def test_new_paid_typed_recipe_waits_without_proven_version(self):
        config = self.configure_recipe([{
            "item_id": "text", "position": 1, "kind": "digital_text_unit",
            "spec_version": 1, "quantity_per_order": 2, "required": True,
            "stock_source_id": "codes", "executor_key": "text_delivery",
            "spec": {"allocation": "one_unique_unit"},
        }])
        adapter = FakePlayerok(
            baseline=[deal("history", "listing-live", "PAID")],
            active=[deal("typed-single", "listing-live", "PAID")],
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        normalized_id = self.normalized_deal_id("typed-single")
        with self.database.connect() as db:
            row = db.execute(
                "SELECT config_version_id FROM gd_deals WHERE deal_id=?", (normalized_id,)
            ).fetchone()
            self.assertIsNone(row["config_version_id"])
            for table in ("gd_orders", "gd_order_lines", "gd_delivery_legs", "gd_resource_reservations"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        self.assertTrue(config["recipe_items"])

    def test_mixed_typed_paid_deal_waits_without_proven_version(self):
        cards = {
            "item_id": "cards", "position": 1, "kind": "steam_card_set",
            "spec_version": 1, "quantity_per_order": 1, "required": True,
            "stock_source_id": None, "executor_key": "steam_trade_offer",
            "spec": {"app_id": 753, "context_id": "6",
                      "selection": "buyer_badges_and_complete_sets", "game_name": "Example",
                      "allowed_bot_pool": ["Bot-A"]},
        }
        cs2 = {
            "item_id": "cs2", "position": 2, "kind": "steam_asset",
            "spec_version": 1, "quantity_per_order": 5, "required": False,
            "stock_source_id": None, "executor_key": "steam_trade_offer",
            "spec": {"game_id": "cs2", "app_id": 730, "context_id": "2",
                     "selector": {"market_hash_name": "Case"},
                     "allowed_bot_pool": ["Bot-A"]},
        }
        first = self.configure_recipe([cards, cs2])
        adapter = FakePlayerok(
            baseline=[deal("history", "listing-live", "PAID")],
            active=[deal("typed-mixed", "listing-live", "PAID")],
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        normalized_id = self.normalized_deal_id("typed-mixed")
        with self.database.connect() as db:
            row = db.execute(
                "SELECT config_version_id FROM gd_deals WHERE deal_id=?", (normalized_id,)
            ).fetchone()
            self.assertIsNone(row["config_version_id"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_order_lines").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_delivery_legs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_resource_reservations").fetchone()[0], 0)
            db.execute("UPDATE gd_listings SET title='Updated title' WHERE listing_id=?",
                       (first["listing_id"],))

        second = self.ledger.create_recipe_config_version(
            listing_id=first["listing_id"], source_platform="playerok", external_listing_id="listing-live",
            recipe_items=[{**cs2, "quantity_per_order": 9}, cards], title="Updated recipe",
        )
        self.syncer.sync(adapter)
        with self.database.connect() as db:
            bound_version = db.execute(
                "SELECT d.config_version_id FROM gd_deals d WHERE d.deal_id=?", (normalized_id,)
            ).fetchone()[0]
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_order_lines").fetchone()[0], 0)
        self.assertIsNone(bound_version)
        self.assertNotEqual(first["config_version_id"], second["config_version_id"])

    def test_legacy_paid_configs_do_not_create_orders_without_proven_version(self):
        self.ledger.create_listing_config_version(
            listing_id="legacy-configured", source_platform="playerok",
            external_listing_id="listing-live", delivery_quantity_per_deal=5,
            item_selectors={"game_id": "cs2", "app_id": 730, "context_id": 2,
                            "market_hash_name": "Case"},
            bot_pool=["Bot-A"],
        )
        adapter = FakePlayerok(
            baseline=[deal("history", "listing-live", "PAID")],
            active=[deal("legacy-compatible", "listing-live", "PAID")],
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        normalized_id = self.normalized_deal_id("legacy-compatible")
        with self.database.connect() as db:
            config = db.execute(
                "SELECT config_version_id FROM gd_deals WHERE deal_id=?", (normalized_id,)
            ).fetchone()[0]
            self.assertIsNone(config)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_order_lines").fetchone()[0], 0)

        self.ledger.create_listing_config_version(
            listing_id="legacy-ambiguous", source_platform="playerok",
            external_listing_id="listing-ambiguous", delivery_quantity_per_deal=2,
            item_selectors={"delivery_mode": "cs2_items", "app_id": 730, "context_id": 2,
                            "items": [{"market_hash_name": "A"}, {"market_hash_name": "B"}]},
            bot_pool=["Bot-A"],
        )
        ambiguous = deal("legacy-ambiguous-deal", "listing-ambiguous", "PAID")
        adapter.active.append(ambiguous)
        adapter.remote[ambiguous["deal_id"]] = dict(ambiguous)
        self.syncer.sync(adapter)
        with self.database.connect() as db:
            ambiguous_order = db.execute(
                "SELECT o.order_id,o.total_quantity,d.config_version_id FROM gd_orders o "
                "JOIN gd_deals d ON d.deal_id=o.deal_id WHERE d.external_deal_id=?",
                (ambiguous["deal_id"],),
            ).fetchone()
            self.assertIsNone(ambiguous_order)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_order_lines").fetchone()[0], 0)

    def test_order_creation_guard_requires_persisted_config_binding(self):
        self.configure_recipe([
            {"item_id": "text-a", "position": 1, "kind": "digital_text_unit",
             "spec_version": 1, "quantity_per_order": 1, "required": True,
             "stock_source_id": "codes", "executor_key": "text_delivery",
             "spec": {"allocation": "one_unique_unit"}},
            {"item_id": "text-b", "position": 2, "kind": "digital_text_unit",
             "spec_version": 1, "quantity_per_order": 1, "required": False,
             "stock_source_id": "codes", "executor_key": "text_delivery",
             "spec": {"allocation": "one_unique_unit"}},
        ])
        adapter = FakePlayerok(
            baseline=[deal("history", "listing-live", "PAID")],
            active=[deal("atomic-failure", "listing-live", "PAID")],
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        normalized_id = self.normalized_deal_id("atomic-failure")
        with self.database.connect() as db:
            config = db.execute(
                "SELECT config_version_id,listing_id FROM gd_deals WHERE deal_id=?", (normalized_id,)
            ).fetchone()
            self.assertIsNone(config["config_version_id"])
            latest_config = db.execute(
                "SELECT config_version_id,delivery_quantity_per_deal,item_selectors_json,bot_pool_json "
                "FROM gd_config_versions WHERE listing_id=? ORDER BY version_number DESC LIMIT 1",
                (config["listing_id"],),
            ).fetchone()
            self.syncer._maybe_create_order(
                db, normalized_id, "atomic-failure", "seller-account-1", "PAID", 0,
                config["listing_id"], latest_config["config_version_id"], dict(latest_config),
            )
            for table in ("gd_orders", "gd_order_lines", "gd_delivery_legs", "gd_resource_reservations"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_ambiguous_remote_mutation_is_reconciled_read_only_without_resend(self):
        config = self.configure_listing()
        adapter = FakePlayerok(
            baseline=[deal("historical", "listing-live")],
            active=[deal("paid-after-baseline", "listing-live")],
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        normalized_id = self.normalized_deal_id("paid-after-baseline")
        order_id = self.seed_existing_order_snapshot("paid-after-baseline", config)
        with self.database.connect() as db:
            db.execute("UPDATE gd_orders SET status='DELIVERED' WHERE order_id=?", (order_id,))
        self.send_policy.enabled = True
        adapter.raise_after_mutation = True
        self.syncer.sync(adapter, allow_status_mutation=True)
        with self.database.connect() as db:
            state = db.execute(
                "SELECT state,attempt_count,last_error_type FROM playerok_status_outbox WHERE order_id=?",
                (order_id,),
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
                (order_id,),
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

    def test_browser_snapshot_baseline_is_historical_and_does_not_create_orders(self):
        self.configure_listing("listing-old")
        result = self.syncer.sync_browser_snapshot(
            reader_completed_snapshot(
                "browser-seller", [deal("browser-old", "listing-old", "PAID", "chat-old")]
            )
        )
        self.assertTrue(result["baseline"])
        self.assertEqual(result["new_deals"], 0)
        with self.database.connect() as db:
            row = db.execute(
                "SELECT external_deal_id,is_historical,playerok_account_id FROM gd_deals"
            ).fetchone()
            self.assertEqual(tuple(row), ("browser-old", 1, "browser-seller"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playerok_status_outbox").fetchone()[0], 0)

    def test_browser_snapshot_requires_trusted_provenance_before_any_write(self):
        from integrations.playerok.browser_sales import BrowserSalesSnapshot

        with self.assertRaisesRegex(ValueError, "provenance"):
            self.syncer.sync_browser_snapshot(
                BrowserSalesSnapshot("browser-seller", [deal("fake", "listing")])
            )
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playerok_sync_state").fetchone()[0], 0)

    def test_stale_trusted_browser_snapshot_is_rejected_before_any_write(self):
        from unittest.mock import patch
        from tests.playerok_freshness_fixtures import reader_completed_snapshot

        snapshot = reader_completed_snapshot(
            "stale-seller", [deal("stale-deal", "listing-stale", "SENT")]
        )
        with patch("integrations.playerok.browser_sales.monotonic", return_value=10**9):
            with self.assertRaisesRegex(ValueError, "stale"):
                self.syncer.sync_browser_snapshot(snapshot)
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playerok_sync_state").fetchone()[0], 0)

    def test_browser_baseline_transaction_rolls_back_deals_and_state_on_write_error(self):
        snapshot = reader_completed_snapshot("browser-seller", [
            deal("browser-good", "listing-good"), deal("browser-fails", "listing-fails")
        ])
        original = self.syncer._upsert_deal
        calls = 0

        def fail_second(db, row, account, historical):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("injected isolated test failure")
            return original(db, row, account, historical)

        with patch.object(self.syncer, "_upsert_deal", side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, "injected"):
                self.syncer.sync_browser_snapshot(snapshot)
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playerok_sync_state").fetchone()[0], 0)

    def test_browser_incremental_transaction_rolls_back_all_deals_on_write_error(self):
        self.syncer.sync_browser_snapshot(reader_completed_snapshot("rollback-seller", []))
        with self.database.connect() as db:
            before = db.execute(
                "SELECT last_sync_at FROM playerok_sync_state WHERE account_user_id='rollback-seller'"
            ).fetchone()[0]
        snapshot = reader_completed_snapshot("rollback-seller", [
            deal("incremental-good", "listing-good", "SENT"),
            deal("incremental-fails", "listing-fails", "SENT"),
        ])
        original = self.syncer._upsert_deal
        calls = 0

        def fail_second(db, row, account, historical):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("injected incremental failure")
            return original(db, row, account, historical)

        with patch.object(self.syncer, "_upsert_deal", side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, "incremental failure"):
                self.syncer.sync_browser_snapshot(snapshot)
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_deals WHERE playerok_account_id='rollback-seller'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT last_sync_at FROM playerok_sync_state WHERE account_user_id='rollback-seller'"
            ).fetchone()[0], before)

    def test_browser_snapshot_incremental_paid_deal_waits_without_version(self):
        self.configure_listing()
        self.syncer.sync_browser_snapshot(reader_completed_snapshot(
            "browser-seller", [deal("browser-old", "listing-live", "SENT")]
        ))
        snapshot_deals = [
            deal("browser-old", "listing-live", "SENT"),
            deal("browser-new", "listing-live", "PAID", "chat-new"),
        ]
        first = self.syncer.sync_browser_snapshot(reader_completed_snapshot("browser-seller", snapshot_deals))
        second = self.syncer.sync_browser_snapshot(reader_completed_snapshot("browser-seller", snapshot_deals))
        self.assertEqual(first["new_deals"], 1)
        self.assertEqual(second["new_deals"], 0)
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_order_lines").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_delivery_legs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_resource_reservations").fetchone()[0], 0)
            row = db.execute(
                "SELECT config_version_id FROM gd_deals WHERE external_deal_id='browser-new'"
            ).fetchone()
            self.assertIsNone(row["config_version_id"])

    def test_first_seen_pre_baseline_paid_deal_is_historical_and_unprepared(self):
        self.configure_listing("listing-late-history")
        baseline = reader_completed_snapshot("late-history-seller", [
            deal("baseline-record", "listing-late-history", "SENT"),
        ])
        self.syncer.sync_browser_snapshot(baseline)
        late_history = deal("late-paid-history", "listing-late-history", "PAID")
        late_history["created_at"] = "2020-01-01T00:00:00Z"
        missing_time = deal("missing-time-paid", "listing-late-history", "PAID")
        missing_time["created_at"] = ""
        malformed_time = deal("malformed-time-paid", "listing-late-history", "PAID")
        malformed_time["created_at"] = "not-a-timestamp"
        result = self.syncer.sync_browser_snapshot(reader_completed_snapshot(
            "late-history-seller", [late_history, missing_time, malformed_time]
        ))
        self.assertEqual(result["new_deals"], 0)
        self.assertEqual(result["historical_deals"], 3)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT is_historical,config_version_id FROM gd_deals "
                "WHERE external_deal_id IN ('late-paid-history','missing-time-paid','malformed-time-paid') "
                "ORDER BY external_deal_id"
            ).fetchall()
            self.assertEqual([tuple(row) for row in rows], [(1, None)] * 3)
            for table in ("gd_orders", "gd_order_lines", "gd_delivery_legs", "gd_resource_reservations"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_incremental_sync_keeps_first_seen_pre_baseline_paid_deal_historical(self):
        self.configure_listing("listing-incremental-history")
        adapter = FakePlayerok(
            baseline=[deal("baseline-record", "listing-incremental-history", "SENT")],
            active=[],
        )
        self.syncer.sync(adapter)
        late_history = deal("late-paid-history", "listing-incremental-history", "PAID")
        late_history["created_at"] = "2020-01-01T00:00:00Z"
        missing_time = deal("missing-time-paid", "listing-incremental-history", "PAID")
        missing_time["created_at"] = ""
        malformed_time = deal("malformed-time-paid", "listing-incremental-history", "PAID")
        malformed_time["created_at"] = "not-a-timestamp"
        adapter.active = [late_history, missing_time, malformed_time]

        result = self.syncer.sync(adapter)
        self.assertEqual(result["new_deals"], 0)
        self.assertEqual(result["historical_deals"], 3)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT external_deal_id,is_historical,config_version_id FROM gd_deals "
                "WHERE external_deal_id IN ('late-paid-history','missing-time-paid','malformed-time-paid') "
                "ORDER BY external_deal_id"
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    ("late-paid-history", 1, None),
                    ("malformed-time-paid", 1, None),
                    ("missing-time-paid", 1, None),
                ],
            )
            for table in ("gd_orders", "gd_order_lines", "gd_delivery_legs", "gd_resource_reservations"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_unresolved_deals_complete_baseline_without_creating_delivery_rows(self):
        snapshot = reader_completed_snapshot("seller-unresolved", [
            deal("deal-no-listing", None, "PAID", "shared-chat"),
        ])
        result = self.syncer.sync_browser_snapshot(snapshot)
        self.assertTrue(result["baseline"])
        with self.database.connect() as db:
            row = db.execute(
                "SELECT external_deal_id,chat_id,listing_id,config_version_id,is_historical,"
                "source_listing_ref,listing_resolution_status FROM gd_deals"
            ).fetchone()
            self.assertEqual(tuple(row), (
                "deal-no-listing", "shared-chat", None, None, 1, "", "unresolved"
            ))
            for table in ("gd_orders", "gd_order_lines", "gd_delivery_legs", "gd_resource_reservations"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
            state = db.execute(
                "SELECT baseline_completed FROM playerok_sync_state WHERE account_user_id='seller-unresolved'"
            ).fetchone()
            self.assertEqual(state[0], 1)

    def test_account_scoped_deals_and_multiple_deals_in_one_chat_remain_distinct(self):
        rows = [
            deal("shared-deal-id", None, "PAID", "shared-chat"),
            deal("second-deal", None, "SENT", "shared-chat"),
        ]
        self.syncer.sync_browser_snapshot(reader_completed_snapshot("seller-one", rows))
        self.syncer.sync_browser_snapshot(reader_completed_snapshot("seller-two", [rows[0]]))
        with self.database.connect() as db:
            stored = db.execute(
                "SELECT playerok_account_id,external_deal_id,chat_id,deal_id FROM gd_deals "
                "WHERE source_platform='playerok' ORDER BY playerok_account_id,external_deal_id"
            ).fetchall()
        self.assertEqual(len(stored), 3)
        self.assertEqual(len({row[3] for row in stored}), 3)
        self.assertEqual(sum(row[2] == "shared-chat" for row in stored), 3)

    def test_manual_listing_resolution_updates_existing_row_and_preserves_historical_flag(self):
        self.syncer.sync_browser_snapshot(reader_completed_snapshot("seller-one", [
            {**deal("unresolved", None), "source_listing_ref": "untrusted-slug"}
        ]))
        original_id = self.normalized_deal_id("unresolved", "seller-one")
        self.configure_listing("canonical-item-id")
        result = self.syncer.resolve_unresolved_deal(
            "seller-one", "unresolved", "canonical-item-id"
        )
        self.assertTrue(result["resolved"])
        with self.database.connect() as db:
            row = db.execute(
                "SELECT deal_id,listing_id,config_version_id,is_historical,listing_resolution_status,source_listing_ref "
                "FROM gd_deals WHERE playerok_account_id='seller-one' AND external_deal_id='unresolved'"
            ).fetchone()
            self.assertEqual(tuple(row), (original_id, "configured-canonical-item-id", None, 1, "resolved", ""))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)

    def test_unresolved_listing_resolution_is_account_scoped_and_exact_id_only(self):
        self.syncer.sync_browser_snapshot(reader_completed_snapshot("seller-one", [
            {**deal("unresolved", None), "source_listing_ref": "display-slug"}
        ]))
        self.configure_listing("canonical-item-id")
        with self.assertRaises(KeyError):
            self.syncer.resolve_unresolved_deal("seller-two", "unresolved", "canonical-item-id")
        with self.assertRaises(KeyError):
            self.syncer.resolve_unresolved_deal("seller-one", "unresolved", "display-slug")
        with self.database.connect() as db:
            row = db.execute(
                "SELECT listing_id,listing_resolution_status,source_listing_ref FROM gd_deals "
                "WHERE playerok_account_id='seller-one' AND external_deal_id='unresolved'"
            ).fetchone()
        self.assertEqual(tuple(row), (None, "unresolved", "display-slug"))

    def test_incremental_paid_deal_with_known_listing_waits_for_historical_version(self):
        first_config = self.configure_listing("listing-versioned-paid")
        adapter = FakePlayerok(
            baseline=[deal("history-paid-version", "listing-versioned-paid", "PAID")],
            active=[deal("late-paid-version", "listing-versioned-paid", "PAID")],
        )
        self.syncer.sync(adapter)
        first = self.syncer.sync(adapter)
        self.assertEqual(first["new_deals"], 1)
        normalized_id = self.normalized_deal_id("late-paid-version")
        with self.database.connect() as db:
            row = db.execute(
                "SELECT platform_status,listing_id,config_version_id,is_historical,listing_resolution_status "
                "FROM gd_deals WHERE deal_id=?", (normalized_id,),
            ).fetchone()
            self.assertEqual(
                tuple(row),
                ("PAID", first_config["listing_id"], None, 0, "resolved"),
            )
            for table in ("gd_orders", "gd_order_lines", "gd_delivery_legs", "gd_resource_reservations"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

        self.ledger.create_listing_config_version(
            listing_id=first_config["listing_id"], source_platform="playerok",
            external_listing_id="listing-versioned-paid", delivery_quantity_per_deal=9,
            item_selectors={"game_id": "cs2", "app_id": 730, "context_id": 2},
            bot_pool=["Bot-A"], title="Later version",
        )
        second = self.syncer.sync(adapter)
        self.assertEqual(second["new_deals"], 0)
        with self.database.connect() as db:
            row = db.execute(
                "SELECT config_version_id FROM gd_deals WHERE deal_id=?", (normalized_id,)
            ).fetchone()
            self.assertIsNone(row["config_version_id"])
            for table in ("gd_orders", "gd_order_lines", "gd_delivery_legs", "gd_resource_reservations"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_existing_unordered_deal_drops_provisional_version_when_status_is_paid(self):
        config = self.configure_listing("listing-status-transition")
        pending = deal("status-transition", "listing-status-transition", "SENT")
        adapter = FakePlayerok(
            baseline=[deal("history-status-transition", "listing-status-transition", "PAID")],
            active=[pending],
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        normalized_id = self.normalized_deal_id("status-transition")
        with self.database.connect() as db:
            provisional = db.execute(
                "SELECT config_version_id FROM gd_deals WHERE deal_id=?", (normalized_id,)
            ).fetchone()
        self.assertEqual(provisional["config_version_id"], config["config_version_id"])

        self.ledger.create_listing_config_version(
            listing_id=config["listing_id"], source_platform="playerok",
            external_listing_id="listing-status-transition", delivery_quantity_per_deal=4,
            item_selectors={"game_id": "cs2", "app_id": 730, "context_id": 2},
            bot_pool=["Bot-A"], title="Updated listing",
        )
        adapter.active[0]["status"] = "PAID"
        adapter.remote["status-transition"]["status"] = "PAID"
        self.syncer.sync(adapter)
        with self.database.connect() as db:
            row = db.execute(
                "SELECT platform_status,config_version_id FROM gd_deals WHERE deal_id=?",
                (normalized_id,),
            ).fetchone()
            self.assertEqual(tuple(row), ("PAID", None))
            for table in ("gd_orders", "gd_order_lines", "gd_delivery_legs", "gd_resource_reservations"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_unresolved_paid_listing_resolution_does_not_choose_config_version(self):
        adapter = FakePlayerok(
            baseline=[deal("history-unresolved-paid", None, "PAID")],
            active=[deal("late-unresolved-paid", None, "PAID")],
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        normalized_id = self.normalized_deal_id("late-unresolved-paid")
        with self.database.connect() as db:
            row = db.execute(
                "SELECT listing_id,config_version_id,listing_resolution_status,is_historical "
                "FROM gd_deals WHERE deal_id=?", (normalized_id,),
            ).fetchone()
            self.assertEqual(tuple(row), (None, None, "unresolved", 0))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_orders").fetchone()[0], 0)

        self.configure_listing("listing-resolved-later")
        self.syncer.resolve_unresolved_deal(
            "seller-account-1", "late-unresolved-paid", "listing-resolved-later"
        )
        with self.database.connect() as db:
            row = db.execute(
                "SELECT listing_id,config_version_id,listing_resolution_status "
                "FROM gd_deals WHERE deal_id=?", (normalized_id,),
            ).fetchone()
            self.assertEqual(
                tuple(row),
                ("configured-listing-resolved-later", None, "resolved"),
            )
            for table in ("gd_orders", "gd_order_lines", "gd_delivery_legs", "gd_resource_reservations"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)


    def test_paid_deal_waiting_for_listing_setup_does_not_adopt_new_config(self):
        adapter = FakePlayerok(
            baseline=[deal("existing", "listing-needs-setup", "PAID")],
            active=[deal("new-pa-id", "listing-needs-setup", "PAID")],
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        new_deal_id = self.normalized_deal_id("new-pa-id")
        with self.database.connect() as db:
            row = db.execute(
                "SELECT listing_id,config_version_id FROM gd_deals WHERE deal_id=?",
                (new_deal_id,),
            ).fetchone()
            self.assertIsNone(row["config_version_id"])
            self.assertIsNone(db.execute(
                "SELECT order_id FROM gd_orders WHERE deal_id=?", (new_deal_id,)
            ).fetchone())
        config = self.ledger.create_listing_config_version(
            listing_id=row["listing_id"],
            source_platform="playerok",
            external_listing_id="listing-needs-setup",
            delivery_quantity_per_deal=1,
            item_selectors={"game_id": "cs2", "app_id": 730, "context_id": 2},
            bot_pool=["Bot-A"],
            title="Now configured",
        )
        self.syncer.sync(adapter)
        with self.database.connect() as db:
            updated = db.execute(
                "SELECT config_version_id FROM gd_deals WHERE deal_id=?", (new_deal_id,)
            ).fetchone()
            order = db.execute(
                "SELECT total_quantity,status FROM gd_orders WHERE deal_id=?", (new_deal_id,)
            ).fetchone()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_order_lines").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_delivery_legs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_resource_reservations").fetchone()[0], 0)
        self.assertIsNone(updated["config_version_id"])
        self.assertIsNone(order)
        self.assertTrue(config["config_version_id"])


    def test_existing_order_sync_keeps_playerok_paid_until_embedded_browser_action_is_enabled(self):
        config = self.configure_listing()
        adapter = FakePlayerok(
            baseline=[deal("old-browser", "listing-live", "PAID")],
            active=[deal("new-browser", "listing-live", "PAID")],
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        normalized_id = self.normalized_deal_id("new-browser")
        order_id = self.seed_existing_order_snapshot("new-browser", config)
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_orders SET status='DELIVERED' WHERE order_id=?", (order_id,)
            )
        self.syncer.sync(adapter)
        self.assertEqual(adapter.mutation_calls, [])
        with self.database.connect() as db:
            state = db.execute(
                "SELECT state,last_platform_status FROM playerok_status_outbox WHERE order_id=?",
                (order_id,),
            ).fetchone()
        self.assertEqual(tuple(state), ("pending", "PAID"))

    def test_playerok_status_mutation_uses_atomic_auto_dispatch_claim(self):
        config = self.configure_listing()
        adapter = FakePlayerok(
            baseline=[deal("old-atomic", "listing-live", "PAID")],
            active=[deal("new-atomic", "listing-live", "PAID")],
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        normalized_id = self.normalized_deal_id("new-atomic")
        order_id = self.seed_existing_order_snapshot("new-atomic", config)
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_orders SET status='DELIVERED' WHERE order_id=?", (order_id,)
            )
        self.syncer.sync(adapter)

        policy = AutoClaimClosedPolicy()
        self.syncer.send_policy = policy
        self.syncer.process_status_outbox(
            adapter, adapter.account_user_id, allow_status_mutation=True
        )

        self.assertTrue(policy.claim_called)
        self.assertEqual(adapter.mutation_calls, [])
        with self.database.connect() as db:
            state = db.execute(
                "SELECT state,attempt_count FROM playerok_status_outbox WHERE order_id=?",
                (order_id,),
            ).fetchone()
        self.assertEqual(tuple(state), ("pending", 0))

    def test_listing_config_changes_do_not_mutate_existing_paid_order_snapshot(self):
        item = {
            "item_id": "historical-line", "position": 1, "kind": "digital_text_unit",
            "spec_version": 1, "quantity_per_order": 2, "required": True,
            "stock_source_id": "codes", "executor_key": "text_delivery",
            "spec": {"allocation": "one_unique_unit"},
        }
        first_config = self.configure_recipe([item], "listing-versioned")
        adapter = FakePlayerok(
            baseline=[deal("history", "listing-versioned", "PAID")],
            active=[deal("new-versioned", "listing-versioned", "PAID")],
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        normalized_id = self.normalized_deal_id("new-versioned")
        order_id = self.seed_existing_order_snapshot("new-versioned", first_config)
        with self.database.connect() as db:
            before = db.execute(
                "SELECT config_version_id FROM gd_deals WHERE deal_id=?", (normalized_id,)
            ).fetchone()[0]
            order_before = db.execute(
                "SELECT total_quantity FROM gd_orders WHERE deal_id=?", (normalized_id,)
            ).fetchone()[0]
            lines_before = [tuple(row) for row in db.execute(
                "SELECT config_version_id,line_number,item_type,quantity,item_selector_json "
                "FROM gd_order_lines WHERE order_id=? ORDER BY line_number", (order_id,)
            ).fetchall()]
        self.assertEqual(before, first_config["config_version_id"])
        self.assertEqual(order_before, 2)
        next_config = self.ledger.create_recipe_config_version(
            listing_id=first_config["listing_id"],
            source_platform="playerok",
            external_listing_id="listing-versioned",
            recipe_items=[{**item, "quantity_per_order": 3}],
            title="Future version",
        )
        self.syncer.sync(adapter)
        with self.database.connect() as db:
            after = db.execute(
                "SELECT config_version_id FROM gd_deals WHERE deal_id=?", (normalized_id,)
            ).fetchone()[0]
            order_after = db.execute(
                "SELECT total_quantity FROM gd_orders WHERE deal_id=?", (normalized_id,)
            ).fetchone()[0]
            lines_after = [tuple(row) for row in db.execute(
                "SELECT config_version_id,line_number,item_type,quantity,item_selector_json "
                "FROM gd_order_lines WHERE order_id=? ORDER BY line_number", (order_id,)
            ).fetchall()]
        self.assertEqual(after, first_config["config_version_id"])
        self.assertNotEqual(after, next_config["config_version_id"])
        self.assertEqual(order_after, 2)
        self.assertEqual(lines_after, lines_before)


if __name__ == "__main__":
    unittest.main()
