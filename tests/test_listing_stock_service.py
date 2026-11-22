from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from core.delivery.listing_stock import InsufficientListingStock, ListingStockService
from core.delivery.stock_ledger import QuantityStockLedger, StockResource
from core.delivery.ledger import DeliveryLedger
from core.delivery.recipes import (
    DigitalTextUnitSpec, RecipeItem, SteamAssetSpec,
)
from games.inventory import InventoryItem
from core.delivery.stock_identity import StockItemIdentity


class ListingStockServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tmp.name) / "listing-stock.sqlite3")
        self.database.initialize()
        self.ledger = QuantityStockLedger(self.database)
        self.service = ListingStockService(self.database)
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO gd_listings(listing_id,source_platform,title) VALUES('listing-1','manual','Test')"
            )
            db.execute(
                """
                INSERT INTO gd_config_versions
                    (config_version_id,listing_id,version_number,delivery_quantity_per_deal,
                     item_selectors_json,bot_pool_json)
                VALUES('config-1','listing-1',1,6,'{}','[]')
                """
            )
            db.execute(
                """
                INSERT INTO gd_deals
                    (deal_id,source_platform,external_deal_id,listing_id,config_version_id,payload_json)
                VALUES('deal-1','playerok','external-1','listing-1','config-1','{}')
                """
            )
            db.execute(
                "INSERT INTO gd_orders(order_id,deal_id,total_quantity) VALUES('order-1','deal-1',6)"
            )
            db.execute(
                """
                INSERT INTO gd_order_lines
                    (order_line_id,order_id,config_version_id,line_number,item_type,item_name,
                     item_selector_json,quantity)
                VALUES('line-1','order-1','config-1',1,'cs2','Test item','{}',6)
                """
            )
        with self.database.connect() as db:
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gd_stock_transfers'"
            ).fetchone())
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gd_stock_allocation_batches'"
            ).fetchone())
        self.a = StockResource("cs2", 730, "2", "Bot-A", "1001")
        self.b = StockResource("cs2", 730, "2", "Bot-B", "2002")

    def tearDown(self):
        self.tmp.cleanup()

    def create_typed_recipe_order(self):
        delivery = DeliveryLedger(self.database)
        first = RecipeItem(
            "redline", 1, "steam_asset", 1, 4, True, None,
            "steam_trade_offer",
            SteamAssetSpec(
                "cs2", 730, "2", {"market_hash_name": "AK-47 | Redline"},
                ("Bot-A",),
            ),
        )
        second = RecipeItem(
            "dragon", 2, "steam_asset", 1, 2, True, None,
            "steam_trade_offer",
            SteamAssetSpec(
                "cs2", 730, "2", {"market_hash_name": "AWP | Dragon Lore"},
                ("Bot-B",),
            ),
        )
        text_item = RecipeItem(
            "text", 3, "digital_text_unit", 1, 1, True,
            "text-stock", "text_delivery", DigitalTextUnitSpec(),
        )
        config = delivery.create_recipe_config_version(
            listing_id="typed-listing", source_platform="manual",
            external_listing_id="typed-external",
            recipe_items=(first, second, text_item),
        )
        delivery.create_deal(
            deal_id="typed-deal", source_platform="manual",
            external_deal_id="typed-external", chat_id=None,
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            platform_status="manual",
        )
        delivery.create_order(order_id="typed-order", deal_id="typed-deal", total_quantity=7)
        lines = delivery.materialize_recipe_order_lines("typed-order")
        line_by_item = {
            json.loads(line["item_selector_json"])["recipe_item_id"]: line
            for line in lines
        }
        recipe_items = config["recipe_items"]
        self.assertEqual([row["kind"] for row in recipe_items],
                         ["steam_asset", "steam_asset", "digital_text_unit"])
        return config, recipe_items, line_by_item

    def observe_typed_inventory(self, *, include_dragon=True):
        items = [
            InventoryItem(
                "cs2", "Bot-A", "1001", "class-redline", "0", "AK-47 | Redline",
                item_type="Rifle", market_hash_name="AK-47 | Redline",
                is_tradable=True, amount=5,
            ),
            # Matching name on a disallowed bot must never enter the first RecipeItem.
            InventoryItem(
                "cs2", "Bot-B", "1002", "class-redline-2", "0", "AK-47 | Redline",
                item_type="Rifle", market_hash_name="AK-47 | Redline",
                is_tradable=True, amount=9,
            ),
        ]
        if include_dragon:
            items.append(InventoryItem(
                "cs2", "Bot-B", "2001", "class-dragon", "0", "AWP | Dragon Lore",
                item_type="Sniper Rifle", market_hash_name="AWP | Dragon Lore",
                is_tradable=True, amount=3,
            ))
        return [
            self.ledger.observe(
                StockResource("cs2", 730, "2", item.bot_name, item.asset_id),
                item.amount,
                identity=StockItemIdentity(
                    "cs2", 730, "2", market_hash_name=item.market_hash_name,
                    name=item.name, item_type=item.item_type, class_id=item.class_id,
                    quality=item.quality, exterior=item.exterior, rarity=item.rarity,
                ),
                is_tradable=item.is_tradable,
            )
            for item in items
        ]

    def test_release_listing_units_splits_only_listing_claim_and_is_idempotent(self):
        self.ledger.observe(self.a, 10)
        self.service.allocate_listing(
            request_id="release-base", listing_id="listing-1",
            config_version_id="config-1", units=8, resources=[self.a],
        )
        self.service.transfer_to_order(
            transfer_event_id="release-order-transfer", listing_id="listing-1",
            config_version_id="config-1", order_line_id="line-1", units=3,
        )

        first = self.service.release_listing_units(
            adjustment_id="release-two", listing_id="listing-1",
            config_version_id="config-1", units=2,
        )
        repeated = self.service.release_listing_units(
            adjustment_id="release-two", listing_id="listing-1",
            config_version_id="config-1", units=2,
        )
        self.assertEqual(
            {row["allocation_id"] for row in first},
            {row["allocation_id"] for row in repeated},
        )
        with self.database.connect() as db:
            listing_units = db.execute(
                "SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations "
                "WHERE owner_type='listing' AND listing_id='listing-1' "
                "AND config_version_id='config-1' AND state='active'"
            ).fetchone()[0]
            order_units = db.execute(
                "SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations "
                "WHERE owner_type='order' AND order_line_id='line-1' AND state='active'"
            ).fetchone()[0]
            adjustment_count = db.execute(
                "SELECT COUNT(*) FROM gd_stock_listing_adjustments WHERE adjustment_id='release-two'"
            ).fetchone()[0]
        self.assertEqual(listing_units, 3)
        self.assertEqual(order_units, 3)
        self.assertEqual(adjustment_count, 1)
        self.assertEqual(self.ledger.free_units(self.a), 4)

    def test_disable_listing_releases_only_listing_owned_units_and_marks_status(self):
        self.ledger.observe(self.a, 10)
        self.service.allocate_listing(
            request_id="disable-base", listing_id="listing-1",
            config_version_id="config-1", units=8, resources=[self.a],
        )
        self.service.transfer_to_order(
            transfer_event_id="disable-order-transfer", listing_id="listing-1",
            config_version_id="config-1", order_line_id="line-1", units=3,
        )

        released = self.service.disable_listing_stock(
            adjustment_id="disable-generation", listing_id="listing-1",
            config_version_id="config-1",
        )
        self.assertTrue(released)
        with self.database.connect() as db:
            status = db.execute(
                "SELECT status FROM gd_listings WHERE listing_id='listing-1'"
            ).fetchone()[0]
            listing_units = db.execute(
                "SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations "
                "WHERE owner_type='listing' AND listing_id='listing-1' "
                "AND config_version_id='config-1' AND state='active'"
            ).fetchone()[0]
            order_row = db.execute(
                "SELECT units,state FROM gd_stock_allocations "
                "WHERE owner_type='order' AND order_line_id='line-1' AND state='active'"
            ).fetchone()
        self.assertEqual(status, "disabled")
        self.assertEqual(listing_units, 0)
        self.assertEqual((order_row["units"], order_row["state"]), (3, "active"))
        self.assertEqual(self.ledger.free_units(self.a), 7)

    def test_partial_release_of_stale_allocation_fails_without_changing_claim(self):
        self.ledger.observe(self.a, 8)
        self.service.allocate_listing(
            request_id="partial-stale-base", listing_id="listing-1",
            config_version_id="config-1", units=5, resources=[self.a],
        )
        self.ledger.observe_snapshot("cs2", "Bot-A", [])
        with self.assertRaisesRegex(ValueError, "частично изменять"):
            self.service.release_listing_units(
                adjustment_id="partial-stale-release", listing_id="listing-1",
                config_version_id="config-1", units=2,
            )
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT state,units FROM gd_stock_allocations "
                "WHERE owner_type='listing' AND listing_id='listing-1' "
                "AND config_version_id='config-1'"
            ).fetchall()
        self.assertEqual([(row["state"], row["units"]) for row in rows], [("active", 5)])

    def test_release_listing_units_can_free_a_stale_inventory_observation(self):
        self.ledger.observe(self.a, 8)
        self.service.allocate_listing(
            request_id="stale-release-base", listing_id="listing-1",
            config_version_id="config-1", units=5, resources=[self.a],
        )
        self.ledger.observe_snapshot("cs2", "Bot-A", [])
        with self.database.connect() as db:
            state = db.execute(
                "SELECT availability_state FROM gd_stock_observations WHERE observation_id=?",
                (self.ledger._observation_id(self.a),),
            ).fetchone()[0]
        self.assertEqual(state, "unavailable")

        released = self.service.release_listing_units(
            adjustment_id="stale-release", listing_id="listing-1",
            config_version_id="config-1", units=5,
        )
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["state"], "released")
        with self.database.connect() as db:
            active_listing_units = db.execute(
                "SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations "
                "WHERE owner_type='listing' AND state='active' AND listing_id='listing-1'"
            ).fetchone()[0]
        self.assertEqual(active_listing_units, 0)

    def test_disabled_listing_generation_rejects_new_allocations(self):
        self.ledger.observe(self.a, 5)
        self.service.allocate_listing(
            request_id="disabled-base", listing_id="listing-1",
            config_version_id="config-1", units=4, resources=[self.a],
        )
        self.service.disable_listing_stock(
            adjustment_id="disabled-generation", listing_id="listing-1",
            config_version_id="config-1",
        )
        with self.assertRaisesRegex(ValueError, "отключено"):
            self.service.allocate_listing(
                request_id="disabled-new-allocation", listing_id="listing-1",
                config_version_id="config-1", units=1, resources=[self.a],
            )

    def test_disable_listing_with_no_listing_units_still_disables_generation(self):
        result = self.service.disable_listing_stock(
            adjustment_id="disable-empty-generation", listing_id="listing-1",
            config_version_id="config-1",
        )
        self.assertEqual(result, [])
        with self.database.connect() as db:
            status, auto_relist = db.execute(
                "SELECT status,auto_relist FROM gd_listings WHERE listing_id='listing-1'"
            ).fetchone()
            audit_units = db.execute(
                "SELECT units FROM gd_stock_listing_adjustments "
                "WHERE adjustment_id='disable-empty-generation'"
            ).fetchone()[0]
        self.assertEqual((status, auto_relist, audit_units), ("disabled", 0, 0))

    def test_disable_listing_fails_closed_when_listing_has_unknown_allocations(self):
        self.ledger.observe(self.a, 10)
        self.service.allocate_listing(
            request_id="disable-unknown-base", listing_id="listing-1",
            config_version_id="config-1", units=5, resources=[self.a],
        )
        from games.inventory import InventoryItem
        self.ledger.observe_snapshot("cs2", "Bot-B", [
            InventoryItem(
                "cs2", "Bot-B", "2002", "class-unknown", "0",
                "AWP | Unknown Listing Item", market_hash_name="AWP | Unknown Listing Item",
                is_tradable=True, amount=2,
            )
        ])
        self.ledger.allocate(
            allocation_id="listing-unknown", resource=self.b, units=2,
            owner_type="listing", listing_id="listing-1", config_version_id="config-1",
            state="unknown",
        )
        with self.assertRaisesRegex(ValueError, "pending/UNKNOWN/conflict"):
            self.service.disable_listing_stock(
                adjustment_id="disable-unknown", listing_id="listing-1",
                config_version_id="config-1",
            )
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT status FROM gd_listings WHERE listing_id='listing-1'"
            ).fetchone()[0], "active")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_stock_listing_adjustments WHERE adjustment_id='disable-unknown'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT state FROM gd_stock_allocations WHERE allocation_id='listing-unknown'"
            ).fetchone()[0], "unknown")

    def test_typed_release_is_scoped_to_one_steam_asset_recipe_item(self):
        config, recipe_items, _ = self.create_typed_recipe_order()
        self.observe_typed_inventory()
        first_id, second_id = [item["item_id"] for item in recipe_items[:2]]
        self.service.allocate_recipe_items(
            request_id="typed-release-base", listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            units_by_recipe_item={first_id: 4, second_id: 2},
        )
        self.service.release_listing_units(
            adjustment_id="typed-release-redline", listing_id=config["listing_id"],
            config_version_id=config["config_version_id"], recipe_item_id=first_id, units=2,
        )
        with self.database.connect() as db:
            allocations = {
                row["recipe_item_id"]: int(row["units"])
                for row in db.execute(
                    "SELECT recipe_item_id,SUM(units) AS units FROM gd_stock_allocations "
                    "WHERE owner_type='listing' AND listing_id=? AND config_version_id=? "
                    "AND state='active' GROUP BY recipe_item_id",
                    (config["listing_id"], config["config_version_id"]),
                ).fetchall()
            }
        self.assertEqual(allocations, {first_id: 2, second_id: 2})

    def test_typed_recipe_allocation_and_transfer_are_pinned_to_exact_recipe_item(self):
        config, recipe_items, lines = self.create_typed_recipe_order()
        self.observe_typed_inventory()
        first_id, second_id, text_id = [item["item_id"] for item in recipe_items]
        rows = self.service.allocate_recipe_items(
            request_id="typed-allocation-1",
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            units_by_recipe_item={first_id: 4, second_id: 2},
        )
        self.assertEqual({row["recipe_item_id"] for row in rows}, {first_id, second_id})
        self.assertEqual(
            {row["recipe_item_id"]: sum(r["units"] for r in rows if r["recipe_item_id"] == row["recipe_item_id"])
             for row in rows},
            {first_id: 4, second_id: 2},
        )
        repeated = self.service.allocate_recipe_items(
            request_id="typed-allocation-1",
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            units_by_recipe_item={first_id: 4, second_id: 2},
        )
        self.assertEqual(
            {row["allocation_id"] for row in repeated},
            {row["allocation_id"] for row in rows},
        )
        with self.assertRaisesRegex(ValueError, "recipe_item_id"):
            self.service.transfer_to_order(
                transfer_event_id="typed-paid-missing-id",
                listing_id=config["listing_id"],
                config_version_id=config["config_version_id"],
                order_line_id=lines[first_id]["order_line_id"], units=4,
            )
        with self.assertRaisesRegex(ValueError, "OrderLine не закреплена"):
            self.service.transfer_to_order(
                transfer_event_id="typed-paid-cross-line",
                listing_id=config["listing_id"],
                config_version_id=config["config_version_id"],
                order_line_id=lines[first_id]["order_line_id"],
                recipe_item_id=second_id, units=2,
            )
        first_transfer = self.service.transfer_to_order(
            transfer_event_id="typed-paid-redline",
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            order_line_id=lines[first_id]["order_line_id"],
            recipe_item_id=first_id, units=4,
        )
        repeated_transfer = self.service.transfer_to_order(
            transfer_event_id="typed-paid-redline",
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            order_line_id=lines[first_id]["order_line_id"],
            recipe_item_id=first_id, units=4,
        )
        self.assertEqual(
            {row["allocation_id"] for row in first_transfer},
            {row["allocation_id"] for row in repeated_transfer},
        )
        self.assertEqual({row["recipe_item_id"] for row in first_transfer}, {first_id})
        second_transfer = self.service.transfer_to_order(
            transfer_event_id="typed-paid-dragon",
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            order_line_id=lines[second_id]["order_line_id"],
            recipe_item_id=second_id, units=1,
        )
        self.assertEqual({row["recipe_item_id"] for row in second_transfer}, {second_id})
        with self.database.connect() as db:
            remaining = {
                row["recipe_item_id"]: int(row["units"])
                for row in db.execute(
                    "SELECT recipe_item_id,SUM(units) AS units FROM gd_stock_allocations "
                    "WHERE owner_type='listing' AND state='active' AND config_version_id=? "
                    "GROUP BY recipe_item_id",
                    (config["config_version_id"],),
                ).fetchall()
            }
            text_transfer_count = db.execute(
                "SELECT COUNT(*) FROM gd_stock_transfers WHERE order_line_id=?",
                (lines[text_id]["order_line_id"],),
            ).fetchone()[0]
            first_order_ids = db.execute(
                "SELECT DISTINCT recipe_item_id FROM gd_stock_allocations "
                "WHERE owner_type='order' AND order_line_id=? AND state='active'",
                (lines[first_id]["order_line_id"],),
            ).fetchall()
        # All 4 Redline units allocated to the listing were transferred;
        # one additional unit remains free in the physical stack, not listing-owned.
        self.assertEqual(remaining, {second_id: 1})
        self.assertEqual(
            self.ledger.free_units(StockResource("cs2", 730, "2", "Bot-A", "1001")),
            1,
        )
        self.assertEqual(text_transfer_count, 0)
        self.assertEqual({row[0] for row in first_order_ids}, {first_id})

    def test_typed_recipe_allocation_is_all_or_nothing_across_positions(self):
        config, recipe_items, _ = self.create_typed_recipe_order()
        self.observe_typed_inventory(include_dragon=False)
        first_id, second_id = [item["item_id"] for item in recipe_items[:2]]
        with self.assertRaises(InsufficientListingStock):
            self.service.allocate_recipe_items(
                request_id="typed-shortage",
                listing_id=config["listing_id"],
                config_version_id=config["config_version_id"],
                units_by_recipe_item={first_id: 4, second_id: 2},
            )
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_stock_allocation_batches WHERE request_id='typed-shortage'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_stock_allocations WHERE config_version_id=?",
                (config["config_version_id"],),
            ).fetchone()[0], 0)

    def test_generic_listing_allocator_rejects_typed_recipe_without_item_binding(self):
        config, recipe_items, _ = self.create_typed_recipe_order()
        self.observe_typed_inventory()
        with self.assertRaisesRegex(ValueError, "allocate_recipe_items"):
            self.service.allocate_listing(
                request_id="typed-generic-forbidden",
                listing_id=config["listing_id"],
                config_version_id=config["config_version_id"],
                units=1,
                resources=[self.a],
            )

    def test_listing_allocation_is_exact_deterministic_and_transfer_is_idempotent(self):
        self.ledger.observe(self.a, 5)
        self.ledger.observe(self.b, 5)
        rows = self.service.allocate_listing(
            request_id="allocation-request-1", listing_id="listing-1",
            config_version_id="config-1", units=8, resources=[self.b, self.a],
        )
        self.assertEqual(sum(row["units"] for row in rows), 8)
        self.assertEqual([(row["bot_name"], row["units"]) for row in rows],
                         [("Bot-A", 5), ("Bot-B", 3)])
        repeated_allocation = self.service.allocate_listing(
            request_id="allocation-request-1", listing_id="listing-1",
            config_version_id="config-1", units=8, resources=[self.a, self.b],
        )
        self.assertEqual(
            {row["allocation_id"] for row in repeated_allocation},
            {row["allocation_id"] for row in rows},
        )
        with self.assertRaises(ValueError):
            self.service.allocate_listing(
                request_id="allocation-request-1", listing_id="listing-1",
                config_version_id="config-1", units=7, resources=[self.a, self.b],
            )

        transferred = self.service.transfer_to_order(
            transfer_event_id="paid-event-1", listing_id="listing-1",
            config_version_id="config-1", order_line_id="line-1", units=6,
        )
        self.assertEqual(sum(row["units"] for row in transferred), 6)
        self.assertEqual([(row["bot_name"], row["units"]) for row in transferred],
                         [("Bot-A", 5), ("Bot-B", 1)])
        repeated = self.service.transfer_to_order(
            transfer_event_id="paid-event-1", listing_id="listing-1",
            config_version_id="config-1", order_line_id="line-1", units=6,
        )
        self.assertEqual(
            {row["allocation_id"] for row in repeated},
            {row["allocation_id"] for row in transferred},
        )
        with self.database.connect() as db:
            active_listing_units = db.execute(
                """
                SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations
                WHERE owner_type='listing' AND listing_id='listing-1'
                  AND config_version_id='config-1' AND state='active'
                """
            ).fetchone()[0]
            order_units = db.execute(
                """
                SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations
                WHERE owner_type='order' AND order_line_id='line-1' AND state='active'
                """
            ).fetchone()[0]
            transfer_events = db.execute(
                "SELECT COUNT(*) FROM gd_stock_transfers WHERE order_line_id='line-1'"
            ).fetchone()[0]
        self.assertEqual(active_listing_units, 2)
        self.assertEqual(order_units, 6)
        self.assertEqual(transfer_events, 1)
        self.assertEqual(self.ledger.free_units(self.a), 0)
        # Bot-B had 5 units, of which only 3 were assigned to the listing.
        # Transferring 1 to the order must preserve the other 2 as listing stock
        # and leave the remaining 2 units of the physical stack free.
        self.assertEqual(self.ledger.free_units(self.b), 2)
        with self.database.connect() as db:
            remainder_units = db.execute(
                """
                SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations
                WHERE owner_type='listing' AND bot_name='Bot-B'
                  AND listing_id='listing-1' AND config_version_id='config-1'
                  AND state='active'
                """
            ).fetchone()[0]
        self.assertEqual(remainder_units, 2)

    def test_insufficient_listing_stock_leaves_no_partial_batch_or_allocations(self):
        self.ledger.observe(self.a, 3)
        self.ledger.observe(self.b, 2)
        with self.assertRaises(InsufficientListingStock) as caught:
            self.service.allocate_listing(
                request_id="not-enough", listing_id="listing-1",
                config_version_id="config-1", units=6, resources=[self.a, self.b],
            )
        self.assertEqual((caught.exception.required, caught.exception.available), (6, 5))
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_stock_allocation_batches"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_stock_allocations"
            ).fetchone()[0], 0)

    def test_transfer_cannot_exceed_order_line_quantity_or_reuse_event_id(self):
        self.ledger.observe(self.a, 10)
        self.service.allocate_listing(
            request_id="allocation-2", listing_id="listing-1",
            config_version_id="config-1", units=10, resources=[self.a],
        )
        self.service.transfer_to_order(
            transfer_event_id="paid-event-2", listing_id="listing-1",
            config_version_id="config-1", order_line_id="line-1", units=4,
        )
        with self.assertRaisesRegex(ValueError, "превышает количество"):
            self.service.transfer_to_order(
                transfer_event_id="paid-event-3", listing_id="listing-1",
                config_version_id="config-1", order_line_id="line-1", units=3,
            )
        with self.assertRaisesRegex(ValueError, "другими параметрами"):
            self.service.transfer_to_order(
                transfer_event_id="paid-event-2", listing_id="listing-1",
                config_version_id="config-1", order_line_id="line-1", units=5,
            )
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_stock_transfers WHERE order_line_id='line-1'"
            ).fetchone()[0], 1)


    def test_transfer_can_join_callers_transaction_and_rolls_back_with_it(self):
        self.ledger.observe(self.a, 4)
        self.service.allocate_listing(
            request_id="allocation-caller-tx", listing_id="listing-1",
            config_version_id="config-1", units=4, resources=[self.a],
        )
        with self.assertRaisesRegex(RuntimeError, "rollback caller"):
            with self.database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                transferred = self.service.transfer_to_order(
                    transfer_event_id="paid-event-caller-tx", listing_id="listing-1",
                    config_version_id="config-1", order_line_id="line-1",
                    units=3, connection=db,
                )
                self.assertEqual(sum(row["units"] for row in transferred), 3)
                raise RuntimeError("rollback caller")
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_stock_transfers WHERE transfer_event_id='paid-event-caller-tx'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations "
                "WHERE owner_type='listing' AND listing_id='listing-1' "
                "AND config_version_id='config-1' AND state='active'"
            ).fetchone()[0], 4)
            self.assertEqual(db.execute(
                "SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations "
                "WHERE owner_type='order' AND order_line_id='line-1' AND state='active'"
            ).fetchone()[0], 0)

        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            transferred = self.service.transfer_to_order(
                transfer_event_id="paid-event-caller-tx", listing_id="listing-1",
                config_version_id="config-1", order_line_id="line-1",
                units=3, connection=db,
            )
            self.assertEqual(sum(row["units"] for row in transferred), 3)
            db.commit()
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COALESCE(SUM(units),0) FROM gd_stock_allocations "
                "WHERE owner_type='order' AND order_line_id='line-1' AND state='active'"
            ).fetchone()[0], 3)



if __name__ == "__main__":
    unittest.main()
