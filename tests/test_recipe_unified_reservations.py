from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.order_pipeline import DeliveryOrderPipeline
from core.delivery.reservation_service import InventorySnapshot, PlannedRecipeLine, PlannedResource
from core.delivery.listing_stock import ListingStockService
from core.delivery.listing_stock import InsufficientListingStock
from core.delivery.stock_ledger import QuantityStockLedger, StockResource
from core.delivery.stock_identity import StockItemIdentity
from core.delivery.recipes import DigitalTextUnitSpec, RecipeItem, SteamAssetSpec, SteamCardSetSpec
from games.cs2.inventory_view import CS2InventoryInstance, RestrictionStatus, RestrictionType
from games.steam_cards.recipient_badge import RecipientBadge
from games.steam_cards.order_preflight import SteamCardOrderPreflight
from games.steam_cards.reservations import AssetReservationStore
from games.steam_cards.selection import CardCopy


NOW = datetime.now(timezone.utc)


def recipe_item(item_id, position, kind, quantity, spec):
    return RecipeItem(
        item_id=item_id,
        position=position,
        kind=kind,
        spec_version=1,
        quantity_per_order=quantity,
        required=True,
        stock_source_id=None,
        executor_key="steam_trade_offer",
        spec=spec,
    )


class UnifiedRecipeReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "unified.sqlite3")
        self.ledger = DeliveryLedger(self.database)
        self.pipeline = DeliveryOrderPipeline(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def create_recipe_order(self, suffix, items):
        config = self.ledger.create_recipe_config_version(
            listing_id=f"listing-{suffix}",
            source_platform="playerok",
            external_listing_id=f"external-listing-{suffix}",
            recipe_items=items,
            title="Recipe " + suffix,
        )
        if any(item.kind == "steam_card_set" for item in items):
            # Playerok Steam Cards orders must reserve against a configured
            # listing-wide complete-set quota, just like production listings.
            with self.database.connect() as db:
                db.execute(
                    "INSERT INTO gd_steam_card_listing_limits(listing_id,total_sets) VALUES(?,100)",
                    (config["listing_id"],),
                )
        self.ledger.create_deal(
            deal_id=f"deal-{suffix}",
            source_platform="playerok",
            external_deal_id=f"external-deal-{suffix}",
            chat_id=f"chat-{suffix}",
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            platform_status="PAID",
        )
        self.ledger.create_order(
            order_id=f"order-{suffix}", deal_id=f"deal-{suffix}", total_quantity=1
        )
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_deals SET playerok_account_id='seller-test',is_historical=0 WHERE deal_id=?",
                (f"deal-{suffix}",),
            )
            db.execute(
                "INSERT OR REPLACE INTO playerok_sync_state(account_user_id,baseline_completed) VALUES('seller-test',1)"
            )
        return f"order-{suffix}", config["config_version_id"]

    @staticmethod
    def cs2(bot, asset_id):
        return CS2InventoryInstance(
            game_id="cs2", appid=730, context_id=2, bot_name=bot, asset_id=asset_id,
            class_id="1", instance_id="0", amount=1, name="AK-47 | Redline",
            item_type="Rifle", market_hash_name="AK-47 | Redline",
            is_tradable=True, is_marketable=True,
            restriction_type=RestrictionType.NONE,
            restriction_status=RestrictionStatus.AVAILABLE,
            last_inventory_refresh=NOW,
        )

    @staticmethod
    def mixed_snapshots(cs2_count=5):
        cs2_ids = [f"cs2-{index}" for index in range(1, cs2_count + 1)]
        cards_a = InventorySnapshot(
            "Bot-A", "steam_cards", 753, "6", NOW,
            (
                CardCopy("card-alpha", "Dota 2", "Dota Alpha", market_hash_name="Dota Alpha", game_app_id=570),
                CardCopy("card-beta", "Dota 2", "Dota Beta", market_hash_name="Dota Beta", game_app_id=570),
            ), True,
        )
        cards_b = InventorySnapshot(
            "Bot-B", "steam_cards", 753, "6", NOW, (), True,
        )
        assets_a = InventorySnapshot(
            "Bot-A", "cs2", 730, "2", NOW,
            tuple(UnifiedRecipeReservationTests.cs2("Bot-A", asset) for asset in cs2_ids[:2]), True,
        )
        assets_b = InventorySnapshot(
            "Bot-B", "cs2", 730, "2", NOW,
            tuple(UnifiedRecipeReservationTests.cs2("Bot-B", asset) for asset in cs2_ids[2:]), True,
        )
        return {
            ("Bot-A", 753, "6"): cards_a,
            ("Bot-B", 753, "6"): cards_b,
            ("Bot-A", 730, "2"): assets_a,
            ("Bot-B", 730, "2"): assets_b,
        }

    @staticmethod
    def multi_card_snapshots(complete_sets=2):
        cards = []
        for index in range(1, complete_sets + 1):
            cards.extend((
                CardCopy(
                    f"dota-alpha-{index}", "Dota 2", "Dota Alpha",
                    market_hash_name="Dota Alpha", game_app_id=570,
                ),
                CardCopy(
                    f"dota-beta-{index}", "Dota 2", "Dota Beta",
                    market_hash_name="Dota Beta", game_app_id=570,
                ),
            ))
        return {
            ("Bot-A", 753, "6"): InventorySnapshot(
                "Bot-A", "steam_cards", 753, "6", NOW, tuple(cards), True,
            ),
        }

    def mixed_recipe(self, cs2_quantity=5):
        return (
            recipe_item("cards", 1, "steam_card_set", 1,
                        SteamCardSetSpec(game_name="Dota 2", allowed_bot_pool=("Bot-A", "Bot-B"))),
            recipe_item("cs2", 2, "steam_asset", cs2_quantity,
                        SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"},
                                       ("Bot-A", "Bot-B"))),
        )

    def prepare(self, order_id, snapshots=None):
        return self.pipeline.prepare_recipe_order(
            order_id=order_id,
            snapshots=snapshots or self.mixed_snapshots(),
            recipient_badges=[RecipientBadge("Dota 2", 4)],
            badge_data_verified=True,
            buyer_complete_sets_by_app_id={570: 0},
            buyer_inventory_verified=True,
            seller_inventory_verified=True,
            card_hashes_by_app_id={570: ("Dota Alpha", "Dota Beta")},
            card_definitions_verified=True,
        )

    def line_rows(self, order):
        with self.database.connect() as db:
            return db.execute(
                "SELECT order_line_id,line_number,item_type FROM gd_order_lines "
                "WHERE order_id=? ORDER BY line_number", (order,),
            ).fetchall()

    def recipe_item_id(self, config_id, position):
        with self.database.connect() as db:
            recipe = self.ledger.recipe_for_config_version(db, config_id)
        return next(item.item_id for item in recipe.items if item.position == position)

    def assert_no_reservation_commit(self, order):
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM gd_orders WHERE order_id=?", (order,)).fetchone()[0], "pending")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_legs WHERE order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,),
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active' AND order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,),
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM steam_asset_reservations WHERE order_id=?", (order,),
            ).fetchone()[0], 0)

    def cs2_line_plan(self, item_id, asset_ids):
        return PlannedRecipeLine(item_id, tuple(
            (PlannedResource("Bot-A", asset, "cs2", 730, "2"),)
            for asset in asset_ids
        ))

    def card_preflight(self, order_id, inventories, requested_sets=1):
        return SteamCardOrderPreflight(AssetReservationStore(self.database.path)).plan_multi_game(
            order_id=order_id,
            requested_sets=requested_sets,
            inventories=inventories,
            recipient_badges=(RecipientBadge("Dota 2", 4),),
            badge_data_verified=True,
            buyer_complete_sets_by_app_id={570: 0},
            buyer_inventory_verified=True,
            seller_inventory_verified=True,
            card_hashes_by_app_id={570: ("Dota Alpha", "Dota Beta")},
            card_definitions_verified=True,
        )

    @staticmethod
    def card_line_plan(item_id, preflight):
        units = tuple(tuple(PlannedResource(
            asset.bot_name, asset.asset_id, "steam_cards", 753, "6", True, asset.card_hash_name
        ) for asset in selected_set.asset_ids) for selected_set in preflight.selected_sets)
        card_sizes = {selected_set.cards_per_set for selected_set in preflight.selected_sets}
        return PlannedRecipeLine(
            item_id,
            units,
            steam_card_count_per_set=next(iter(card_sizes)) if len(card_sizes) == 1 else None,
            steam_card_preflight=preflight,
        )

    def test_recipe_manifest_is_created_inside_successful_reservation_commit(self):
        item = recipe_item(
            "atomic-cs2", 1, "steam_asset", 1,
            SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"},
                           ("Bot-A", "Bot-B")),
        )
        order, _config_id = self.create_recipe_order("atomic-manifest-success", (item,))
        self.assertEqual(self.line_rows(order), [])

        result = self.prepare(order)
        self.assertTrue(result.reserved, result.message)
        self.assertEqual(len(self.line_rows(order)), 1)
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_legs l "
                "JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id "
                "WHERE ol.order_id=?", (order,),
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations r "
                "JOIN gd_order_lines ol ON ol.order_line_id=r.order_line_id "
                "WHERE ol.order_id=? AND r.state='active'", (order,),
            ).fetchone()[0], 1)

    def test_recipe_manifest_materialization_rolls_back_with_failed_reservation_commit(self):
        item = recipe_item(
            "atomic-cs2-rollback", 1, "steam_asset", 1,
            SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"},
                           ("Bot-A",)),
        )
        order, config_id = self.create_recipe_order("atomic-manifest-rollback", (item,))
        self.assertEqual(self.line_rows(order), [])
        line_id = self.ledger._stable_recipe_line_id(order, config_id, item.item_id)

        with self.assertRaises(ValueError):
            self.pipeline.cs2_reservations.reserve_recipe_plan(
                order_id=order,
                config_version_id=config_id,
                planned_by_line={line_id: PlannedRecipeLine("wrong-recipe-item", ())},
            )

        self.assertEqual(self.line_rows(order), [])
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_legs WHERE order_line_id LIKE 'recipe-line:%'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT status FROM gd_orders WHERE order_id=?", (order,),
            ).fetchone()[0], "pending")

    def test_steam_card_listing_limit_is_atomic_and_cancel_releases_claim(self):
        item = recipe_item(
            "cards", 1, "steam_card_set", 1,
            SteamCardSetSpec(game_name="Dota 2", allowed_bot_pool=("Bot-A",)),
        )
        first_order, config_id = self.create_recipe_order("steam-card-cap-flow", (item,))
        listing_id = "listing-steam-card-cap-flow"
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_steam_card_listing_limits SET total_sets=1 WHERE listing_id=?",
                (listing_id,),
            )

        second_deal = "deal-steam-card-cap-flow-second"
        second_order = "order-steam-card-cap-flow-second"
        self.ledger.create_deal(
            deal_id=second_deal,
            source_platform="playerok",
            external_deal_id="external-steam-card-cap-flow-second",
            chat_id="chat-steam-card-cap-flow-second",
            listing_id=listing_id,
            config_version_id=config_id,
            platform_status="PAID",
        )
        self.ledger.create_order(
            order_id=second_order, deal_id=second_deal, total_quantity=1
        )
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_deals SET playerok_account_id='seller-test',is_historical=0 WHERE deal_id=?",
                (second_deal,),
            )
            db.execute(
                "INSERT OR REPLACE INTO playerok_sync_state(account_user_id,baseline_completed) "
                "VALUES('seller-test',1)"
            )

        snapshots = self.multi_card_snapshots(complete_sets=2)
        first = self.prepare(first_order, snapshots)
        self.assertTrue(first.reserved, first.message)
        with self.database.connect() as db:
            first_claim = db.execute(
                "SELECT sets,state FROM gd_steam_card_listing_claims WHERE order_id=?",
                (first_order,),
            ).fetchone()
            physical_card_count = db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?) AND app_id=753 AND context_id='6'",
                (first_order,),
            ).fetchone()[0]
        self.assertEqual((int(first_claim["sets"]), first_claim["state"]), (1, "reserved"))
        self.assertEqual(physical_card_count, 2)

        second_blocked = self.prepare(second_order, snapshots)
        self.assertEqual(second_blocked.status, "preflight_blocked")
        self.assertIn("Недостаточно закреплённого складского остатка", second_blocked.message)
        self.assert_no_reservation_commit(second_order)
        with self.database.connect() as db:
            claimed = db.execute(
                "SELECT COALESCE(SUM(sets),0) FROM gd_steam_card_listing_claims "
                "WHERE listing_id=? AND state='reserved'",
                (listing_id,),
            ).fetchone()[0]
            second_claims = db.execute(
                "SELECT COUNT(*) FROM gd_steam_card_listing_claims WHERE order_id=?",
                (second_order,),
            ).fetchone()[0]
        self.assertEqual((int(claimed), int(second_claims)), (1, 0))

        cancelled = self.pipeline.cancel_order(first_order)
        self.assertEqual(cancelled["released_steam_card_listing_sets"], 1)
        second = self.prepare(second_order, snapshots)
        self.assertTrue(second.reserved, second.message)
        with self.database.connect() as db:
            claims = db.execute(
                "SELECT order_id,sets,state FROM gd_steam_card_listing_claims "
                "WHERE listing_id=? ORDER BY order_id",
                (listing_id,),
            ).fetchall()
        claim_states = {
            row["order_id"]: (int(row["sets"]), row["state"])
            for row in claims
        }
        self.assertEqual(claim_states, {
            first_order: (1, "released"),
            second_order: (1, "reserved"),
        })

    def test_steam_card_recipe_without_manual_bot_pool_uses_available_card_snapshots(self):
        item = recipe_item(
            "cards", 1, "steam_card_set", 1,
            SteamCardSetSpec(game_name="Dota 2"),
        )
        order, _config_id = self.create_recipe_order("steam-card-all-bots", (item,))
        result = self.prepare(order, self.multi_card_snapshots(complete_sets=1))
        self.assertTrue(result.reserved, result.message)
        with self.database.connect() as db:
            card_reservations = db.execute(
                "SELECT bot_name,app_id,context_id,COUNT(*) AS cards "
                "FROM gd_resource_reservations WHERE owner_type='order' AND state='active' "
                "AND order_line_id IN (SELECT order_line_id FROM gd_order_lines WHERE order_id=?) "
                "GROUP BY bot_name,app_id,context_id",
                (order,),
            ).fetchall()
            claim = db.execute(
                "SELECT sets,state FROM gd_steam_card_listing_claims WHERE order_id=?",
                (order,),
            ).fetchone()
        self.assertEqual(
            [(row["bot_name"], int(row["app_id"]), row["context_id"], int(row["cards"]))
             for row in card_reservations],
            [("Bot-A", 753, "6", 2)],
        )
        self.assertEqual((int(claim["sets"]), claim["state"]), (1, "reserved"))

    def test_playerok_steam_card_order_without_listing_limit_fails_closed(self):
        item = recipe_item(
            "cards", 1, "steam_card_set", 1,
            SteamCardSetSpec(game_name="Dota 2", allowed_bot_pool=("Bot-A",)),
        )
        order, _config_id = self.create_recipe_order("steam-card-no-limit", (item,))
        listing_id = "listing-steam-card-no-limit"
        with self.database.connect() as db:
            db.execute(
                "DELETE FROM gd_steam_card_listing_limits WHERE listing_id=?",
                (listing_id,),
            )
        with self.assertRaisesRegex(ValueError, "не задано общее количество наборов"):
            self.prepare(order, self.multi_card_snapshots(complete_sets=1))
        self.assert_no_reservation_commit(order)
        with self.database.connect() as db:
            claims = db.execute(
                "SELECT COUNT(*) FROM gd_steam_card_listing_claims WHERE order_id=?",
                (order,),
            ).fetchone()[0]
        self.assertEqual(claims, 0)

    def test_alternative_cs2_selectors_reserve_complete_variant_batches_and_fallback_per_order(self):
        item = recipe_item(
            "variant-choice", 1, "steam_asset", 2,
            SteamAssetSpec(
                "cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"},
                ("Bot-A",),
                selector_options=({"market_hash_name": "AWP | Dragon Lore"},),
            ),
        )
        config = self.ledger.create_recipe_config_version(
            listing_id="listing-alternative-variants",
            source_platform="playerok",
            external_listing_id="external-alternative-variants",
            recipe_items=(item,),
        )
        item_id = config["recipe_items"][0]["item_id"]
        stock = QuantityStockLedger(self.database)
        resources = (
            ("redline-1", "AK-47 | Redline"),
            ("redline-2", "AK-47 | Redline"),
            ("dragon-1", "AWP | Dragon Lore"),
            ("dragon-2", "AWP | Dragon Lore"),
            ("dragon-3", "AWP | Dragon Lore"),
            ("dragon-4", "AWP | Dragon Lore"),
        )
        for asset_id, market_name in resources:
            stock.observe(
                StockResource("cs2", 730, "2", "Bot-A", asset_id), 1,
                identity=StockItemIdentity(
                    "cs2", 730, "2", market_hash_name=market_name,
                    name=market_name, item_type="Weapon", class_id="class-" + asset_id,
                ),
                is_tradable=True,
            )
        allocations = ListingStockService(self.database).allocate_recipe_items(
            request_id="alternative-variants-stock",
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            units_by_recipe_item={item_id: 6},
        )
        self.assertEqual(sum(int(row["units"]) for row in allocations), 6)
        with self.database.connect() as db:
            reserved_by_variant = {}
            for row in db.execute(
                "SELECT o.identity_json,a.units FROM gd_stock_allocations a "
                "JOIN gd_stock_observations o ON o.observation_id=a.observation_id "
                "WHERE a.owner_type='listing' AND a.listing_id=? AND a.state='active'",
                (config["listing_id"],),
            ).fetchall():
                name = json.loads(row["identity_json"])["market_hash_name"]
                reserved_by_variant[name] = reserved_by_variant.get(name, 0) + int(row["units"])
        self.assertEqual(reserved_by_variant, {
            "AK-47 | Redline": 2,
            "AWP | Dragon Lore": 4,
        })

        order_ids = ("alternative-order-1", "alternative-order-2", "alternative-order-3")
        for index, order_id in enumerate(order_ids, start=1):
            deal_id = f"alternative-deal-{index}"
            self.ledger.create_deal(
                deal_id=deal_id, source_platform="playerok",
                external_deal_id=f"alternative-external-deal-{index}",
                chat_id=f"alternative-chat-{index}",
                listing_id=config["listing_id"],
                config_version_id=config["config_version_id"],
                platform_status="PAID",
            )
            self.ledger.create_order(order_id=order_id, deal_id=deal_id, total_quantity=1)
            with self.database.connect() as db:
                db.execute(
                    "UPDATE gd_deals SET playerok_account_id='seller-test',is_historical=0 WHERE deal_id=?",
                    (deal_id,),
                )
                db.execute(
                    "INSERT OR REPLACE INTO playerok_sync_state(account_user_id,baseline_completed) "
                    "VALUES('seller-test',1)"
                )
            prepared = self.pipeline.prepare_recipe_order(order_id=order_id, snapshots={})
            self.assertTrue(prepared.reserved, prepared.message)
            with self.database.connect() as db:
                selected = db.execute(
                    "SELECT o.identity_json,a.asset_id FROM gd_stock_allocations a "
                    "JOIN gd_stock_observations o ON o.observation_id=a.observation_id "
                    "JOIN gd_order_lines ol ON ol.order_line_id=a.order_line_id "
                    "WHERE a.owner_type='order' AND a.state='active' AND ol.order_id=?",
                    (order_id,),
                ).fetchall()
            selected_variants = {
                json.loads(row["identity_json"])["market_hash_name"] for row in selected
            }
            self.assertEqual(len(selected), 2)
            expected = "AK-47 | Redline" if index == 1 else "AWP | Dragon Lore"
            self.assertEqual(selected_variants, {expected})

        with self.database.connect() as db:
            owned_assets = db.execute(
                "SELECT asset_id,COUNT(DISTINCT order_line_id) AS owners "
                "FROM gd_stock_allocations WHERE owner_type='order' AND state='active' "
                "GROUP BY asset_id"
            ).fetchall()
        self.assertTrue(owned_assets)
        self.assertTrue(all(int(row["owners"]) == 1 for row in owned_assets))

    def test_order_resource_conflict_rolls_back_prior_listing_stock_transfer(self):
        stock_item = recipe_item(
            "stock-backed", 1, "steam_asset", 1,
            SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",)),
        )
        manual_item = recipe_item(
            "manual", 2, "steam_asset", 1,
            SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AWP | Asiimov"}, ("Bot-A",)),
        )
        order, config_id = self.create_recipe_order("mixed-stock-conflict", (stock_item, manual_item))
        stock_id = self.ledger._stable_recipe_line_id(order, config_id, stock_item.item_id)
        manual_id = self.ledger._stable_recipe_line_id(order, config_id, manual_item.item_id)

        stock = QuantityStockLedger(self.database)
        stock.observe(
            StockResource("cs2", 730, "2", "Bot-A", "stock-backed-asset"), 1,
            identity=StockItemIdentity(
                "cs2", 730, "2", market_hash_name="AK-47 | Redline",
                name="AK-47 | Redline", item_type="Rifle", class_id="stock-class",
            ),
            is_tradable=True,
        )
        ListingStockService(self.database).allocate_recipe_items(
            request_id="mixed-stock-conflict-batch",
            listing_id="listing-mixed-stock-conflict",
            config_version_id=config_id,
            units_by_recipe_item={stock_item.item_id: 1},
        )

        conflict_order, conflict_config = self.create_recipe_order(
            "mixed-stock-conflict-owner", (manual_item,)
        )
        self.ledger.materialize_recipe_order_lines(conflict_order)
        conflict_line = self.line_rows(conflict_order)[0]["order_line_id"]
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO gd_resource_reservations "
                "(reservation_id,owner_type,order_line_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                "VALUES(?,?,?,?,?,?,?,?,?,'active')",
                ("conflict-reservation", "order", conflict_line, "cs2", 730, "2",
                 "Bot-A", "conflict-asset", 1),
            )

        result = self.pipeline.cs2_reservations.reserve_recipe_plan(
            order_id=order,
            config_version_id=config_id,
            planned_by_line={
                stock_id: PlannedRecipeLine(stock_item.item_id, (), use_listing_stock=True),
                manual_id: PlannedRecipeLine(
                    manual_item.item_id,
                    ((PlannedResource("Bot-A", "conflict-asset", "cs2", 730, "2"),),),
                ),
            },
        )
        self.assertEqual(result.status, "preflight_blocked")
        with self.database.connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM gd_orders WHERE order_id=?", (order,)).fetchone()[0],
                "preflight_blocked",
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM gd_stock_transfers WHERE order_line_id IN "
                    "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations "
                    "WHERE owner_type='order' AND state='active' AND order_line_id IN "
                    "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations "
                    "WHERE owner_type='listing' AND state='active' AND config_version_id=?",
                    (config_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM gd_delivery_legs WHERE order_line_id IN "
                    "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,),
                ).fetchone()[0],
                0,
            )

    def test_listing_stack_amounts_flow_into_delivery_leg_and_canonical_reservation(self):
        item = recipe_item(
            "stacked-redline", 1, "steam_asset", 10,
            SteamAssetSpec(
                "cs2", 730, "2",
                {"market_hash_name": "AK-47 | Redline"},
                ("Bot-A", "Bot-B"),
            ),
        )
        order, config_id = self.create_recipe_order("stacked-stock", (item,))
        item_id = self.recipe_item_id(config_id, 1)
        stock = QuantityStockLedger(self.database)
        identity = StockItemIdentity(
            "cs2", 730, "2",
            market_hash_name="AK-47 | Redline",
            name="AK-47 | Redline",
            item_type="Rifle",
            class_id="class-redline",
        )
        resources = (
            StockResource("cs2", 730, "2", "Bot-A", "51001"),
            StockResource("cs2", 730, "2", "Bot-B", "52002"),
        )
        for resource, amount in zip(resources, (4, 6), strict=True):
            stock.observe(resource, amount, identity=identity, is_tradable=True)
        listing = ListingStockService(self.database)
        listing.allocate_recipe_items(
            request_id="stacked-stock-batch",
            listing_id="listing-stacked-stock",
            config_version_id=config_id,
            units_by_recipe_item={item_id: 10},
        )

        result = self.pipeline.prepare_recipe_order(order_id=order, snapshots={})
        self.assertTrue(result.reserved, result.message)
        with self.database.connect() as db:
            legs = db.execute(
                "SELECT bot_name,asset_ids_json,asset_amounts_json FROM gd_delivery_legs "
                "WHERE order_line_id IN (SELECT order_line_id FROM gd_order_lines WHERE order_id=?) "
                "ORDER BY bot_name",
                (order,),
            ).fetchall()
            reservations = db.execute(
                "SELECT bot_name,asset_id,units FROM gd_stock_allocations "
                "WHERE owner_type='order' AND state='active' AND order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?) ORDER BY bot_name",
                (order,),
            ).fetchall()
            transferred = db.execute(
                "SELECT state,SUM(units) AS units FROM gd_stock_allocations "
                "WHERE owner_type='order' AND order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?) GROUP BY state",
                (order,),
            ).fetchall()
            canonical_count = db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE owner_type='order' "
                "AND state='active' AND order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)",
                (order,),
            ).fetchone()[0]
        self.assertEqual(
            {row["bot_name"]: json.loads(row["asset_amounts_json"]) for row in legs},
            {"Bot-A": {"51001": 4}, "Bot-B": {"52002": 6}},
        )
        self.assertEqual(
            {(row["bot_name"], row["asset_id"], int(row["units"])) for row in reservations},
            {("Bot-A", "51001", 4), ("Bot-B", "52002", 6)},
        )
        self.assertEqual(
            {(row["state"], int(row["units"])) for row in transferred},
            {("active", 10)},
        )
        self.assertEqual(canonical_count, 0)

        repeated = self.pipeline.prepare_recipe_order(order_id=order, snapshots={})
        self.assertTrue(repeated.reserved, repeated.message)
        with self.database.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM gd_delivery_legs WHERE order_line_id IN "
                    "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)",
                    (order,),
                ).fetchone()[0],
                2,
            )

    def test_two_orders_can_reserve_disjoint_amounts_from_the_same_stack(self):
        item = recipe_item(
            "repeat-sale-redline", 1, "steam_asset", 3,
            SteamAssetSpec(
                "cs2", 730, "2",
                {"market_hash_name": "AK-47 | Redline"},
                ("Bot-A",),
            ),
        )
        config = self.ledger.create_recipe_config_version(
            listing_id="repeat-sale-listing",
            source_platform="playerok",
            external_listing_id="repeat-sale-external",
            recipe_items=(item,),
        )
        item_id = self.recipe_item_id(config["config_version_id"], 1)
        stock = QuantityStockLedger(self.database)
        resource = StockResource("cs2", 730, "2", "Bot-A", "61001")
        identity = StockItemIdentity(
            "cs2", 730, "2",
            market_hash_name="AK-47 | Redline",
            name="AK-47 | Redline",
            item_type="Rifle",
            class_id="class-redline",
        )
        stock.observe(resource, 10, identity=identity, is_tradable=True)
        ListingStockService(self.database).allocate_recipe_items(
            request_id="repeat-sale-stock-batch",
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            units_by_recipe_item={item_id: 10},
        )

        orders = ("repeat-order-1", "repeat-order-2")
        for index, order_id in enumerate(orders, start=1):
            deal_id = f"repeat-deal-{index}"
            self.ledger.create_deal(
                deal_id=deal_id,
                source_platform="playerok",
                external_deal_id=f"repeat-external-deal-{index}",
                chat_id=f"repeat-chat-{index}",
                listing_id=config["listing_id"],
                config_version_id=config["config_version_id"],
                platform_status="PAID",
            )
            self.ledger.create_order(order_id=order_id, deal_id=deal_id, total_quantity=1)
            with self.database.connect() as db:
                db.execute(
                    "UPDATE gd_deals SET playerok_account_id='seller-test',is_historical=0 WHERE deal_id=?",
                    (deal_id,),
                )
                db.execute(
                    "INSERT OR REPLACE INTO playerok_sync_state(account_user_id,baseline_completed) VALUES('seller-test',1)"
                )
            result = self.pipeline.prepare_recipe_order(order_id=order_id, snapshots={})
            self.assertTrue(result.reserved, result.message)

        with self.database.connect() as db:
            allocations = db.execute(
                "SELECT ol.order_id,a.asset_id,a.units,a.state FROM gd_stock_allocations a "
                "JOIN gd_order_lines ol ON ol.order_line_id=a.order_line_id "
                "WHERE a.owner_type='order' AND a.state='active' AND ol.order_id IN (?,?) "
                "ORDER BY ol.order_id",
                orders,
            ).fetchall()
            listing_remaining = db.execute(
                "SELECT SUM(units) FROM gd_stock_allocations "
                "WHERE owner_type='listing' AND state='active' AND listing_id=? AND config_version_id=?",
                (config["listing_id"], config["config_version_id"]),
            ).fetchone()[0]
            amount_manifests = db.execute(
                "SELECT ol.order_id,l.asset_amounts_json FROM gd_delivery_legs l "
                "JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id "
                "WHERE ol.order_id IN (?,?) ORDER BY ol.order_id",
                orders,
            ).fetchall()
        self.assertEqual(
            {(row["order_id"], row["asset_id"], int(row["units"]), row["state"]) for row in allocations},
            {
                ("repeat-order-1", "61001", 3, "active"),
                ("repeat-order-2", "61001", 3, "active"),
            },
        )
        self.assertEqual(int(listing_remaining), 4)
        self.assertEqual(
            {row["order_id"]: json.loads(row["asset_amounts_json"]) for row in amount_manifests},
            {"repeat-order-1": {"61001": 3}, "repeat-order-2": {"61001": 3}},
        )

    def test_cancelled_quantity_stock_order_returns_units_to_listing(self):
        item = recipe_item(
            "cancel-return-redline", 1, "steam_asset", 3,
            SteamAssetSpec(
                "cs2", 730, "2",
                {"market_hash_name": "AK-47 | Redline"},
                ("Bot-A",),
            ),
        )
        order, config_id = self.create_recipe_order("cancel-return", (item,))
        item_id = self.recipe_item_id(config_id, 1)
        stock = QuantityStockLedger(self.database)
        resource = StockResource("cs2", 730, "2", "Bot-A", "62001")
        identity = StockItemIdentity(
            "cs2", 730, "2",
            market_hash_name="AK-47 | Redline",
            name="AK-47 | Redline",
            item_type="Rifle",
            class_id="class-redline",
        )
        stock.observe(resource, 10, identity=identity, is_tradable=True)
        ListingStockService(self.database).allocate_recipe_items(
            request_id="cancel-return-stock-batch",
            listing_id="listing-cancel-return",
            config_version_id=config_id,
            units_by_recipe_item={item_id: 10},
        )
        prepared = self.pipeline.prepare_recipe_order(order_id=order, snapshots={})
        self.assertTrue(prepared.reserved, prepared.message)
        cancelled = self.pipeline.cancel_order(order)
        self.assertEqual(cancelled["released_quantity_allocations"], 1)
        self.assertEqual(cancelled["restored_listing_units"], 3)
        with self.database.connect() as db:
            listing_units = db.execute(
                "SELECT SUM(units) FROM gd_stock_allocations "
                "WHERE owner_type='listing' AND state='active' AND listing_id=? AND config_version_id=?",
                ("listing-cancel-return", config_id),
            ).fetchone()[0]
            order_active = db.execute(
                "SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations "
                "WHERE owner_type='order' AND state='active' AND order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)",
                (order,),
            ).fetchone()[0]
        self.assertEqual(int(listing_units), 10)
        self.assertEqual(int(order_active), 0)

    def test_mixed_cards_and_five_cs2_assets_commit_all_positions_together(self):
        order, _ = self.create_recipe_order("mixed", self.mixed_recipe())
        result = self.prepare(order)
        self.assertTrue(result.reserved, result.message)
        self.assertEqual(len(result.reserved_assets), 7)
        with self.database.connect() as db:
            lines = db.execute(
                "SELECT order_line_id,line_number,item_type,quantity FROM gd_order_lines WHERE order_id=? ORDER BY line_number",
                (order,),
            ).fetchall()
            reservations = db.execute(
                "SELECT game_id,app_id,context_id,state,COUNT(*) AS count FROM gd_resource_reservations "
                "WHERE order_line_id IN (SELECT order_line_id FROM gd_order_lines WHERE order_id=?) "
                "GROUP BY game_id,app_id,context_id,state",
                (order,),
            ).fetchall()
            legacy_cards = db.execute(
                "SELECT COUNT(*) FROM steam_asset_reservations WHERE order_id=?", (order,)
            ).fetchone()[0]
            event_count = db.execute(
                "SELECT COUNT(*) FROM gd_reservation_events WHERE new_state='active'"
            ).fetchone()[0]
        self.assertEqual([(row["line_number"], row["item_type"], row["quantity"]) for row in lines], [
            (1, "steam_card_set", 1), (2, "steam_asset", 5),
        ])
        self.assertEqual({(row["game_id"], row["app_id"], row["context_id"], row["state"], row["count"])
                          for row in reservations}, {
            ("steam_cards", 753, "6", "active", 2),
            ("cs2", 730, "2", "active", 5),
        })
        self.assertEqual(legacy_cards, 2)
        self.assertEqual(event_count, 7)
        with self.database.connect() as db:
            cs2_by_bot = {row["bot_name"]: row["count"] for row in db.execute(
                "SELECT bot_name,COUNT(*) AS count FROM gd_resource_reservations "
                "WHERE state='active' AND game_id='cs2' GROUP BY bot_name"
            )}
        self.assertEqual(cs2_by_bot, {"Bot-A": 2, "Bot-B": 3})

    def test_reservation_boundary_rejects_plan_missing_required_recipe_item(self):
        order, config_id = self.create_recipe_order("boundary-missing-item", self.mixed_recipe())
        self.ledger.materialize_recipe_order_lines(order)
        lines = self.line_rows(order)
        cs2_line = next(row for row in lines if row["item_type"] == "steam_asset")
        plan = {
            cs2_line["order_line_id"]: self.cs2_line_plan(
                self.recipe_item_id(config_id, cs2_line["line_number"]), ["boundary-1"]
            )
        }

        with self.assertRaisesRegex(ValueError, "all mandatory|все обязательные"):
            self.pipeline.cs2_reservations.reserve_recipe_plan(
                order_id=order, config_version_id=config_id, planned_by_line=plan,
            )

        self.assert_no_reservation_commit(order)

    def test_reservation_boundary_rejects_insufficient_recipe_item_quantity(self):
        recipe = (recipe_item(
            "cs2", 1, "steam_asset", 2,
            SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",)),
        ),)
        order, config_id = self.create_recipe_order("boundary-short-quantity", recipe)
        self.ledger.materialize_recipe_order_lines(order)
        line = self.line_rows(order)[0]
        plan = {
            line["order_line_id"]: self.cs2_line_plan(
                self.recipe_item_id(config_id, line["line_number"]), ["only-one"]
            )
        }

        with self.assertRaisesRegex(ValueError, "quantity"):
            self.pipeline.cs2_reservations.reserve_recipe_plan(
                order_id=order, config_version_id=config_id, planned_by_line=plan,
            )

        self.assert_no_reservation_commit(order)

    def test_invalid_asset_ids_are_rejected_for_physical_resources(self):
        recipe = (recipe_item(
            "cs2", 1, "steam_asset", 1,
            SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",)),
        ),)
        order, config_id = self.create_recipe_order("bad-asset-id", recipe)
        self.ledger.materialize_recipe_order_lines(order)
        line = self.line_rows(order)[0]
        item_id = self.recipe_item_id(config_id, 1)
        for asset_id in (None, "", "   ", True, 123, 12.5):
            with self.subTest(asset_id=asset_id):
                plan = PlannedRecipeLine(item_id, ((
                    PlannedResource("Bot-A", asset_id, "cs2", 730, "2"),
                ),))
                with self.assertRaisesRegex(ValueError, "asset_id должен быть непустой строкой"):
                    self.pipeline.cs2_reservations.reserve_recipe_plan(
                        order_id=order, config_version_id=config_id,
                        planned_by_line={line["order_line_id"]: plan},
                    )
                self.assert_no_reservation_commit(order)

        valid_plan = self.cs2_line_plan(item_id, [" valid-asset "])
        result = self.pipeline.cs2_reservations.reserve_recipe_plan(
            order_id=order, config_version_id=config_id,
            planned_by_line={line["order_line_id"]: valid_plan},
        )
        self.assertTrue(result.reserved)
        self.assertEqual(result.reserved_assets, (("Bot-A", "valid-asset"),))

    def test_reservation_boundary_accepts_full_mixed_recipe_plan(self):
        recipe = (
            recipe_item("cards", 1, "steam_card_set", 1,
                        SteamCardSetSpec(game_name="Dota 2", allowed_bot_pool=("Bot-A",))),
            recipe_item("cs2", 2, "steam_asset", 1,
                        SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",))),
        )
        order, config_id = self.create_recipe_order("boundary-full-mixed", recipe)
        self.ledger.materialize_recipe_order_lines(order)
        lines = self.line_rows(order)
        card_line = next(row for row in lines if row["item_type"] == "steam_card_set")
        cs2_line = next(row for row in lines if row["item_type"] == "steam_asset")
        card_preflight = self.card_preflight(order, {"Bot-A": (
            CardCopy("boundary-card-a", "Dota 2", "Dota Alpha", market_hash_name="Dota Alpha", game_app_id=570),
            CardCopy("boundary-card-b", "Dota 2", "Dota Beta", market_hash_name="Dota Beta", game_app_id=570),
        )})
        plan = {
            card_line["order_line_id"]: self.card_line_plan(self.recipe_item_id(config_id, 1), card_preflight),
            cs2_line["order_line_id"]: self.cs2_line_plan(
                self.recipe_item_id(config_id, 2), ["boundary-cs2"]
            ),
        }

        result = self.pipeline.cs2_reservations.reserve_recipe_plan(
            order_id=order, config_version_id=config_id, planned_by_line=plan,
        )

        self.assertTrue(result.reserved)
        self.assertEqual(len(result.reserved_assets), 3)

    def test_card_count_and_arbitrary_assets_cannot_substitute_for_preflight_composition(self):
        recipe = (recipe_item(
            "cards", 1, "steam_card_set", 1,
            SteamCardSetSpec(game_name="Dota 2", allowed_bot_pool=("Bot-A",)),
        ),)
        order, config_id = self.create_recipe_order("card-proof-required", recipe)
        self.ledger.materialize_recipe_order_lines(order)
        line = self.line_rows(order)[0]
        item_id = self.recipe_item_id(config_id, 1)
        preflight = self.card_preflight(order, {"Bot-A": (
            CardCopy("real-alpha", "Dota 2", "Dota Alpha", market_hash_name="Dota Alpha", game_app_id=570),
            CardCopy("real-beta", "Dota 2", "Dota Beta", market_hash_name="Dota Beta", game_app_id=570),
        )})

        plans = (
            PlannedRecipeLine(item_id, ((
                PlannedResource("Bot-A", "one-card", "steam_cards", 753, "6", True, "Dota Alpha"),
            ),), steam_card_count_per_set=1),
            PlannedRecipeLine(item_id, ((
                PlannedResource("Bot-A", "fake-alpha", "steam_cards", 753, "6", True, "Dota Alpha"),
                PlannedResource("Bot-A", "fake-beta", "steam_cards", 753, "6", True, "Dota Beta"),
            ),), steam_card_count_per_set=2,
                steam_card_preflight=preflight),
        )
        for plan in plans:
            with self.subTest(plan=plan):
                with self.assertRaises(ValueError):
                    self.pipeline.cs2_reservations.reserve_recipe_plan(
                        order_id=order, config_version_id=config_id,
                        planned_by_line={line["order_line_id"]: plan},
                    )
                self.assert_no_reservation_commit(order)

    def test_card_unit_must_use_one_bot_and_unique_assets(self):
        recipe = (recipe_item(
            "cards", 1, "steam_card_set", 1,
            SteamCardSetSpec(game_name="Dota 2", allowed_bot_pool=("Bot-A", "Bot-B")),
        ),)
        order, config_id = self.create_recipe_order("card-unit-owner", recipe)
        self.ledger.materialize_recipe_order_lines(order)
        line = self.line_rows(order)[0]
        item_id = self.recipe_item_id(config_id, 1)
        mixed_bot_preflight = self.card_preflight(order, {
            "Bot-A": (CardCopy("card-a", "Dota 2", "Dota Alpha", market_hash_name="Dota Alpha", game_app_id=570),),
            "Bot-B": (CardCopy("card-b", "Dota 2", "Dota Beta", market_hash_name="Dota Beta", game_app_id=570),),
        })
        complete_preflight = self.card_preflight(order, {"Bot-A": (
            CardCopy("same-asset", "Dota 2", "Dota Alpha", market_hash_name="Dota Alpha", game_app_id=570),
            CardCopy("card-b", "Dota 2", "Dota Beta", market_hash_name="Dota Beta", game_app_id=570),
        )})
        invalid_plans = (
            self.card_line_plan(item_id, mixed_bot_preflight),
            PlannedRecipeLine(item_id, ((
                PlannedResource("Bot-A", "same-asset", "steam_cards", 753, "6", True, "Dota Alpha"),
                PlannedResource("Bot-A", "same-asset", "steam_cards", 753, "6", True, "Dota Beta"),
            ),), steam_card_count_per_set=2, steam_card_preflight=complete_preflight),
        )
        for plan in invalid_plans:
            with self.subTest(plan=plan):
                with self.assertRaises(ValueError):
                    self.pipeline.cs2_reservations.reserve_recipe_plan(
                        order_id=order, config_version_id=config_id,
                        planned_by_line={line["order_line_id"]: plan},
                    )
                self.assert_no_reservation_commit(order)

    def test_two_preflight_complete_card_sets_reserve_in_one_order_line(self):
        recipe = (recipe_item(
            "cards", 1, "steam_card_set", 2,
            SteamCardSetSpec(game_name="Dota 2", allowed_bot_pool=("Bot-A",)),
        ),)
        order, _ = self.create_recipe_order("two-complete-card-sets", recipe)
        snapshot = InventorySnapshot(
            "Bot-A", "steam_cards", 753, "6", NOW,
            (
                CardCopy("alpha-1", "Dota 2", "Dota Alpha", market_hash_name="Dota Alpha", game_app_id=570),
                CardCopy("alpha-2", "Dota 2", "Dota Alpha", market_hash_name="Dota Alpha", game_app_id=570),
                CardCopy("beta-1", "Dota 2", "Dota Beta", market_hash_name="Dota Beta", game_app_id=570),
                CardCopy("beta-2", "Dota 2", "Dota Beta", market_hash_name="Dota Beta", game_app_id=570),
            ), True,
        )
        result = self.pipeline.prepare_recipe_order(
            order_id=order,
            snapshots={("Bot-A", 753, "6"): snapshot},
            recipient_badges=[],
            badge_data_verified=True,
            buyer_complete_sets_by_app_id={570: 0},
            buyer_inventory_verified=True,
            seller_inventory_verified=True,
            card_hashes_by_app_id={570: ("Dota Alpha", "Dota Beta")},
            card_definitions_verified=True,
        )
        self.assertTrue(result.reserved, result.message)
        self.assertEqual(len(result.reserved_assets), 4)
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_legs WHERE order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,),
            ).fetchone()[0], 1)

    def test_mapped_legacy_owner_is_excluded_but_foreign_legacy_reservation_blocks(self):
        recipe = (recipe_item(
            "cs2", 1, "steam_asset", 1,
            SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",)),
        ),)
        order, config_id = self.create_recipe_order("foreign-legacy-block", recipe)
        self.ledger.materialize_recipe_order_lines(order)
        line = self.line_rows(order)[0]
        store = AssetReservationStore(self.database.path)
        self.assertTrue(store.reserve("unrelated-order", [("Bot-A", "legacy-owned")])["reserved"])
        self.assertIn(("Bot-A", "legacy-owned"), store.list_reserved_assets(excluding_order_id=order))

        plan = {
            line["order_line_id"]: self.cs2_line_plan(
                self.recipe_item_id(config_id, line["line_number"]), ["legacy-owned"]
            )
        }
        with self.assertRaisesRegex(ValueError, "активный резерв"):
            self.pipeline.cs2_reservations.reserve_recipe_plan(
                order_id=order, config_version_id=config_id, planned_by_line=plan,
            )

        self.assert_no_reservation_commit(order)

    def test_optional_recipe_item_can_be_omitted_at_reservation_boundary(self):
        optional_text = RecipeItem(
            item_id="optional-text", position=1, kind="digital_text_unit", spec_version=1,
            quantity_per_order=1, required=False, stock_source_id="text-stock",
            executor_key="text_delivery", spec=DigitalTextUnitSpec(),
        )
        required_cs2 = recipe_item(
            "required-cs2", 2, "steam_asset", 1,
            SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",)),
        )
        order, config_id = self.create_recipe_order("boundary-optional", (optional_text, required_cs2))
        self.ledger.materialize_recipe_order_lines(order)
        line = next(row for row in self.line_rows(order) if row["item_type"] == "steam_asset")

        result = self.pipeline.cs2_reservations.reserve_recipe_plan(
            order_id=order, config_version_id=config_id,
            planned_by_line={line["order_line_id"]: self.cs2_line_plan(
                self.recipe_item_id(config_id, line["line_number"]), ["optional-cs2"]
            )},
        )

        self.assertTrue(result.reserved)
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_legs l JOIN gd_order_lines ol "
                "ON ol.order_line_id=l.order_line_id WHERE ol.order_id=? AND ol.item_type='digital_text_unit'",
                (order,),
            ).fetchone()[0], 0)

    def test_reservation_boundary_rejects_recipe_line_and_config_mismatches(self):
        order, config_id = self.create_recipe_order(
            "boundary-mismatch", (recipe_item(
                "cs2", 1, "steam_asset", 1,
                SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",)),
            ),),
        )
        self.ledger.materialize_recipe_order_lines(order)
        line = self.line_rows(order)[0]
        wrong_item_plan = {line["order_line_id"]: self.cs2_line_plan("different-item", ["mismatch-1"])}
        with self.assertRaisesRegex(ValueError, "RecipeItem"):
            self.pipeline.cs2_reservations.reserve_recipe_plan(
                order_id=order, config_version_id=config_id, planned_by_line=wrong_item_plan,
            )
        self.assert_no_reservation_commit(order)

        foreign_line = "unknown-order-line"
        with self.assertRaisesRegex(ValueError, "another order|другого заказа"):
            self.pipeline.cs2_reservations.reserve_recipe_plan(
                order_id=order, config_version_id=config_id,
                planned_by_line={foreign_line: self.cs2_line_plan("cs2", ["mismatch-2"])},
            )
        self.assert_no_reservation_commit(order)

        with self.assertRaisesRegex(ValueError, "ConfigVersion"):
            self.pipeline.cs2_reservations.reserve_recipe_plan(
                order_id=order, config_version_id="different-config",
                planned_by_line={line["order_line_id"]: self.cs2_line_plan("cs2", ["mismatch-3"])},
            )
        self.assert_no_reservation_commit(order)

        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_order_lines SET item_selector_json='{}' WHERE order_line_id=?",
                (line["order_line_id"],),
            )
        with self.assertRaisesRegex(ValueError, "snapshot"):
            self.pipeline.cs2_reservations.reserve_recipe_plan(
                order_id=order, config_version_id=config_id,
                planned_by_line={line["order_line_id"]: self.cs2_line_plan(
                    self.recipe_item_id(config_id, line["line_number"]), ["mismatch-4"]
                )},
            )
        self.assert_no_reservation_commit(order)

    def test_shortage_of_any_required_line_leaves_no_partial_reservations(self):
        order, _ = self.create_recipe_order("short", self.mixed_recipe())
        result = self.prepare(order, self.mixed_snapshots(cs2_count=4))
        self.assertEqual(result.status, "preflight_blocked")
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_delivery_legs WHERE order_line_id IN "
                                        "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM steam_asset_reservations WHERE order_id=?", (order,)).fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT status FROM gd_orders WHERE order_id=?", (order,)).fetchone()[0],
                "preflight_blocked",
            )

    def test_card_shortage_keeps_planned_cs2_assets_unreserved(self):
        order, _ = self.create_recipe_order("cards-short", self.mixed_recipe())
        snapshots = self.mixed_snapshots()
        snapshots[("Bot-A", 753, "6")] = InventorySnapshot(
            "Bot-A", "steam_cards", 753, "6", NOW, (), True,
        )
        result = self.prepare(order, snapshots)
        self.assertEqual(result.status, "preflight_blocked")
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_legs WHERE order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,),
            ).fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT status FROM gd_orders WHERE order_id=?", (order,)).fetchone()[0],
                "preflight_blocked",
            )

    def test_insert_failure_after_card_plan_rolls_back_every_resource_and_leg(self):
        order, _ = self.create_recipe_order("rollback", self.mixed_recipe())
        with self.database.connect() as db:
            db.execute("""
                CREATE TRIGGER fail_cs2_reservation
                BEFORE INSERT ON gd_resource_reservations
                WHEN NEW.game_id='cs2'
                BEGIN SELECT RAISE(ABORT,'synthetic reservation insert failure'); END
            """)
        with self.assertRaisesRegex(Exception, "synthetic reservation insert failure"):
            self.prepare(order)
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_delivery_legs WHERE order_line_id IN "
                                        "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM steam_asset_reservations WHERE order_id=?", (order,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM gd_orders WHERE order_id=?", (order,)).fetchone()[0], "pending")

    def test_recipe_plan_respects_legacy_reservations_in_the_shared_pool(self):
        order, _ = self.create_recipe_order("legacy-conflict", self.mixed_recipe(cs2_quantity=4))
        legacy = AssetReservationStore(self.database.path)
        result = legacy.reserve("manual-legacy-order", [("Bot-A", "cs2-1")])
        self.assertTrue(result["reserved"])
        prepared = self.prepare(order)
        self.assertTrue(prepared.reserved)
        self.assertNotIn(("Bot-A", "cs2-1"), set(prepared.reserved_assets))

    def test_two_orders_share_inventory_without_double_claiming(self):
        spec = (recipe_item("cs2", 1, "steam_asset", 3,
                            SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"},
                                           ("Bot-A", "Bot-B"))),)
        first, _ = self.create_recipe_order("pool-a", spec)
        second, _ = self.create_recipe_order("pool-b", spec)
        snapshots = {
            ("Bot-A", 730, "2"): InventorySnapshot("Bot-A", "cs2", 730, "2", NOW,
                tuple(self.cs2("Bot-A", f"shared-{i}") for i in range(3)), True),
            ("Bot-B", 730, "2"): InventorySnapshot("Bot-B", "cs2", 730, "2", NOW,
                tuple(self.cs2("Bot-B", f"shared-{i}") for i in range(3, 6)), True),
        }
        first_result = self.pipeline.prepare_recipe_order(order_id=first, snapshots=snapshots)
        second_result = self.pipeline.prepare_recipe_order(order_id=second, snapshots=snapshots)
        self.assertTrue(first_result.reserved)
        self.assertTrue(second_result.reserved)
        self.assertEqual(len(set(first_result.reserved_assets) & set(second_result.reserved_assets)), 0)
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active'"
            ).fetchone()[0], 6)

    def test_reprocessing_reserved_recipe_is_idempotent(self):
        order, _ = self.create_recipe_order("repeat", self.mixed_recipe())
        first = self.prepare(order)
        second = self.prepare(order)
        self.assertTrue(first.reserved)
        self.assertTrue(second.reserved)
        self.assertEqual(first.legs, second.legs)
        self.assertEqual(set(first.reserved_assets), set(second.reserved_assets))
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active'"
            ).fetchone()[0], 7)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_legs WHERE order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,),
            ).fetchone()[0], 3)

    def test_unknown_required_resource_fails_closed_and_optional_text_is_skipped(self):
        required_text = RecipeItem(
            item_id="text", position=1, kind="digital_text_unit", spec_version=1,
            quantity_per_order=1, required=True, stock_source_id="text-stock",
            executor_key="text_delivery", spec=DigitalTextUnitSpec(),
        )
        order, _ = self.create_recipe_order("text-required", (required_text,))
        result = self.pipeline.prepare_recipe_order(order_id=order, snapshots={})
        self.assertEqual(result.status, "preflight_blocked")
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active'").fetchone()[0], 0)

        optional_text = RecipeItem(
            item_id="optional-text", position=1, kind="digital_text_unit", spec_version=1,
            quantity_per_order=1, required=False, stock_source_id="text-stock",
            executor_key="text_delivery", spec=DigitalTextUnitSpec(),
        )
        required_cs2 = recipe_item(
            "required-cs2", 2, "steam_asset", 1,
            SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",)),
        )
        optional_order, _ = self.create_recipe_order("text-optional", (optional_text, required_cs2))
        snapshots = {
            ("Bot-A", 730, "2"): InventorySnapshot(
                "Bot-A", "cs2", 730, "2", NOW, (self.cs2("Bot-A", "optional-skip-cs2"),), True,
            ),
        }
        optional_result = self.pipeline.prepare_recipe_order(order_id=optional_order, snapshots=snapshots)
        self.assertTrue(optional_result.reserved)
        self.assertEqual(optional_result.reserved_assets, (("Bot-A", "optional-skip-cs2"),))
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active' AND order_line_id="
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=? AND item_type='digital_text_unit')",
                (optional_order,),
            ).fetchone()[0], 0)

        optional_only, _ = self.create_recipe_order("text-optional-only", (optional_text,))
        empty_plan = self.pipeline.prepare_recipe_order(order_id=optional_only, snapshots={})
        self.assertEqual(empty_plan.status, "preflight_blocked")
        self.assertIn("нет обязательных физических ресурсов", empty_plan.message)

    def test_parallel_orders_cannot_claim_the_same_assets(self):
        spec = (recipe_item(
            "cs2", 1, "steam_asset", 2,
            SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"}, ("Bot-A",)),
        ),)
        first, _ = self.create_recipe_order("parallel-a", spec)
        second, _ = self.create_recipe_order("parallel-b", spec)
        snapshots = {
            ("Bot-A", 730, "2"): InventorySnapshot(
                "Bot-A", "cs2", 730, "2", NOW,
                (self.cs2("Bot-A", "parallel-1"), self.cs2("Bot-A", "parallel-2")), True,
            ),
        }

        def prepare(order_id):
            return self.pipeline.prepare_recipe_order(order_id=order_id, snapshots=snapshots)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(prepare, (first, second)))
        self.assertEqual(sum(result.reserved for result in results), 1)
        self.assertEqual(sum(result.status == "preflight_blocked" for result in results), 1)
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active'"
            ).fetchone()[0], 2)

    def test_unknown_order_keeps_canonical_and_card_compatibility_reserves(self):
        order, _ = self.create_recipe_order(
            "unknown-retained", (recipe_item(
                "cards", 1, "steam_card_set", 1,
                SteamCardSetSpec(game_name="Dota 2", allowed_bot_pool=("Bot-A", "Bot-B")),
            ),),
        )
        result = self.prepare(order)
        self.assertTrue(result.reserved)
        with self.database.connect() as db:
            db.execute("UPDATE gd_orders SET status='UNKNOWN' WHERE order_id=?", (order,))
            db.execute("UPDATE gd_delivery_legs SET status='UNKNOWN' WHERE order_line_id IN "
                       "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,))
        with self.assertRaisesRegex(ValueError, "UNKNOWN/SENT/PROCESSING"):
            self.pipeline.cancel_order(order)
        with self.database.connect() as db:
            self.assertGreater(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active' AND order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,),
            ).fetchone()[0], 0)
            self.assertGreater(db.execute(
                "SELECT COUNT(*) FROM steam_asset_reservations WHERE order_id=?", (order,),
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
