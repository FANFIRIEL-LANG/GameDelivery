from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.order_pipeline import DeliveryOrderPipeline
from core.delivery.playerok_eligibility import (
    PlayerokDeliveryBlocked,
    create_playerok_order_for_preparation,
    load_playerok_paid_preparation,
)
from core.delivery.recipes import RecipeItem, SteamAssetSpec, SteamCardSetSpec
from core.delivery.reservation_service import InventorySnapshot
from core.delivery.stock_identity import StockItemIdentity
from core.delivery.stock_ledger import QuantityStockLedger, StockResource
from games.cs2.inventory_view import CS2InventoryInstance, RestrictionStatus, RestrictionType
from games.steam_cards.recipient_badge import RecipientBadge
from games.steam_cards.selection import CardCopy
from integrations.playerok.listing_config import PlayerokListingConfigService
from integrations.playerok.sync import PlayerokDealSyncService
from tests.test_playerok_sync import FakePlayerok, deal


class PlayerokListingStockHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "playerok-listing-stock.sqlite3")
        self.database.initialize()
        self.ledger = DeliveryLedger(self.database)
        self.syncer = PlayerokDealSyncService(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_paid_recipe_without_resources_commits_only_blocked_order_and_manifest(self):
        item = RecipeItem(
            item_id="required-asset", position=1, kind="steam_asset", spec_version=1,
            quantity_per_order=1, required=True, stock_source_id="asf-stock",
            executor_key="steam_trade_offer",
            spec=SteamAssetSpec(
                "cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",)
            ),
        )
        config = self.ledger.create_recipe_config_version(
            listing_id="typed-listing-blocked", source_platform="playerok",
            external_listing_id="listing-blocked", recipe_items=[item], title="Blocked listing",
        )
        adapter = FakePlayerok(baseline=[], active=[deal("blocked-paid", "listing-blocked")])
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        bound = self.syncer.bind_paid_deal_config_version(
            account_user_id="seller-account-1", external_deal_id="blocked-paid",
            config_version_id=config["config_version_id"], operator_id="desktop-operator",
            reason="Проверена версия объявления на момент оплаты",
        )

        result = DeliveryOrderPipeline(self.database).prepare_paid_recipe_deal(
            account_user_id="seller-account-1", external_deal_id="blocked-paid", snapshots={},
        )
        self.assertEqual(result.status, "preflight_blocked")
        self.assertEqual(result.order_id, bound["order_id"])
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT status,total_quantity FROM gd_orders WHERE order_id=?", (result.order_id,)
            ).fetchone()[:], ("preflight_blocked", 1))
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_order_lines WHERE order_id=?", (result.order_id,)
            ).fetchone()[0], 1)
            for table in ("gd_delivery_legs", "gd_resource_reservations"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

        repeated = DeliveryOrderPipeline(self.database).prepare_paid_recipe_deal(
            account_user_id="seller-account-1", external_deal_id="blocked-paid", snapshots={},
        )
        self.assertEqual(repeated.order_id, result.order_id)
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_orders WHERE deal_id=(SELECT deal_id FROM gd_deals "
                "WHERE external_deal_id='blocked-paid')"
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_order_lines WHERE order_id=?", (result.order_id,)
            ).fetchone()[0], 1)

    def test_paid_playerok_recipe_transfers_listing_stock_only_with_reservation_commit(self):
        recipe_item = RecipeItem(
            "cs2-redline", 1, "steam_asset", 1, 2, True, "shared-asf-stock",
            "steam_trade_offer",
            SteamAssetSpec(
                "cs2", 730, "2",
                {"market_hash_name": "AK-47 | Redline"},
                (),
            ),
        )
        config = self.ledger.create_recipe_config_version(
            listing_id="typed-listing-live",
            source_platform="playerok",
            external_listing_id="listing-live",
            recipe_items=[recipe_item],
            title="Typed CS2 listing",
        )
        stock = QuantityStockLedger(self.database)
        resource = StockResource("cs2", 730, "2", "Bot-A", "stock-asset-1")
        stock.observe(
            resource,
            6,
            identity=StockItemIdentity(
                "cs2", 730, "2",
                market_hash_name="AK-47 | Redline",
                name="AK-47 | Redline",
                item_type="Rifle",
                class_id="redline-class",
            ),
            is_tradable=True,
        )
        allocated = PlayerokListingConfigService(self.database).allocate_listing_stock(
            config_version_id=config["config_version_id"],
            listing_quantity=3,
        )
        self.assertEqual(allocated["reserved_units"], 6)
        alternate = self.ledger.create_recipe_config_version(
            listing_id=config["listing_id"], source_platform="playerok",
            external_listing_id="listing-live", recipe_items=[recipe_item],
            title="Later version, not bound to this deal",
        )

        adapter = FakePlayerok(
            baseline=[],
            active=[deal("stock-transfer-paid", "listing-live")],
        )
        self.syncer.sync(adapter)  # Complete the historical baseline.
        self.syncer.sync(adapter)  # Persist the first post-baseline PAID deal.

        # A PAID event alone does not prove which ConfigVersion was active at
        # payment time. Keep the production fail-closed behavior: no order is
        # created until that exact version has been explicitly bound.
        with self.database.connect() as db:
            deal_row = db.execute(
                "SELECT deal_id FROM gd_deals WHERE source_platform='playerok' "
                "AND external_deal_id=?",
                ("stock-transfer-paid",),
            ).fetchone()
            self.assertIsNotNone(deal_row)
            normalized_deal_id = str(deal_row["deal_id"])
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM gd_orders WHERE deal_id=?",
                           (normalized_deal_id,)).fetchone()[0],
                0,
            )

            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM gd_order_lines WHERE order_id IN "
                           "(SELECT order_id FROM gd_orders WHERE deal_id=?)", (normalized_deal_id,)).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM gd_stock_transfers WHERE order_line_id IN "
                           "(SELECT order_line_id FROM gd_order_lines WHERE order_id IN "
                           "(SELECT order_id FROM gd_orders WHERE deal_id=?))", (normalized_deal_id,)).fetchone()[0],
                0,
            )

        bound = self.syncer.bind_paid_deal_config_version(
            account_user_id="seller-account-1", external_deal_id="stock-transfer-paid",
            config_version_id=config["config_version_id"], operator_id="desktop-operator",
            reason="Оператор подтвердил ConfigVersion на момент оплаты",
        )
        order_id = bound["order_id"]
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_orders WHERE deal_id=?", (normalized_deal_id,)
            ).fetchone()[0], 0)
            context = load_playerok_paid_preparation(
                db, "seller-account-1", "stock-transfer-paid"
            )
            db.execute(
                "UPDATE gd_deals SET config_version_id=? WHERE deal_id=?",
                (alternate["config_version_id"], normalized_deal_id),
            )
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(PlayerokDeliveryBlocked):
                create_playerok_order_for_preparation(db, context, 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_orders WHERE deal_id=?", (normalized_deal_id,)
            ).fetchone()[0], 0)
            db.execute("ROLLBACK")
            db.execute(
                "UPDATE gd_deals SET config_version_id=? WHERE deal_id=?",
                (config["config_version_id"], normalized_deal_id),
            )
            db.commit()
            context = load_playerok_paid_preparation(
                db, "seller-account-1", "stock-transfer-paid"
            )
            db.execute("UPDATE gd_deals SET platform_status='SENT' WHERE deal_id=?", (normalized_deal_id,))
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(PlayerokDeliveryBlocked):
                create_playerok_order_for_preparation(db, context, 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_orders WHERE deal_id=?", (normalized_deal_id,)
            ).fetchone()[0], 0)
            db.execute("ROLLBACK")
            db.execute("UPDATE gd_deals SET platform_status='PAID' WHERE deal_id=?", (normalized_deal_id,))

        pipeline = DeliveryOrderPipeline(self.database)

        # Fail after each preceding write boundary. Every attempt starts with a
        # new Order and must leave the ListingStock allocation untouched.
        for trigger_name, table_name, error_message in (
            ("forced_orderline_failure", "gd_order_lines", "orderline failure"),
            ("forced_stock_transfer_failure", "gd_stock_transfers", "stock transfer failure"),
            ("forced_delivery_leg_failure", "gd_delivery_legs", "delivery leg failure"),
        ):
            with self.database.connect() as db:
                db.execute(f"""
                    CREATE TRIGGER {trigger_name}
                    BEFORE INSERT ON {table_name}
                    BEGIN
                        SELECT RAISE(ABORT, '{error_message}');
                    END
                """)
            with self.assertRaisesRegex(sqlite3.IntegrityError, error_message):
                pipeline.prepare_paid_recipe_deal(
                    account_user_id="seller-account-1", external_deal_id="stock-transfer-paid",
                    snapshots={},
                )

            with self.database.connect() as db:
                for table in (
                    "gd_orders", "gd_order_lines", "gd_stock_transfers",
                    "gd_delivery_legs", "gd_resource_reservations",
                ):
                    self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM gd_stock_allocations "
                    "WHERE owner_type='listing' AND config_version_id=? AND state='active'",
                    (config["config_version_id"],),
                ).fetchone()[0], 1)
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM gd_stock_allocations WHERE owner_type='order'"
                ).fetchone()[0], 0)
                db.execute(f"DROP TRIGGER {trigger_name}")

        with ThreadPoolExecutor(max_workers=2) as workers:
            prepared_results = list(workers.map(
                lambda _index: pipeline.prepare_paid_recipe_deal(
                    account_user_id="seller-account-1",
                    external_deal_id="stock-transfer-paid", snapshots={},
                ),
                range(2),
            ))
        prepared = prepared_results[0]
        self.assertEqual([result.status for result in prepared_results], ["reserved", "reserved"])

        with self.database.connect() as db:
            line = db.execute(
                "SELECT order_line_id FROM gd_order_lines WHERE order_id=?",
                (order_id,),
            ).fetchone()
            self.assertIsNotNone(line)
            self.assertIsNotNone(line)
            transfer = db.execute(
                "SELECT units,listing_id,config_version_id,order_line_id "
                "FROM gd_stock_transfers WHERE order_line_id=?",
                (line["order_line_id"],),
            ).fetchone()
            self.assertIsNotNone(transfer)
            self.assertEqual(
                (transfer["units"], transfer["listing_id"], transfer["config_version_id"]),
                (2, config["listing_id"], config["config_version_id"]),
            )
            self.assertEqual(transfer["order_line_id"], line["order_line_id"])

            allocations = db.execute(
                "SELECT owner_type,state,units FROM gd_stock_allocations "
                "WHERE owner_type='listing' AND config_version_id=? "
                "UNION ALL "
                "SELECT owner_type,state,units FROM gd_stock_allocations "
                "WHERE owner_type='order' AND order_line_id=? "
                "ORDER BY owner_type",
                (config["config_version_id"], line["order_line_id"]),
            ).fetchall()
            self.assertEqual(
                sorted((row["owner_type"], row["state"], row["units"]) for row in allocations),
                [("listing", "active", 4), ("listing", "released", 6), ("order", "active", 2)],
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM gd_stock_transfers WHERE order_line_id=?",
                    (line["order_line_id"],),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM gd_delivery_legs l "
                    "JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id "
                    "WHERE ol.order_id=?",
                    (order_id,),
                ).fetchone()[0],
                1,
            )

        # Re-running preparation for the already-reserved order must not transfer
        # the same listing units a second time or append another transfer event.
        repeated = pipeline.prepare_paid_recipe_deal(
            account_user_id="seller-account-1", external_deal_id="stock-transfer-paid",
            snapshots={},
        )
        self.assertEqual(repeated.status, "reserved")
        with self.database.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM gd_stock_transfers WHERE order_line_id=("
                    "SELECT order_line_id FROM gd_order_lines WHERE order_id=?)",
                    (order_id,),
                ).fetchone()[0],
                1,
            )

    def test_paid_recipe_reservation_insert_failure_rolls_back_new_order_and_leg(self):
        now = datetime.now(timezone.utc)
        item = RecipeItem(
            item_id="resource-item", position=1, kind="steam_asset", spec_version=1,
            quantity_per_order=1, required=True, stock_source_id="asf-inventory",
            executor_key="steam_trade_offer",
            spec=SteamAssetSpec(
                "cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",)
            ),
        )
        config = self.ledger.create_recipe_config_version(
            listing_id="typed-listing-resource-failure", source_platform="playerok",
            external_listing_id="listing-resource-failure", recipe_items=[item],
            title="Resource failure",
        )
        adapter = FakePlayerok(
            baseline=[], active=[deal("resource-failure-paid", "listing-resource-failure")]
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        bound = self.syncer.bind_paid_deal_config_version(
            account_user_id="seller-account-1", external_deal_id="resource-failure-paid",
            config_version_id=config["config_version_id"], operator_id="desktop-operator",
            reason="Проверена версия объявления на момент оплаты",
        )
        resource = CS2InventoryInstance(
            game_id="cs2", appid=730, context_id=2, bot_name="Bot-A",
            asset_id="inventory-asset-1", class_id="redline-class", instance_id="0",
            amount=1, name="AK-47 | Redline", item_type="Rifle",
            market_hash_name="AK-47 | Redline", is_tradable=True, is_marketable=True,
            restriction_type=RestrictionType.NONE,
            restriction_status=RestrictionStatus.AVAILABLE,
            last_inventory_refresh=now,
        )
        snapshots = {("Bot-A", 730, "2"): InventorySnapshot(
            "Bot-A", "cs2", 730, "2", now, (resource,), True,
        )}
        with self.database.connect() as db:
            db.execute("""
                CREATE TRIGGER forced_resource_reservation_failure
                BEFORE INSERT ON gd_resource_reservations
                BEGIN
                    SELECT RAISE(ABORT, 'forced resource reservation failure');
                END
            """)
        pipeline = DeliveryOrderPipeline(self.database)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced resource reservation failure"):
            pipeline.prepare_paid_recipe_deal(
                account_user_id="seller-account-1", external_deal_id="resource-failure-paid",
                snapshots=snapshots, now=now,
            )
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_orders WHERE order_id=?", (bound["order_id"],)
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_order_lines WHERE order_id=?", (bound["order_id"],)
            ).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_delivery_legs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_resource_reservations").fetchone()[0], 0)
            db.execute("DROP TRIGGER forced_resource_reservation_failure")

    def test_paid_recipe_conflict_after_planning_persists_only_blocked_manifest(self):
        now = datetime.now(timezone.utc)
        item = RecipeItem(
            item_id="racing-resource", position=1, kind="steam_asset", spec_version=1,
            quantity_per_order=1, required=True, stock_source_id="asf-inventory",
            executor_key="steam_trade_offer",
            spec=SteamAssetSpec(
                "cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",)
            ),
        )
        config = self.ledger.create_recipe_config_version(
            listing_id="typed-listing-race", source_platform="playerok",
            external_listing_id="listing-race", recipe_items=[item], title="Reservation race",
        )
        adapter = FakePlayerok(baseline=[], active=[deal("race-paid", "listing-race")])
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        bound = self.syncer.bind_paid_deal_config_version(
            account_user_id="seller-account-1", external_deal_id="race-paid",
            config_version_id=config["config_version_id"], operator_id="desktop-operator",
            reason="Проверена версия объявления на момент оплаты",
        )
        resource = CS2InventoryInstance(
            game_id="cs2", appid=730, context_id=2, bot_name="Bot-A",
            asset_id="racing-asset", class_id="redline-class", instance_id="0", amount=1,
            name="AK-47 | Redline", item_type="Rifle", market_hash_name="AK-47 | Redline",
            is_tradable=True, is_marketable=True, restriction_type=RestrictionType.NONE,
            restriction_status=RestrictionStatus.AVAILABLE, last_inventory_refresh=now,
        )
        snapshots = {("Bot-A", 730, "2"): InventorySnapshot(
            "Bot-A", "cs2", 730, "2", now, (resource,), True,
        )}
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO gd_resource_reservations "
                "(reservation_id,owner_type,listing_id,config_version_id,game_id,app_id,context_id,"
                "bot_name,asset_id,units,state) VALUES(?,?,?,?,?,?,?,?,?,1,'active')",
                ("racing-claim", "listing", config["listing_id"], config["config_version_id"],
                 "cs2", 730, "2", "Bot-A", "racing-asset"),
            )
        pipeline = DeliveryOrderPipeline(self.database)
        # Simulate another owner claiming the resource after pure planning but
        # before the reservation transaction's authoritative conflict check.
        with patch.object(pipeline.card_reservations, "list_reserved_assets", return_value=()):
            result = pipeline.prepare_paid_recipe_deal(
                account_user_id="seller-account-1", external_deal_id="race-paid",
                snapshots=snapshots, now=now,
            )
        self.assertEqual(result.status, "preflight_blocked")
        self.assertEqual(result.order_id, bound["order_id"])
        with self.database.connect() as db:
            order = db.execute(
                "SELECT status FROM gd_orders WHERE order_id=?", (bound["order_id"],)
            ).fetchone()
            self.assertEqual(order["status"], "preflight_blocked")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_order_lines WHERE order_id=?", (bound["order_id"],)
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_legs"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE owner_type='order'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE reservation_id='racing-claim'"
            ).fetchone()[0], 1)

    def test_paid_status_change_after_planning_prevents_order_commit(self):
        now = datetime.now(timezone.utc)
        item = RecipeItem(
            item_id="status-race-resource", position=1, kind="steam_asset", spec_version=1,
            quantity_per_order=1, required=True, stock_source_id="asf-inventory",
            executor_key="steam_trade_offer",
            spec=SteamAssetSpec(
                "cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",)
            ),
        )
        config = self.ledger.create_recipe_config_version(
            listing_id="typed-listing-status-race", source_platform="playerok",
            external_listing_id="listing-status-race", recipe_items=[item], title="Status race",
        )
        adapter = FakePlayerok(
            baseline=[], active=[deal("status-race-paid", "listing-status-race")]
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        bound = self.syncer.bind_paid_deal_config_version(
            account_user_id="seller-account-1", external_deal_id="status-race-paid",
            config_version_id=config["config_version_id"], operator_id="desktop-operator",
            reason="Проверена версия объявления на момент оплаты",
        )
        resource = CS2InventoryInstance(
            game_id="cs2", appid=730, context_id=2, bot_name="Bot-A",
            asset_id="status-race-asset", class_id="redline-class", instance_id="0", amount=1,
            name="AK-47 | Redline", item_type="Rifle", market_hash_name="AK-47 | Redline",
            is_tradable=True, is_marketable=True, restriction_type=RestrictionType.NONE,
            restriction_status=RestrictionStatus.AVAILABLE, last_inventory_refresh=now,
        )
        snapshots = {("Bot-A", 730, "2"): InventorySnapshot(
            "Bot-A", "cs2", 730, "2", now, (resource,), True,
        )}
        pipeline = DeliveryOrderPipeline(self.database)
        commit = pipeline.cs2_reservations.reserve_recipe_plan

        def change_status_then_commit(**kwargs):
            with self.database.connect() as db:
                db.execute(
                    "UPDATE gd_deals SET platform_status='SENT' WHERE external_deal_id=?",
                    ("status-race-paid",),
                )
            return commit(**kwargs)

        with patch.object(
            pipeline.cs2_reservations, "reserve_recipe_plan", side_effect=change_status_then_commit
        ), self.assertRaises(PlayerokDeliveryBlocked):
            pipeline.prepare_paid_recipe_deal(
                account_user_id="seller-account-1", external_deal_id="status-race-paid",
                snapshots=snapshots, now=now,
            )
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_orders WHERE order_id=?", (bound["order_id"],)
            ).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_delivery_legs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_resource_reservations").fetchone()[0], 0)

    def test_paid_steam_card_quota_race_keeps_only_blocked_manifest(self):
        item = RecipeItem(
            item_id="quota-card-set", position=1, kind="steam_card_set", spec_version=1,
            quantity_per_order=1, required=True, stock_source_id=None,
            executor_key="steam_trade_offer",
            spec=SteamCardSetSpec(game_name="Dota 2", allowed_bot_pool=("Bot-A",)),
        )
        config = self.ledger.create_recipe_config_version(
            listing_id="typed-listing-quota-race", source_platform="playerok",
            external_listing_id="listing-quota-race", recipe_items=[item], title="Quota race",
        )
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO gd_steam_card_listing_limits(listing_id,total_sets) VALUES(?,1)",
                (config["listing_id"],),
            )
        adapter = FakePlayerok(baseline=[], active=[deal("quota-paid", "listing-quota-race")])
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        bound = self.syncer.bind_paid_deal_config_version(
            account_user_id="seller-account-1", external_deal_id="quota-paid",
            config_version_id=config["config_version_id"], operator_id="desktop-operator",
            reason="Проверена версия объявления на момент оплаты",
        )
        # A different order consumes the only listing-wide set slot after
        # this Deal is bound, so the reservation transaction must block cleanly.
        self.ledger.create_deal(
            deal_id="quota-owner-deal", source_platform="playerok",
            external_deal_id="quota-owner-deal", chat_id="quota-owner-chat",
            listing_id=config["listing_id"], config_version_id=config["config_version_id"],
            platform_status="PAID",
        )
        self.ledger.create_order(
            order_id="quota-owner-order", deal_id="quota-owner-deal", total_quantity=1,
        )
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO gd_steam_card_listing_claims "
                "(order_id,listing_id,config_version_id,sets,state) VALUES(?,?,?,1,'reserved')",
                ("quota-owner-order", config["listing_id"], config["config_version_id"]),
            )
        cards = (
            CardCopy("quota-alpha", "Dota 2", "Dota Alpha", market_hash_name="Dota Alpha", game_app_id=570),
            CardCopy("quota-beta", "Dota 2", "Dota Beta", market_hash_name="Dota Beta", game_app_id=570),
        )
        snapshots = {("Bot-A", 753, "6"): InventorySnapshot(
            "Bot-A", "steam_cards", 753, "6", datetime.now(timezone.utc), cards, True,
        )}
        result = DeliveryOrderPipeline(self.database).prepare_paid_recipe_deal(
            account_user_id="seller-account-1", external_deal_id="quota-paid",
            snapshots=snapshots, recipient_badges=(RecipientBadge("Dota 2", 4),),
            badge_data_verified=True, buyer_complete_sets_by_app_id={570: 0},
            buyer_inventory_verified=True, seller_inventory_verified=True,
            card_hashes_by_app_id={570: ("Dota Alpha", "Dota Beta")},
            card_definitions_verified=True,
        )
        self.assertEqual(result.status, "preflight_blocked")
        self.assertEqual(result.order_id, bound["order_id"])
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT status FROM gd_orders WHERE order_id=?", (bound["order_id"],)
            ).fetchone()["status"], "preflight_blocked")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_order_lines WHERE order_id=?", (bound["order_id"],)
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_legs WHERE order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (bound["order_id"],)
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (bound["order_id"],)
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_steam_card_listing_claims WHERE order_id=?",
                (bound["order_id"],),
            ).fetchone()[0], 0)

    def test_paid_listing_stock_shortage_after_planning_keeps_only_blocked_manifest(self):
        item = RecipeItem(
            item_id="stock-race-resource", position=1, kind="steam_asset", spec_version=1,
            quantity_per_order=1, required=True, stock_source_id="shared-asf-stock",
            executor_key="steam_trade_offer",
            spec=SteamAssetSpec(
                "cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, (),
            ),
        )
        config = self.ledger.create_recipe_config_version(
            listing_id="typed-listing-stock-race", source_platform="playerok",
            external_listing_id="listing-stock-race", recipe_items=[item], title="Stock race",
        )
        stock = QuantityStockLedger(self.database)
        resource = StockResource("cs2", 730, "2", "Bot-A", "stock-race-asset")
        stock.observe(
            resource, 2,
            identity=StockItemIdentity(
                "cs2", 730, "2", market_hash_name="AK-47 | Redline",
                name="AK-47 | Redline", item_type="Rifle", class_id="redline-class",
            ),
            is_tradable=True,
        )
        PlayerokListingConfigService(self.database).allocate_listing_stock(
            config_version_id=config["config_version_id"], listing_quantity=1,
        )
        adapter = FakePlayerok(
            baseline=[], active=[deal("stock-race-paid", "listing-stock-race")]
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        bound = self.syncer.bind_paid_deal_config_version(
            account_user_id="seller-account-1", external_deal_id="stock-race-paid",
            config_version_id=config["config_version_id"], operator_id="desktop-operator",
            reason="Проверена версия объявления на момент оплаты",
        )
        pipeline = DeliveryOrderPipeline(self.database)
        commit = pipeline.cs2_reservations.reserve_recipe_plan

        def drain_stock_then_commit(**kwargs):
            with self.database.connect() as db:
                db.execute(
                    "UPDATE gd_stock_allocations SET state='released' "
                    "WHERE owner_type='listing' AND listing_id=? AND config_version_id=? AND state='active'",
                    (config["listing_id"], config["config_version_id"]),
                )
            return commit(**kwargs)

        with patch.object(
            pipeline.cs2_reservations, "reserve_recipe_plan", side_effect=drain_stock_then_commit
        ):
            result = pipeline.prepare_paid_recipe_deal(
                account_user_id="seller-account-1", external_deal_id="stock-race-paid", snapshots={},
            )
        self.assertEqual(result.status, "preflight_blocked")
        self.assertEqual(result.order_id, bound["order_id"])
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT status FROM gd_orders WHERE order_id=?", (bound["order_id"],)
            ).fetchone()["status"], "preflight_blocked")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_order_lines WHERE order_id=?", (bound["order_id"],)
            ).fetchone()[0], 1)
            for table in ("gd_stock_transfers", "gd_delivery_legs", "gd_resource_reservations"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_stock_allocations WHERE owner_type='order'"
            ).fetchone()[0], 0)

    def test_paid_steam_card_recipe_commits_complete_set_and_listing_quota_atomically(self):
        item = RecipeItem(
            item_id="paid-card-set", position=1, kind="steam_card_set", spec_version=1,
            quantity_per_order=1, required=True, stock_source_id=None,
            executor_key="steam_trade_offer",
            spec=SteamCardSetSpec(game_name="Dota 2", allowed_bot_pool=("Bot-A",)),
        )
        config = self.ledger.create_recipe_config_version(
            listing_id="typed-listing-paid-cards", source_platform="playerok",
            external_listing_id="listing-paid-cards", recipe_items=[item], title="Paid cards",
        )
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO gd_steam_card_listing_limits(listing_id,total_sets) VALUES(?,1)",
                (config["listing_id"],),
            )
        adapter = FakePlayerok(
            baseline=[], active=[deal("paid-cards", "listing-paid-cards")]
        )
        self.syncer.sync(adapter)
        self.syncer.sync(adapter)
        bound = self.syncer.bind_paid_deal_config_version(
            account_user_id="seller-account-1", external_deal_id="paid-cards",
            config_version_id=config["config_version_id"], operator_id="desktop-operator",
            reason="Проверена версия объявления на момент оплаты",
        )
        cards = (
            CardCopy("paid-alpha", "Dota 2", "Dota Alpha", market_hash_name="Dota Alpha", game_app_id=570),
            CardCopy("paid-beta", "Dota 2", "Dota Beta", market_hash_name="Dota Beta", game_app_id=570),
        )
        snapshots = {("Bot-A", 753, "6"): InventorySnapshot(
            "Bot-A", "steam_cards", 753, "6", datetime.now(timezone.utc), cards, True,
        )}
        result = DeliveryOrderPipeline(self.database).prepare_paid_recipe_deal(
            account_user_id="seller-account-1", external_deal_id="paid-cards",
            snapshots=snapshots, recipient_badges=(RecipientBadge("Dota 2", 4),),
            badge_data_verified=True, buyer_complete_sets_by_app_id={570: 0},
            buyer_inventory_verified=True, seller_inventory_verified=True,
            card_hashes_by_app_id={570: ("Dota Alpha", "Dota Beta")},
            card_definitions_verified=True,
        )
        self.assertEqual((result.order_id, result.status), (bound["order_id"], "reserved"))
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_orders WHERE order_id=? AND status='reserved'",
                (bound["order_id"],),
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_order_lines WHERE order_id=?", (bound["order_id"],)
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_legs l JOIN gd_order_lines ol "
                "ON ol.order_line_id=l.order_line_id WHERE ol.order_id=?", (bound["order_id"],)
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (bound["order_id"],)
            ).fetchone()[0], 2)
            claim = db.execute(
                "SELECT sets,state FROM gd_steam_card_listing_claims WHERE order_id=?",
                (bound["order_id"],),
            ).fetchone()
            self.assertEqual(tuple(claim), (1, "reserved"))


if __name__ == "__main__":
    unittest.main()
