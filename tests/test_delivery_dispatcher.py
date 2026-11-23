from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.dispatcher import DeliveryDispatcher
from core.delivery.playerok_freshness import PlayerokFreshnessGate
from core.delivery.reconciliation import DeliveryReconciliation
from core.delivery.send_policy import DeliverySendPolicy
from core.delivery.stock_ledger import QuantityStockLedger, StockResource
from core.delivery.stock_identity import StockItemIdentity
from integrations.asf.trade_credentials import SteamTradeCredentialStore
from integrations.playerok.sync import PlayerokDealSyncService
from tests.playerok_freshness_fixtures import reader_completed_snapshot


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def set_password(self, service, account, password):
        self.values[(service, account)] = password

    def get_password(self, service, account):
        return self.values.get((service, account))

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class MemorySettings:
    def __init__(self):
        self.values = {}

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        pass

    def status(self):
        return 0


class FakeASF:
    def __init__(self):
        self.send_result = {}
        self.send_error = {}
        self.statuses = {}
        self.sent = []
        self.inventory_refreshes = []

    def send_trade_offer(
        self, *, order_id, bot_name, trade_url, asset_ids, app_id=753, context_id=6,
        asset_amounts=None,
    ):
        self.sent.append((
            order_id, bot_name, trade_url, tuple(asset_ids),
            asset_amounts, app_id, context_id,
        ))
        if bot_name in self.send_error:
            raise self.send_error[bot_name]
        return self.send_result[bot_name]

    def get_trade_offer_status(self, *, bot_name, trade_offer_id):
        return self.statuses[(bot_name, trade_offer_id)]

    def get_bot_inventory(self, bot_name, *, app_id, context_id):
        self.inventory_refreshes.append((bot_name, app_id, context_id))
        return {"Success": True}


class DeliveryDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "delivery.sqlite3")
        self.ledger = DeliveryLedger(self.db)
        self.playerok_sync = PlayerokDealSyncService(self.db)
        self.keyring = MemoryKeyring()
        self.credentials = SteamTradeCredentialStore(backend=self.keyring)
        self.asf = FakeASF()
        self.send_policy = DeliverySendPolicy(MemorySettings())
        self.send_policy.set_auto_send_enabled(True)
        self.freshness_gate = PlayerokFreshnessGate()
        self.sent_offers = self.root / "sent_offers.json"
        self.reconciler = DeliveryReconciliation(
            self.db, self.asf, sent_offers_path=self.sent_offers
        )
        self.dispatcher = DeliveryDispatcher(
            self.db, self.asf, self.credentials,
            restricted_buyer_message="Настройте Steam Guard и повторно обратитесь в магазин.",
            send_policy=self.send_policy,
            playerok_freshness_gate=self.freshness_gate,
        )
        self.trade_url = self.credentials.store_trade_url(
            "order-1", "https://steamcommunity.com/tradeoffer/new/?partner=123&token=secret_token"
        )

    def tearDown(self):
        self.temp.cleanup()

    def create_order(self, order_id="order-1", bots=("Bot-A",), asset_amounts=None):
        config = self.ledger.create_listing_config_version(
            listing_id=f"listing-{order_id}",
            source_platform="playerok",
            external_listing_id=f"listing-external-{order_id}",
            delivery_quantity_per_deal=1,
            item_selectors={"game_id": "cs2", "app_id": 730, "context_id": 2},
            bot_pool=list(bots),
        )
        self.ledger.create_deal(
            deal_id=f"deal-{order_id}",
            source_platform="playerok",
            external_deal_id=f"deal-external-{order_id}",
            chat_id=f"chat-{order_id}",
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            platform_status="PAID",
        )
        with self.db.connect() as db:
            db.execute(
                "UPDATE gd_deals SET playerok_account_id=?, is_historical=0, "
                "listing_resolution_status='resolved' WHERE deal_id=?",
                (f"account-{order_id}", f"deal-{order_id}"),
            )
            db.execute(
                "INSERT INTO playerok_sync_state(account_user_id,baseline_completed) VALUES(?,1)",
                (f"account-{order_id}",),
            )
        self.ledger.create_order(order_id=order_id, deal_id=f"deal-{order_id}", total_quantity=1)
        line_id = f"line-{order_id}"
        self.ledger.add_order_line(
            order_line_id=line_id, order_id=order_id, line_number=1,
            item_type="cs2_item", item_name="AK-47",
            item_selector={"game_id": "cs2", "app_id": 730, "context_id": 2},
            quantity=len(bots),
        )
        legs = []
        for index, bot in enumerate(bots, start=1):
            leg_id = f"leg-{order_id}-{index}"
            self.ledger.add_delivery_leg(
                leg_id=leg_id, order_line_id=line_id, bot_name=bot,
                app_id=730, context_id=2, asset_ids=[str(index * 100)],
                status="reserved",
                asset_amounts=(
                    {str(index * 100): asset_amounts[str(index * 100)]}
                    if asset_amounts is not None and str(index * 100) in asset_amounts
                    else None
                ),
            )
            with self.db.connect() as db:
                db.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,order_line_id,game_id,app_id,context_id,"
                    "bot_name,asset_id,units,state) VALUES(?,?,?,?,?,?,?,?,?,'active')",
                    (f"reservation-{order_id}-{index}", "order", line_id, "cs2", 730,
                     "2", bot, str(index * 100),
                     asset_amounts.get(str(index * 100), 1) if asset_amounts is not None else 1),
                )
            legs.append(leg_id)
        with self.db.connect() as db:
            db.execute("UPDATE gd_orders SET status='reserved' WHERE order_id=?", (order_id,))
        self.ledger.bind_order_recipient(order_id, 76561197960265851)
        if order_id != "order-1":
            self.credentials.store_trade_url(
                order_id, "https://steamcommunity.com/tradeoffer/new/?partner=123&token=secret_token"
            )
        return legs

    def _freshness_arguments(self, order_id, leg_id):
        with self.db.connect() as db:
            row = db.execute(
                "SELECT external_deal_id,platform_status,playerok_account_id FROM gd_deals WHERE deal_id=?",
                (f"deal-{order_id}",),
            ).fetchone()
        if row is None or row["platform_status"] != "PAID":
            return {}
        lease = self.freshness_gate.try_acquire_order(order_id)
        proof = self.freshness_gate.issue_from_complete_snapshot(
            reader_completed_snapshot(row["playerok_account_id"], [
                {"deal_id": row["external_deal_id"], "status": "PAID"}
            ]),
            expected_account_user_id=row["playerok_account_id"],
            deal_id=row["external_deal_id"], order_id=order_id, leg_id=leg_id,
            lease=lease,
        )
        return {"playerok_freshness_authorization": proof, "playerok_order_lease": lease}

    def dispatch(self, order_id, leg_id, *, manual_authorization=None, retry_generation=0):
        safe_url = self.trade_url if order_id == "order-1" else (
            "https://steamcommunity.com/tradeoffer/new/?partner=123"
        )
        freshness = self._freshness_arguments(order_id, leg_id)
        try:
            return self.dispatcher.dispatch_leg(
                order_id=order_id, leg_id=leg_id, stored_trade_url=safe_url,
                retry_generation=retry_generation,
                manual_authorization=manual_authorization, **freshness,
            )
        finally:
            if freshness.get("playerok_order_lease") is not None:
                self.freshness_gate.release_order(freshness["playerok_order_lease"])

    def test_transport_failure_becomes_unknown_and_reconciliation_recovers_offer_without_resend(self):
        leg = self.create_order()[0]
        self.asf.send_error["Bot-A"] = TimeoutError("IPC timeout")
        result = self.dispatch("order-1", leg)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(len(self.asf.sent), 1)
        self.assertEqual(self.asf.sent[0][-2:], (730, 2))
        retry = self.dispatch("order-1", leg)
        self.assertFalse(retry["dispatched"])
        self.assertEqual(len(self.asf.sent), 1, "UNKNOWN must prohibit automatic resend")

        self.sent_offers.write_text(json.dumps({
            "order-1": {
                "OrderId": "order-1", "BotName": "Bot-A",
                "AssetIds": ["100"], "OfferIds": ["90001"], "State": "sent",
            }
        }), encoding="utf-8")
        self.asf.statuses[("Bot-A", "90001")] = {
            "success": True, "mode": "trade-status", "found": True,
            "tradeOfferId": "90001", "botName": "Bot-A", "status": "Accepted",
        }

        reconciled = self.reconciler.reconcile(order_id="order-1")
        self.assertEqual(reconciled[0]["status"], "ACCEPTED")
        self.assertEqual(len(self.asf.sent), 1, "reconciliation must never resend the leg")
        with self.db.connect() as db:
            row = db.execute("SELECT status, trade_offer_id FROM gd_delivery_legs WHERE leg_id=?", (leg,)).fetchone()
            order = db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()
        self.assertEqual((row["status"], row["trade_offer_id"]), ("ACCEPTED", "90001"))
        self.assertEqual(order["status"], "DELIVERED")

    def test_recovered_offer_updates_attempt_and_keeps_order_processing_until_accepted(self):
        leg = self.create_order()[0]
        self.asf.send_error["Bot-A"] = TimeoutError("IPC timeout")
        self.assertEqual(self.dispatch("order-1", leg)["status"], "UNKNOWN")
        self.sent_offers.write_text(json.dumps({
            "order-1": {
                "OrderId": "order-1", "BotName": "Bot-A",
                "AssetIds": ["100"], "OfferIds": ["90002"], "State": "sent",
            }
        }), encoding="utf-8")
        self.asf.statuses[("Bot-A", "90002")] = {
            "success": True, "mode": "trade-status", "found": True,
            "tradeOfferId": "90002", "botName": "Bot-A", "status": "Active",
        }

        result = self.reconciler.reconcile(order_id="order-1")

        self.assertEqual(result[0]["status"], "SENT_TO_STEAM")
        with self.db.connect() as db:
            leg_row = db.execute(
                "SELECT status,trade_offer_id FROM gd_delivery_legs WHERE leg_id=?", (leg,)
            ).fetchone()
            attempt = db.execute(
                "SELECT status,result_json FROM gd_delivery_attempts WHERE leg_id=?", (leg,)
            ).fetchone()
            order_status = db.execute(
                "SELECT status FROM gd_orders WHERE order_id='order-1'"
            ).fetchone()[0]
        self.assertEqual((leg_row["status"], leg_row["trade_offer_id"]), ("SENT_TO_STEAM", "90002"))
        self.assertEqual(attempt["status"], "sent_to_steam")
        self.assertTrue(json.loads(attempt["result_json"])["recovered_from_unknown"])
        self.assertEqual(order_status, "PROCESSING")

    def test_startup_recovery_converts_interrupted_dispatch_to_unknown_before_polling(self):
        leg = self.create_order()[0]
        with self.db.connect() as db:
            self.ledger.create_delivery_attempt(
                order_id="order-1", leg_id=leg, retry_generation=0,
                status="dispatching", result={"dispatch_started": True}, connection=db,
            )
            db.execute(
                "UPDATE gd_delivery_legs SET status='DISPATCHING' WHERE leg_id=?", (leg,)
            )
        recovered = DeliveryReconciliation.recover_interrupted_dispatches(self.db)
        self.assertEqual(recovered, 1)
        with self.db.connect() as db:
            status = db.execute(
                "SELECT status FROM gd_delivery_legs WHERE leg_id=?", (leg,)
            ).fetchone()[0]
            attempt_status = db.execute(
                "SELECT status FROM gd_delivery_attempts WHERE leg_id=?", (leg,)
            ).fetchone()[0]
        self.assertEqual(status, "UNKNOWN")
        self.assertEqual(attempt_status, "unknown")
        # Normal reconciliation may only read/recover; it must never create a new offer.
        result = self.reconciler.reconcile(order_id="order-1")
        self.assertEqual(result[0]["status"], "UNKNOWN")
        self.assertEqual(self.asf.sent, [])

    def test_global_off_blocks_dispatch_without_attempt_or_external_effects(self):
        leg = self.create_order()[0]
        off_dispatcher = DeliveryDispatcher(self.db, self.asf, self.credentials)

        result = off_dispatcher.dispatch_leg(
            order_id="order-1", leg_id=leg, stored_trade_url=self.trade_url
        )

        self.assertFalse(result["dispatched"])
        self.assertEqual(result["status"], "AUTO_SEND_DISABLED")
        self.assertEqual(self.asf.sent, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations "
                "WHERE order_line_id='line-order-1' AND state='active'"
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT status FROM gd_delivery_legs WHERE leg_id=?", (leg,)
            ).fetchone()[0], "reserved")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_events WHERE order_id='order-1'"
            ).fetchone()[0], 0)

    def test_manual_grant_dispatches_exact_leg_while_global_auto_is_off_and_is_audited(self):
        leg = self.create_order()[0]
        self.send_policy.set_auto_send_enabled(False)
        self.asf.send_result["Bot-A"] = {"success": True, "offerIds": ["manual-offer"]}
        grant = self.send_policy.issue_manual_authorization("order-1", leg)

        result = self.dispatch("order-1", leg, manual_authorization=grant)

        self.assertTrue(result["dispatched"])
        self.assertEqual(len(self.asf.sent), 1)
        self.assertFalse(self.send_policy.auto_send_enabled())
        self.assertFalse(self.send_policy.validate_manual_authorization(grant, "order-1", leg))
        with self.db.connect() as db:
            attempt = db.execute(
                "SELECT result_json FROM gd_delivery_attempts WHERE order_id='order-1'"
            ).fetchone()
        audit = json.loads(attempt["result_json"])
        self.assertEqual(audit["dispatch_authorization"]["mode"], "manual")
        self.assertEqual(audit["dispatch_authorization"]["authorization_id"], grant.authorization_id)

    def test_dispatch_blocks_missing_recipient_snapshot_before_creating_attempt(self):
        leg = self.create_order()[0]
        with self.db.connect() as db:
            db.execute("UPDATE gd_orders SET recipient_steam_id64=NULL WHERE order_id='order-1'")
        result = self.dispatch("order-1", leg)
        self.assertEqual(result["status"], "RECIPIENT_NOT_BOUND")
        self.assertEqual(self.asf.sent, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'").fetchone()[0], 0)

    def test_dispatch_blocks_changed_trade_url_recipient_before_creating_attempt(self):
        leg = self.create_order()[0]
        freshness = self._freshness_arguments("order-1", leg)
        try:
            result = self.dispatcher.dispatch_leg(
                order_id="order-1", leg_id=leg,
                stored_trade_url="https://steamcommunity.com/tradeoffer/new/?partner=124",
                **freshness,
            )
        finally:
            if freshness.get("playerok_order_lease") is not None:
                self.freshness_gate.release_order(freshness["playerok_order_lease"])
        self.assertEqual(result["status"], "RECIPIENT_MISMATCH")
        self.assertEqual(self.asf.sent, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'").fetchone()[0], 0)

    def test_dispatch_rejects_reservation_mismatch_before_creating_attempt(self):
        leg = self.create_order()[0]
        with self.db.connect() as db:
            db.execute("UPDATE gd_resource_reservations SET state='released'")

        result = self.dispatch( "order-1", leg)

        self.assertFalse(result["dispatched"])
        self.assertIn("reservation", result["reason"])
        self.assertEqual(self.asf.sent, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'"
            ).fetchone()[0], 0)

    def test_dispatch_blocks_non_paid_playerok_statuses_before_asf(self):
        for index, status in enumerate(("SENT", "CONFIRMED", "CANCELLED", "unknown", ""), start=1):
            order_id = f"nonpaid-{index}"
            leg = self.create_order(order_id=order_id, bots=(f"Bot-{index}",))[0]
            with self.db.connect() as db:
                db.execute(
                    "UPDATE gd_deals SET platform_status=? WHERE external_deal_id=?",
                    (status, f"deal-external-{order_id}"),
                )

            result = self.dispatch(order_id, leg)

            self.assertFalse(result["dispatched"], status)
            self.assertEqual(result["status"], "PLAYEROK_NOT_ELIGIBLE")
            self.assertEqual(self.asf.sent, [])
            with self.db.connect() as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id=?", (order_id,)
                ).fetchone()[0], 0)

    def test_paid_local_snapshot_without_fresh_browser_proof_is_blocked_before_asf(self):
        leg = self.create_order()[0]
        result = self.dispatcher.dispatch_leg(
            order_id="order-1", leg_id=leg, stored_trade_url=self.trade_url
        )
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["status"], "PLAYEROK_FRESHNESS_REQUIRED")
        self.assertEqual(self.asf.sent, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute(
                "SELECT status FROM gd_delivery_legs WHERE leg_id=?", (leg,)
            ).fetchone()[0], "reserved")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations "
                "WHERE order_line_id='line-order-1' AND state='active'"
            ).fetchone()[0], 1)

    def test_dispatch_blocks_historical_paid_playerok_deal_before_asf(self):
        leg = self.create_order()[0]
        with self.db.connect() as db:
            db.execute("UPDATE gd_deals SET is_historical=1 WHERE deal_id='deal-order-1'")

        result = self.dispatch("order-1", leg)

        self.assertFalse(result["dispatched"])
        self.assertEqual(result["status"], "PLAYEROK_NOT_ELIGIBLE")
        self.assertIn("Историческая", result["reason"])
        self.assertEqual(self.asf.sent, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'"
            ).fetchone()[0], 0)

    def test_dispatch_claim_serializes_legs_of_the_same_order(self):
        first_leg, second_leg = self.create_order(bots=("Bot-A", "Bot-B"))
        self.ledger.create_delivery_attempt(
            order_id="order-1", leg_id=first_leg, retry_generation=0,
            status="dispatching",
        )
        with self.db.connect() as db:
            db.execute("UPDATE gd_delivery_legs SET status='DISPATCHING' WHERE leg_id=?", (first_leg,))
            db.execute("UPDATE gd_orders SET status='PROCESSING' WHERE order_id='order-1'")

        result = self.dispatch("order-1", second_leg)

        self.assertFalse(result["dispatched"])
        self.assertIn("another leg", result["reason"])
        self.assertEqual(self.asf.sent, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'"
            ).fetchone()[0], 1)

    def test_delivery_leg_amount_manifest_migration_is_applied(self):
        with self.db.connect() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(gd_delivery_legs)")}
            self.assertIn("asset_amounts_json", columns)
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM gd_schema_migrations "
                "WHERE migration_id='2026-11-delivery-leg-asset-amounts-v1'"
            ).fetchone())

    def test_delivery_leg_rejects_incomplete_or_non_positive_amount_manifest(self):
        self.create_order()
        for amounts in ({"100": 3, "101": 1}, {"100": 0}, {"100": True}):
            with self.subTest(amounts=amounts):
                with self.assertRaises(ValueError):
                    self.ledger.add_delivery_leg(
                        leg_id="invalid-amount-manifest",
                        order_line_id="line-order-1", bot_name="Bot-A",
                        app_id=730, context_id=2, asset_ids=["100"],
                        status="reserved", asset_amounts=amounts,
                    )

    def test_dispatch_blocks_invalid_asset_amount_manifest_before_attempt(self):
        leg = self.create_order(asset_amounts={"100": 3})[0]
        with self.db.connect() as db:
            db.execute(
                "UPDATE gd_delivery_legs SET asset_amounts_json=? WHERE leg_id=?",
                ('{"100":0}', leg),
            )

        result = self.dispatch("order-1", leg)

        self.assertFalse(result["dispatched"])
        self.assertEqual(result["reason"], "invalid asset amount manifest")
        self.assertEqual(self.asf.sent, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'"
            ).fetchone()[0], 0)

    def test_dispatch_passes_exact_asset_amount_manifest_to_asf(self):
        leg = self.create_order(asset_amounts={"100": 3})[0]
        self.asf.send_result["Bot-A"] = {"success": True, "offerIds": ["amount-offer"]}

        result = self.dispatch("order-1", leg)

        self.assertTrue(result["dispatched"])
        self.assertEqual(self.asf.sent[0][3], ("100",))
        self.assertEqual(self.asf.sent[0][4], {"100": 3})
        self.assertEqual(self.asf.sent[0][-2:], (730, 2))

    def test_dispatches_and_reconciles_from_shared_quantity_stock_allocation(self):
        leg = self.create_order(asset_amounts={"100": 3})[0]
        with self.db.connect() as db:
            db.execute(
                "UPDATE gd_resource_reservations SET state='released' "
                "WHERE order_line_id='line-order-1' AND asset_id='100'"
            )
        stock = QuantityStockLedger(self.db)
        observation = stock.observe(
            StockResource("cs2", 730, "2", "Bot-A", "100"),
            10,
            identity=StockItemIdentity(
                "cs2", 730, "2",
                market_hash_name="AK-47 | Redline",
                name="AK-47 | Redline",
                item_type="Rifle",
                class_id="1",
            ),
            is_tradable=True,
        )
        with self.db.connect() as db:
            db.execute(
                """
                INSERT INTO gd_stock_allocations
                    (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                     asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                     order_line_id,legacy_owner_id,units,state,recipe_item_id)
                VALUES(?,?,?,?,?,?,?,?,'order',NULL,NULL,?,NULL,3,'active',NULL)
                """,
                (
                    "stock-order-allocation", observation["observation_id"],
                    "cs2", 730, "2", "Bot-A", "100", observation["asset_id_key"],
                    "line-order-1",
                ),
            )
        self.asf.send_result["Bot-A"] = {"success": True, "offerIds": ["stock-offer"]}

        result = self.dispatch("order-1", leg)

        self.assertTrue(result["dispatched"])
        self.assertEqual(self.asf.sent[0][3], ("100",))
        self.assertEqual(self.asf.sent[0][4], {"100": 3})
        self.asf.statuses[("Bot-A", "stock-offer")] = {
            "success": True, "mode": "trade-status", "found": True,
            "tradeOfferId": "stock-offer", "botName": "Bot-A", "status": "Accepted",
        }
        self.reconciler.reconcile(order_id="order-1")
        with self.db.connect() as db:
            allocation = db.execute(
                "SELECT state,consumed_revision FROM gd_stock_allocations "
                "WHERE allocation_id='stock-order-allocation'"
            ).fetchone()
            order_status = db.execute(
                "SELECT status FROM gd_orders WHERE order_id='order-1'"
            ).fetchone()[0]
        self.assertEqual(allocation["state"], "consumed")
        self.assertEqual(int(allocation["consumed_revision"]), int(observation["revision"]))
        self.assertEqual(order_status, "DELIVERED")

    def test_reconciliation_consumes_only_matching_app_context_stock_allocation(self):
        leg = self.create_order(asset_amounts={"100": 3})[0]
        with self.db.connect() as db:
            db.execute(
                "UPDATE gd_resource_reservations SET state='released' "
                "WHERE order_line_id='line-order-1' AND asset_id='100'"
            )

        stock = QuantityStockLedger(self.db)
        identity_context_2 = StockItemIdentity(
            "cs2", 730, "2",
            market_hash_name="AK-47 | Redline",
            name="AK-47 | Redline", item_type="Rifle", class_id="ctx2",
        )
        identity_context_6 = StockItemIdentity(
            "cs2", 730, "6",
            market_hash_name="AK-47 | Redline",
            name="AK-47 | Redline", item_type="Rifle", class_id="ctx6",
        )
        target = stock.observe(
            StockResource("cs2", 730, "2", "Bot-A", "100"),
            10, identity=identity_context_2, is_tradable=True,
        )
        neighbour = stock.observe(
            StockResource("cs2", 730, "6", "Bot-A", "100"),
            10, identity=identity_context_6, is_tradable=True,
        )
        with self.db.connect() as db:
            for allocation_id, observation, context_id, amount in (
                ("stock-target-context", target, "2", 3),
                ("stock-neighbour-context", neighbour, "6", 5),
            ):
                db.execute(
                    """
                    INSERT INTO gd_stock_allocations
                        (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                         asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                         order_line_id,legacy_owner_id,units,state,recipe_item_id)
                    VALUES(?,?,?,?,?,?,?,?,'order',NULL,NULL,?,NULL,?,'active',NULL)
                    """,
                    (
                        allocation_id, observation["observation_id"], "cs2", 730,
                        context_id, "Bot-A", "100", observation["asset_id_key"],
                        "line-order-1", amount,
                    ),
                )

        self.asf.send_result["Bot-A"] = {
            "success": True, "offerIds": ["context-scoped-offer"],
        }
        result = self.dispatch("order-1", leg)
        self.assertTrue(result["dispatched"])
        self.asf.statuses[("Bot-A", "context-scoped-offer")] = {
            "success": True, "mode": "trade-status", "found": True,
            "tradeOfferId": "context-scoped-offer", "botName": "Bot-A", "status": "Accepted",
        }
        self.reconciler.reconcile(order_id="order-1")

        with self.db.connect() as db:
            states = {
                row["allocation_id"]: row["state"]
                for row in db.execute(
                    "SELECT allocation_id,state FROM gd_stock_allocations "
                    "WHERE allocation_id IN ('stock-target-context','stock-neighbour-context')"
                ).fetchall()
            }
        self.assertEqual(states, {
            "stock-target-context": "consumed",
            "stock-neighbour-context": "active",
        })

    def test_dispatch_blocks_manifest_reservation_amount_mismatch_before_attempt(self):
        leg = self.create_order(asset_amounts={"100": 3})[0]
        with self.db.connect() as db:
            db.execute(
                "UPDATE gd_resource_reservations SET units=2 WHERE order_line_id='line-order-1'"
            )

        result = self.dispatch("order-1", leg)

        self.assertFalse(result["dispatched"])
        self.assertEqual(result["status"], "reserved")
        self.assertEqual(result["reason"], "active reservation amount mismatch")
        self.assertEqual(self.asf.sent, [])
        with self.db.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_attempts WHERE order_id='order-1'"
            ).fetchone()[0], 0)

    def test_multi_bot_order_delivers_only_after_both_offers_are_accepted(self):
        legs = self.create_order(bots=("Bot-A", "Bot-B"))
        self.asf.send_result = {
            "Bot-A": {"success": True, "offerIds": ["91001"]},
            "Bot-B": {"success": True, "offerIds": ["91002"]},
        }
        self.dispatch("order-1", legs[0])
        self.dispatch("order-1", legs[1])
        self.asf.statuses[("Bot-A", "91001")] = {
            "success": True, "mode": "trade-status", "found": True,
            "tradeOfferId": "91001", "botName": "Bot-A", "status": "Accepted",
        }
        self.asf.statuses[("Bot-B", "91002")] = {
            "success": True, "mode": "trade-status", "found": True,
            "tradeOfferId": "91002", "botName": "Bot-B", "status": "Active",
        }
        self.reconciler.reconcile(order_id="order-1")
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0], "PROCESSING")
        self.asf.statuses[("Bot-B", "91002")]["status"] = "Accepted"
        self.reconciler.reconcile(order_id="order-1")
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0], "DELIVERED")
            reservation_states = {
                row["state"] for row in db.execute(
                    "SELECT state FROM gd_resource_reservations WHERE order_line_id='line-order-1'"
                ).fetchall()
            }
            self.assertEqual(reservation_states, {"consumed"})

    def test_stale_declined_reconciliation_cannot_overwrite_concurrent_acceptance(self):
        leg = self.create_order()[0]
        self.asf.send_result["Bot-A"] = {"success": True, "offerIds": ["95001"]}
        self.assertEqual(self.dispatch("order-1", leg)["status"], "SENT_TO_STEAM")

        def stale_declined_status(bot_name, offer_id):
            # Simulate another reconciliation worker committing ACCEPTED after
            # this worker loaded SENT_TO_STEAM but before its stale response.
            self.assertTrue(self.reconciler._update_leg(
                leg, "ACCEPTED", trade_offer_id=offer_id
            ))
            return {
                "success": True, "mode": "trade-status", "found": True,
                "tradeOfferId": offer_id, "botName": bot_name, "status": "Declined",
            }

        self.reconciler._status = stale_declined_status
        result = self.reconciler.reconcile(order_id="order-1")

        self.assertEqual(result[0]["status"], "STATE_CHANGED")
        self.assertFalse(result[0]["reconciled"])
        with self.db.connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM gd_delivery_legs WHERE leg_id=?", (leg,)).fetchone()[0],
                "ACCEPTED",
            )
            self.assertEqual(
                db.execute("SELECT status FROM gd_delivery_attempts WHERE leg_id=?", (leg,)).fetchone()[0],
                "accepted",
            )
            self.assertEqual(
                db.execute("SELECT state FROM gd_resource_reservations WHERE order_line_id='line-order-1'").fetchone()[0],
                "consumed",
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM gd_delivery_events "
                    "WHERE order_id='order-1' AND event_type='telegram_notification'"
                ).fetchone()[0],
                0,
            )

    def test_reconciliation_preserves_privacy_wait_when_another_leg_is_accepted(self):
        legs = self.create_order(bots=("Bot-A", "Bot-B"))
        self.asf.send_result["Bot-B"] = {"success": True, "offerIds": ["94002"]}
        self.assertEqual(self.dispatch("order-1", legs[1])["status"], "SENT_TO_STEAM")
        self.asf.send_result["Bot-A"] = {
            "success": False, "resultUnknown": False,
            "error": "Recipient inventory is private",
        }
        self.assertEqual(self.dispatch("order-1", legs[0])["status"], "PRIVACY_WAIT")
        self.asf.statuses[("Bot-B", "94002")] = {
            "success": True, "mode": "trade-status", "found": True,
            "tradeOfferId": "94002", "botName": "Bot-B", "status": "Accepted",
        }

        result = self.reconciler.reconcile(order_id="order-1")

        self.assertEqual(result[0]["status"], "ACCEPTED")
        with self.db.connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0],
                "PRIVACY_WAIT",
            )
            self.assertEqual(
                db.execute("SELECT status FROM gd_delivery_legs WHERE leg_id=?", (legs[0],)).fetchone()[0],
                "PRIVACY_WAIT",
            )
            self.assertEqual(
                db.execute("SELECT state FROM gd_resource_reservations WHERE bot_name='Bot-A'").fetchone()[0],
                "active",
            )
            self.assertEqual(
                db.execute("SELECT state FROM gd_resource_reservations WHERE bot_name='Bot-B'").fetchone()[0],
                "consumed",
            )

    def test_privacy_error_holds_reservation_and_queues_buyer_instruction(self):
        leg = self.create_order()[0]
        self.asf.send_result["Bot-A"] = {
            "success": False, "resultUnknown": False,
            "error": "Recipient inventory is private",
        }
        result = self.dispatch("order-1", leg)
        self.assertEqual(result["status"], "PRIVACY_WAIT")
        with self.db.connect() as db:
            order = db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0]
            event = db.execute(
                "SELECT payload_json FROM gd_delivery_events WHERE order_id='order-1' AND event_type='buyer_privacy_instruction'"
            ).fetchone()
        self.assertEqual(order, "PRIVACY_WAIT")
        self.assertIn("!настроил приватность", event["payload_json"])

    def test_trade_restriction_queues_buyer_and_telegram_events_without_retry(self):
        leg = self.create_order()[0]
        self.asf.send_result["Bot-A"] = {
            "success": False, "resultUnknown": False,
            "error": "Recipient has a trade hold; Steam Guard required",
        }
        result = self.dispatch("order-1", leg)
        self.assertEqual(result["status"], "TRADE_RESTRICTED")
        with self.db.connect() as db:
            order = db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0]
            kinds = {row[0] for row in db.execute(
                "SELECT event_type FROM gd_delivery_events WHERE order_id='order-1'"
            ).fetchall()}
        self.assertEqual(order, "TRADE_RESTRICTED")
        self.assertIn("buyer_trade_restricted", kinds)
        self.assertIn("telegram_notification", kinds)
        again = self.dispatch("order-1", leg)
        self.assertFalse(again["dispatched"])

    def test_declined_offer_triggers_inventory_refresh_and_telegram_event(self):
        leg = self.create_order()[0]
        self.asf.send_result["Bot-A"] = {"success": True, "offerIds": ["92001"]}
        self.dispatch("order-1", leg)
        self.asf.statuses[("Bot-A", "92001")] = {
            "success": True, "mode": "trade-status", "found": True,
            "tradeOfferId": "92001", "botName": "Bot-A", "status": "Declined",
        }
        result = self.reconciler.reconcile(order_id="order-1")
        self.assertEqual(result[0]["status"], "DECLINED")
        self.assertEqual(self.asf.inventory_refreshes, [("Bot-A", 730, 2)])
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM gd_delivery_legs WHERE leg_id=?", (leg,)).fetchone()[0], "DECLINED")
            self.assertEqual(db.execute("SELECT status FROM gd_delivery_attempts WHERE leg_id=?", (leg,)).fetchone()[0], "declined")
            self.assertEqual(db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0], "ACTION_REQUIRED")
            self.assertEqual(db.execute("SELECT state FROM gd_resource_reservations WHERE order_line_id='line-order-1'").fetchone()[0], "active")
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM gd_delivery_events WHERE order_id='order-1' AND event_type='telegram_notification'"
            ).fetchone())


    def test_proven_failure_requires_operator_action_and_blocks_unstarted_legs(self):
        legs = self.create_order(bots=("Bot-A", "Bot-B"))
        self.asf.send_result["Bot-A"] = {
            "success": False, "resultUnknown": False, "error": "invalid asset manifest",
        }
        result = self.dispatch("order-1", legs[0])
        self.assertEqual(result["status"], "FAILED")
        with self.db.connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0],
                "ACTION_REQUIRED",
            )
            self.assertEqual(
                db.execute("SELECT status FROM gd_delivery_attempts WHERE leg_id=?", (legs[0],)).fetchone()[0],
                "failed",
            )
        sent_before_blocked_dispatch = len(self.asf.sent)
        blocked = self.dispatch("order-1", legs[1])
        self.assertFalse(blocked["dispatched"])
        self.assertEqual(blocked["status"], "ACTION_REQUIRED")
        self.assertEqual(len(self.asf.sent), sent_before_blocked_dispatch)

    def test_sent_offer_continues_reconciliation_after_another_leg_fails(self):
        legs = self.create_order(bots=("Bot-A", "Bot-B"))
        self.asf.send_result["Bot-B"] = {"success": True, "offerIds": ["93002"]}
        self.assertEqual(self.dispatch("order-1", legs[1])["status"], "SENT_TO_STEAM")
        self.asf.send_result["Bot-A"] = {
            "success": False, "resultUnknown": False, "error": "invalid asset manifest",
        }
        self.assertEqual(self.dispatch("order-1", legs[0])["status"], "FAILED")
        with self.db.connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0],
                "PROCESSING",
            )
        self.asf.statuses[("Bot-B", "93002")] = {
            "success": True, "mode": "trade-status", "found": True,
            "tradeOfferId": "93002", "botName": "Bot-B", "status": "Accepted",
        }
        reconciled = self.reconciler.reconcile(order_id="order-1")
        self.assertEqual(reconciled[0]["status"], "ACCEPTED")
        with self.db.connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0],
                "ACTION_REQUIRED",
            )
            self.assertEqual(
                db.execute("SELECT status FROM gd_delivery_attempts WHERE leg_id=?", (legs[1],)).fetchone()[0],
                "accepted",
            )



    def test_expired_operator_retry_proof_is_rejected_before_asf_boundary(self):
        leg = self.create_order()[0]
        with self.db.connect() as db:
            self.ledger.create_delivery_attempt(
                order_id="order-1", leg_id=leg, retry_generation=1,
                status="prepared",
                result={
                    "operator_retry_action_id": "action-expired",
                    "recovery_evidence": {"inventory": {"expires_at": "2020-01-01T00:00:00+00:00"}},
                },
                connection=db,
            )
            db.execute("UPDATE gd_orders SET status='PROCESSING' WHERE order_id='order-1'")
        sent_before = len(self.asf.sent)
        result = self.dispatch("order-1", leg, retry_generation=1)
        self.assertEqual(result["status"], "RECOVERY_PROOF_EXPIRED")
        self.assertEqual(len(self.asf.sent), sent_before)
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM gd_delivery_legs WHERE leg_id=?", (leg,)).fetchone()[0], "FAILED")
            self.assertEqual(db.execute("SELECT status FROM gd_orders WHERE order_id='order-1'").fetchone()[0], "ACTION_REQUIRED")
            self.assertEqual(db.execute("SELECT status FROM gd_delivery_attempts WHERE retry_generation=1").fetchone()[0], "failed")



if __name__ == "__main__":
    unittest.main()
