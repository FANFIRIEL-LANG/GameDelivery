from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.reservation_service import (
    DeliveryReservationService,
    InventorySnapshot,
)
from games.cs2.inventory_view import (
    CS2InventoryInstance,
    RestrictionStatus,
    RestrictionType,
)
from games.steam_cards.selection import CardCopy
from games.steam_cards.recipient_badge import RecipientBadge


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


class DeliveryReservationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "delivery.sqlite3")
        self.ledger = DeliveryLedger(self.database)
        self.service = DeliveryReservationService(self.database)
        self._counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def create_order(self, *, quantity, selectors, bot_pool, prefix="order"):
        self._counter += 1
        suffix = f"{prefix}-{self._counter}"
        config = self.ledger.create_listing_config_version(
            listing_id=f"listing-{suffix}",
            source_platform="playerok",
            external_listing_id=f"external-listing-{suffix}",
            delivery_quantity_per_deal=quantity,
            item_selectors=selectors,
            bot_pool=bot_pool,
            title="Reservation test",
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
            order_id=suffix,
            deal_id=f"deal-{suffix}",
            total_quantity=quantity,
        )
        return suffix, config["config_version_id"]

    @staticmethod
    def cs2_item(bot, asset_id, *, tradable=True, restriction=RestrictionType.NONE,
                 restriction_status=RestrictionStatus.AVAILABLE):
        return CS2InventoryInstance(
            game_id="cs2",
            appid=730,
            context_id=2,
            bot_name=bot,
            asset_id=str(asset_id),
            class_id="100",
            instance_id="0",
            amount=1,
            name="AK-47 | Redline",
            item_type="Rifle",
            market_hash_name="AK-47 | Redline",
            is_tradable=tradable,
            is_marketable=True,
            restriction_type=restriction,
            restriction_status=restriction_status,
            last_inventory_refresh=NOW,
        )

    def snapshot(self, bot, game, app, context, items):
        return InventorySnapshot(
            bot_name=bot,
            game_id=game,
            app_id=app,
            context_id=str(context),
            refreshed_at=NOW,
            items=tuple(items),
            asf_verified=True,
        )

    def test_cs2_order_is_split_into_legs_for_two_bots(self):
        order, config_id = self.create_order(
            quantity=3,
            selectors={
                "game_id": "cs2", "app_id": 730, "context_id": 2,
                "market_hash_name": "AK-47 | Redline",
            },
            bot_pool=["Bot-A", "Bot-B"],
        )
        snapshots = {
            "Bot-A": self.snapshot("Bot-A", "cs2", 730, 2, [
                self.cs2_item("Bot-A", "1001"),
                self.cs2_item("Bot-A", "1002"),
            ]),
            "Bot-B": self.snapshot("Bot-B", "cs2", 730, 2, [
                self.cs2_item("Bot-B", "2001"),
                self.cs2_item("Bot-B", "2002"),
            ]),
        }

        result = self.service.reserve_order(
            order_id=order, config_version_id=config_id, snapshots=snapshots, now=NOW
        )

        self.assertTrue(result.reserved)
        self.assertEqual(len(result.legs), 2)
        self.assertEqual(
            {(leg.bot_name, leg.app_id, leg.context_id, leg.asset_ids) for leg in result.legs},
            {
                ("Bot-A", 730, "2", ("1001", "1002")),
                ("Bot-B", 730, "2", ("2001",)),
            },
        )
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active'"
            ).fetchone()[0], 3)

    def test_card_set_uses_existing_owned_set_selection_and_splits_across_bots(self):
        order, config_id = self.create_order(
            quantity=1,
            selectors={
                "game_id": "steam_cards", "app_id": 753, "context_id": 6,
                "item_type": "steam_card_set", "game_name": "Example Game",
            },
            bot_pool=["Cards-A", "Cards-B"],
            prefix="cards",
        )
        snapshots = {
            "Cards-A": self.snapshot("Cards-A", "steam_cards", 753, 6, [
                CardCopy("card-a", "Example Game", "Card A", is_tradable=True),
            ]),
            "Cards-B": self.snapshot("Cards-B", "steam_cards", 753, 6, [
                CardCopy("card-b", "Example Game", "Card B", is_tradable=True),
            ]),
        }

        result = self.service.reserve_order(
            order_id=order, config_version_id=config_id, snapshots=snapshots, now=NOW,
            recipient_badges=[RecipientBadge("Example Game", 0)],
            badge_data_verified=True,
        )

        self.assertTrue(result.reserved)
        self.assertEqual(len(result.legs), 2)
        self.assertEqual(
            {(leg.bot_name, leg.app_id, leg.context_id, leg.asset_ids) for leg in result.legs},
            {
                ("Cards-A", 753, "6", ("card-a",)),
                ("Cards-B", 753, "6", ("card-b",)),
            },
        )

    def test_parallel_orders_cannot_reserve_same_physical_asset(self):
        first, config_id = self.create_order(
            quantity=1,
            selectors={
                "game_id": "cs2", "app_id": 730, "context_id": 2,
                "market_hash_name": "AK-47 | Redline",
            },
            bot_pool=["Bot-A"],
            prefix="parallel",
        )
        with self.database.connect() as db:
            listing_id = db.execute(
                "SELECT listing_id FROM gd_config_versions WHERE config_version_id=?",
                (config_id,),
            ).fetchone()[0]
        self.ledger.create_deal(
            deal_id="parallel-second-deal",
            source_platform="playerok",
            external_deal_id="parallel-second-external",
            chat_id="parallel-second-chat",
            listing_id=listing_id,
            config_version_id=config_id,
            platform_status="PAID",
        )
        self.ledger.create_order(
            order_id="parallel-second-order",
            deal_id="parallel-second-deal",
            total_quantity=1,
        )
        second = "parallel-second-order"
        snapshots = {
            "Bot-A": self.snapshot("Bot-A", "cs2", 730, 2, [
                self.cs2_item("Bot-A", "same-asset"),
            ])
        }

        def reserve(order_id):
            return self.service.reserve_order(
                order_id=order_id, config_version_id=config_id,
                snapshots=snapshots, now=NOW,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, (first, second)))

        self.assertEqual(sorted(result.status for result in results), ["insufficient_stock", "reserved"])
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active' "
                "AND bot_name='Bot-A' AND asset_id='same-asset'"
            ).fetchone()[0], 1)

    def test_false_unknown_and_trade_locked_cs2_assets_are_rejected(self):
        order, config_id = self.create_order(
            quantity=2,
            selectors={
                "game_id": "cs2", "app_id": 730, "context_id": 2,
                "market_hash_name": "AK-47 | Redline",
            },
            bot_pool=["Bot-A"],
            prefix="untradable",
        )
        snapshots = {
            "Bot-A": self.snapshot("Bot-A", "cs2", 730, 2, [
                self.cs2_item("Bot-A", "tradable", tradable=True),
                self.cs2_item("Bot-A", "false", tradable=False),
                self.cs2_item(
                    "Bot-A", "unknown",
                    restriction=RestrictionType.UNKNOWN,
                    restriction_status=RestrictionStatus.UNKNOWN,
                ),
                self.cs2_item(
                    "Bot-A", "locked",
                    restriction=RestrictionType.TRADE_PROTECTION,
                    restriction_status=RestrictionStatus.RESTRICTED,
                ),
            ])
        }

        result = self.service.reserve_order(
            order_id=order, config_version_id=config_id, snapshots=snapshots, now=NOW
        )

        self.assertEqual(result.status, "insufficient_stock")
        self.assertEqual(result.reserved_assets, ())
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_legs WHERE order_line_id IN "
                "(SELECT order_line_id FROM gd_order_lines WHERE order_id=?)", (order,)
            ).fetchone()[0], 0)

    def test_insufficient_stock_rolls_back_all_reservations_and_legs_and_queues_notifications(self):
        order, config_id = self.create_order(
            quantity=2,
            selectors={
                "game_id": "cs2", "app_id": 730, "context_id": 2,
                "market_hash_name": "AK-47 | Redline",
            },
            bot_pool=["Bot-A", "Bot-B"],
            prefix="short",
        )
        snapshots = {
            "Bot-A": self.snapshot("Bot-A", "cs2", 730, 2, [
                self.cs2_item("Bot-A", "only-one"),
            ]),
            "Bot-B": self.snapshot("Bot-B", "cs2", 730, 2, []),
        }

        result = self.service.reserve_order(
            order_id=order, config_version_id=config_id, snapshots=snapshots, now=NOW
        )

        self.assertEqual(result.status, "insufficient_stock")
        self.assertEqual(result.legs, ())
        self.assertEqual(result.reserved_assets, ())
        self.assertEqual(len(result.notification_event_ids), 2)
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT status FROM gd_orders WHERE order_id=?", (order,)
            ).fetchone()[0], "insufficient_stock")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_resource_reservations WHERE state='active'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_legs"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_delivery_events WHERE order_id=? AND status='pending'",
                (order,),
            ).fetchone()[0], 2)

    def test_stale_asf_snapshot_is_not_eligible(self):
        order, config_id = self.create_order(
            quantity=1,
            selectors={
                "game_id": "cs2", "app_id": 730, "context_id": 2,
                "market_hash_name": "AK-47 | Redline",
            },
            bot_pool=["Bot-A"],
            prefix="stale",
        )
        stale = InventorySnapshot(
            bot_name="Bot-A", game_id="cs2", app_id=730, context_id="2",
            refreshed_at=datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc),
            items=(self.cs2_item("Bot-A", "old-asset"),),
            asf_verified=True,
        )
        result = self.service.reserve_order(
            order_id=order, config_version_id=config_id,
            snapshots={"Bot-A": stale}, now=NOW,
        )
        self.assertEqual(result.status, "insufficient_stock")
        self.assertEqual(result.reserved_assets, ())


if __name__ == "__main__":
    unittest.main()
