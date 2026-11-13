from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.order_pipeline import DeliveryOrderPipeline, NO_CARDS_MESSAGE
from core.delivery.reconciliation import DeliveryReconciliation
from core.delivery.reservation_service import InventorySnapshot
from games.cs2.inventory_view import (
    CS2InventoryInstance, RestrictionStatus, RestrictionType,
)
from games.steam_cards.recipient_badge import RecipientBadge
from games.steam_cards.selection import CardCopy
from integrations.playerok.sync import PlayerokDealSyncService


NOW = datetime.now(timezone.utc)


class DeliveryOrderPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "pipeline.sqlite3")
        self.ledger = DeliveryLedger(self.database)
        self.pipeline = DeliveryOrderPipeline(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def create_order(self, *, quantity, selectors, bots, suffix):
        config = self.ledger.create_listing_config_version(
            listing_id="listing-" + suffix,
            source_platform="playerok",
            external_listing_id="external-" + suffix,
            delivery_quantity_per_deal=quantity,
            item_selectors=selectors,
            bot_pool=bots,
            title="Configured listing " + suffix,
        )
        self.ledger.create_deal(
            deal_id="deal-" + suffix,
            source_platform="playerok",
            external_deal_id="external-deal-" + suffix,
            chat_id="chat-" + suffix,
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            platform_status="PAID",
        )
        self.ledger.create_order(
            order_id="order-" + suffix,
            deal_id="deal-" + suffix,
            total_quantity=quantity,
        )
        return "order-" + suffix, config["config_version_id"]

    def test_card_sets_use_quantity_badge_formula_shared_bots_and_canonical_legs(self):
        order, _ = self.create_order(
            quantity=2,
            selectors={
                "game_id": "steam_cards", "app_id": 753, "context_id": 6,
                "item_type": "steam_card_set",
            },
            bots=["Bot-A", "Bot-B"],
            suffix="cards",
        )
        inventories = {
            "Bot-A": [
                CardCopy("dota-a", "Dota 2", "Dota Alpha", market_hash_name="Dota Alpha"),
                CardCopy("rust-a", "Rust", "Rust Alpha", market_hash_name="Rust Alpha"),
            ],
            "Bot-B": [
                CardCopy("dota-b", "Dota 2", "Dota Beta", market_hash_name="Dota Beta"),
                CardCopy("rust-b", "Rust", "Rust Beta", market_hash_name="Rust Beta"),
            ],
        }
        result = self.pipeline.prepare_steam_card_order(
            order_id=order,
            inventories=inventories,
            recipient_badges=[RecipientBadge("Dota 2", 4), RecipientBadge("Rust", 3)],
            badge_data_verified=True,
            buyer_complete_sets_by_app_id={570: 0, 252490: 1},
            buyer_inventory_verified=True,
            seller_inventory_verified=True,
            card_hashes_by_app_id={
                570: ("Dota Alpha", "Dota Beta"),
                252490: ("Rust Alpha", "Rust Beta"),
            },
            card_definitions_verified=True,
        )
        self.assertTrue(result.ready_for_manual_review)
        self.assertEqual(sum(allocation.sets for allocation in result.allocations), 2)
        with self.database.connect() as db:
            lines = db.execute(
                "SELECT item_type,item_name,quantity,item_selector_json FROM gd_order_lines "
                "WHERE order_id=? ORDER BY line_number", (order,)
            ).fetchall()
            legs = db.execute(
                "SELECT bot_name,app_id,context_id,asset_ids_json FROM gd_delivery_legs "
                "WHERE order_line_id IN (SELECT order_line_id FROM gd_order_lines WHERE order_id=?)",
                (order,),
            ).fetchall()
            reservations = db.execute(
                "SELECT bot_name,asset_id,state FROM gd_resource_reservations "
                "WHERE order_line_id IN (SELECT order_line_id FROM gd_order_lines WHERE order_id=?)",
                (order,),
            ).fetchall()
            state = db.execute("SELECT status FROM gd_orders WHERE order_id=?", (order,)).fetchone()[0]
        self.assertEqual(state, "reserved")
        self.assertEqual([(row["item_type"], row["quantity"]) for row in lines], [
            ("steam_card_set", 1), ("steam_card_set", 1),
        ])
        self.assertEqual(len(legs), 4, "each game's complete set is split into its two source bots")
        self.assertEqual({(row["app_id"], row["context_id"]) for row in legs}, {(753, "6")})
        self.assertEqual(len(reservations), 4)
        self.assertTrue(all(row["state"] == "active" for row in reservations))
        self.assertEqual(
            {row["asset_id"] for row in reservations},
            {"dota-a", "dota-b", "rust-a", "rust-b"},
        )


    def test_card_sets_accepts_verified_app_ids_from_asf_for_games_outside_static_catalog(self):
        order, _ = self.create_order(
            quantity=1,
            selectors={
                "game_id": "steam_cards", "app_id": 753, "context_id": 6,
                "item_type": "steam_card_set",
            },
            bots=["Bot-A", "Bot-B"],
            suffix="dynamic-card-app",
        )
        result = self.pipeline.prepare_steam_card_order(
            order_id=order,
            inventories={
                "Bot-A": [CardCopy(
                    "custom-a", "Community Game", "Community Alpha",
                    market_hash_name="Community Alpha", game_app_id=990001,
                )],
                "Bot-B": [CardCopy(
                    "custom-b", "Community Game", "Community Beta",
                    market_hash_name="Community Beta", game_app_id=990001,
                )],
            },
            recipient_badges=[RecipientBadge("Community Game", 4)],
            badge_data_verified=True,
            buyer_complete_sets_by_app_id={990001: 0},
            buyer_inventory_verified=True,
            seller_inventory_verified=True,
            card_hashes_by_app_id={990001: ("Community Alpha", "Community Beta")},
            card_definitions_verified=True,
        )
        self.assertTrue(result.ready_for_manual_review)
        with self.database.connect() as db:
            line = db.execute(
                "SELECT item_name,quantity,item_selector_json FROM gd_order_lines WHERE order_id=?",
                (order,),
            ).fetchone()
            legs = db.execute(
                "SELECT app_id,context_id,bot_name,asset_ids_json FROM gd_delivery_legs "
                "WHERE order_line_id IN (SELECT order_line_id FROM gd_order_lines WHERE order_id=?)",
                (order,),
            ).fetchall()
        self.assertEqual((line["item_name"], line["quantity"]), ("Community Game", 1))
        self.assertEqual({(row["app_id"], row["context_id"]) for row in legs}, {(753, "6")})
        self.assertEqual({row["bot_name"] for row in legs}, {"Bot-A", "Bot-B"})

    def test_card_shortage_creates_exact_buyer_and_telegram_events_without_partial_reserve(self):
        order, _ = self.create_order(
            quantity=2,
            selectors={
                "game_id": "steam_cards", "app_id": 753, "context_id": 6,
                "item_type": "steam_card_set",
            },
            bots=["Bot-A", "Bot-B"],
            suffix="short-cards",
        )
        result = self.pipeline.prepare_steam_card_order(
            order_id=order,
            inventories={
                "Bot-A": [CardCopy("only-a", "Dota 2", "Dota Alpha", market_hash_name="Dota Alpha")],
                "Bot-B": [CardCopy("only-b", "Dota 2", "Dota Beta", market_hash_name="Dota Beta")],
            },
            recipient_badges=[RecipientBadge("Dota 2", 4)],
            badge_data_verified=True,
            buyer_complete_sets_by_app_id={570: 0},
            buyer_inventory_verified=True,
            seller_inventory_verified=True,
            card_hashes_by_app_id={570: ("Dota Alpha", "Dota Beta")},
            card_definitions_verified=True,
        )
        self.assertFalse(result.ready_for_manual_review)
        with self.database.connect() as db:
            state = db.execute("SELECT status FROM gd_orders WHERE order_id=?", (order,)).fetchone()[0]
            line_count = db.execute("SELECT COUNT(*) FROM gd_order_lines WHERE order_id=?", (order,)).fetchone()[0]
            reserve_count = db.execute("SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active'").fetchone()[0]
            events = {
                row["event_type"]: json.loads(row["payload_json"])
                for row in db.execute("SELECT event_type,payload_json FROM gd_delivery_events WHERE order_id=?", (order,))
            }
            queued = db.execute(
                "SELECT deal_id,chat_id,message_text,status FROM gd_buyer_message_outbox"
            ).fetchone()
        self.assertEqual(state, "insufficient_stock")
        self.assertEqual(line_count, 0)
        self.assertEqual(reserve_count, 0)
        self.assertEqual(events["buyer_notification"]["message"], NO_CARDS_MESSAGE)
        self.assertIn("insufficient_card_sets", events["telegram_notification"]["event"])
        self.assertEqual(
            tuple(queued),
            ("deal-short-cards", "chat-short-cards", NO_CARDS_MESSAGE, "pending"),
        )

    @staticmethod
    def cs2_item(bot, asset_id, hash_name="AK-47 | Redline"):
        return CS2InventoryInstance(
            game_id="cs2", appid=730, context_id=2, bot_name=bot,
            asset_id=asset_id, class_id="100", instance_id="0", amount=1,
            name=hash_name, item_type="Rifle", market_hash_name=hash_name,
            is_tradable=True, is_marketable=True,
            restriction_type=RestrictionType.NONE,
            restriction_status=RestrictionStatus.AVAILABLE,
            last_inventory_refresh=NOW,
        )

    def test_order_is_delivered_only_after_every_steam_leg_is_accepted_and_enqueues_playerok(self):
        PlayerokDealSyncService(self.database)
        config = self.ledger.create_listing_config_version(
            listing_id="listing-reconcile",
            source_platform="playerok",
            external_listing_id="listing-reconcile",
            delivery_quantity_per_deal=2,
            item_selectors={"game_id": "cs2", "app_id": 730, "context_id": 2,
                            "item_type": "cs2_item", "market_hash_name": "AK-47 | Redline"},
            bot_pool=["Bot-A", "Bot-B"],
            title="Reconcile listing",
        )
        self.ledger.create_deal(
            deal_id="deal-reconcile",
            source_platform="playerok",
            external_deal_id="external-deal-reconcile",
            chat_id="chat-reconcile",
            listing_id=config["listing_id"],
            config_version_id=config["config_version_id"],
            platform_status="PAID",
        )
        self.ledger.create_order(order_id="order-reconcile", deal_id="deal-reconcile", total_quantity=2)
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_deals SET playerok_account_id='seller-1' WHERE deal_id='deal-reconcile'"
            )
            db.execute(
                "INSERT INTO gd_order_lines "
                "(order_line_id,order_id,line_number,item_type,item_name,item_selector_json,quantity) "
                "VALUES('line-reconcile','order-reconcile',1,'cs2_item','AK-47','{}',2)"
            )
            db.executemany(
                "INSERT INTO gd_delivery_legs "
                "(leg_id,order_line_id,bot_name,app_id,context_id,asset_ids_json,trade_offer_id,status) "
                "VALUES(?,?,?,?,?,?,?,'SENT_TO_STEAM')",
                [
                    ("leg-a", "line-reconcile", "Bot-A", 730, "2", '["1001"]', "91001"),
                    ("leg-b", "line-reconcile", "Bot-B", 730, "2", '["2001"]', "91002"),
                ],
            )

        class FakeASF:
            statuses = {
                ("Bot-A", "91001"): {"success": True, "found": True, "status": "Accepted"},
                ("Bot-B", "91002"): {"success": True, "found": True, "status": "Active"},
            }

            def get_trade_offer_status(self, *, bot_name, trade_offer_id):
                return self.statuses[(bot_name, trade_offer_id)]

        reconciler = DeliveryReconciliation(
            self.database, FakeASF(), sent_offers_path=Path(self.temp.name) / "sent_offers.json"
        )
        reconciler.reconcile(order_id="order-reconcile")
        with self.database.connect() as db:
            status = db.execute(
                "SELECT status FROM gd_orders WHERE order_id='order-reconcile'"
            ).fetchone()[0]
            outbox_count = db.execute(
                "SELECT COUNT(*) FROM playerok_status_outbox WHERE order_id='order-reconcile'"
            ).fetchone()[0]
        self.assertNotEqual(status, "DELIVERED")
        self.assertEqual(outbox_count, 0)

        FakeASF.statuses[("Bot-B", "91002")]["status"] = "Accepted"
        reconciler.reconcile(order_id="order-reconcile")
        with self.database.connect() as db:
            status = db.execute(
                "SELECT status FROM gd_orders WHERE order_id='order-reconcile'"
            ).fetchone()[0]
            outbox = db.execute(
                "SELECT state,external_deal_id FROM playerok_status_outbox WHERE order_id='order-reconcile'"
            ).fetchone()
        self.assertEqual(status, "DELIVERED")
        self.assertEqual(tuple(outbox), ("pending", "external-deal-reconcile"))

    def test_cs2_config_selector_730_2_reserves_exact_quantity(self):
        order, _ = self.create_order(
            quantity=2,
            selectors={
                "game_id": "cs2", "app_id": 730, "context_id": 2,
                "item_type": "cs2_item", "market_hash_name": "AK-47 | Redline",
            },
            bots=["Bot-A", "Bot-B"],
            suffix="cs2",
        )
        snapshots = {
            "Bot-A": InventorySnapshot(
                "Bot-A", "cs2", 730, "2", NOW,
                (self.cs2_item("Bot-A", "1001"),), True,
            ),
            "Bot-B": InventorySnapshot(
                "Bot-B", "cs2", 730, "2", NOW,
                (self.cs2_item("Bot-B", "2001"), self.cs2_item("Bot-B", "2002")), True,
            ),
        }
        result = self.pipeline.prepare_cs2_order(order_id=order, snapshots=snapshots)
        self.assertEqual(result.status, "reserved")
        self.assertEqual(set(result.reserved_assets), {
            ("Bot-A", "1001"), ("Bot-B", "2001"),
        })
        with self.database.connect() as db:
            lines = db.execute(
                "SELECT item_type,quantity,item_selector_json FROM gd_order_lines WHERE order_id=?",
                (order,),
            ).fetchall()
            legs = db.execute(
                "SELECT bot_name,app_id,context_id,asset_ids_json FROM gd_delivery_legs "
                "WHERE order_line_id IN (SELECT order_line_id FROM gd_order_lines WHERE order_id=?)",
                (order,),
            ).fetchall()
            active = db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active' AND order_line_id "
                "IN (SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,)
            ).fetchone()[0]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["item_type"], "cs2_item")
        self.assertEqual(lines[0]["quantity"], 2)
        self.assertEqual({(row["app_id"],row["context_id"]) for row in legs}, {(730,"2")})
        self.assertEqual(active, 2)


if __name__ == "__main__":
    unittest.main()
