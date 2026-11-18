from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.recipes import (
    DigitalTextUnitSpec,
    Recipe,
    RecipeItem,
    RecipeValidationError,
    SteamAssetSpec,
    SteamCardSetSpec,
    UnsupportedLegacyRecipeError,
    legacy_config_to_recipe,
)


def item(item_id: str, position: int, kind: str, quantity: int, spec, *, stock_source_id=None):
    return RecipeItem(
        item_id=item_id,
        position=position,
        kind=kind,
        spec_version=1,
        quantity_per_order=quantity,
        required=True,
        stock_source_id=stock_source_id,
        executor_key={
            "steam_card_set": "steam_trade_offer",
            "steam_asset": "steam_trade_offer",
            "digital_text_unit": "text_delivery",
        }[kind],
        spec=spec,
    )


class RecipeModelTests(unittest.TestCase):
    def test_valid_steam_cards_cs2_and_future_text_item(self):
        cards = item("cards", 1, "steam_card_set", 1,
                     SteamCardSetSpec(game_name="Example", allowed_bot_pool=("Bot-A",)))
        cs2 = item("cs2", 2, "steam_asset", 5,
                   SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "Case"}, ("Bot-A",)))
        text = item("text", 3, "digital_text_unit", 1, DigitalTextUnitSpec(),
                    stock_source_id="text-stock-1")
        recipe = Recipe((text, cards, cs2))
        self.assertEqual([row.item_id for row in recipe.items], ["cards", "cs2", "text"])
        self.assertEqual(recipe.items[1].quantity_per_order, 5)
        self.assertEqual(recipe.items[2].stock_source_id, "text-stock-1")

    def test_recipe_accepts_multiple_independent_positions(self):
        recipe = Recipe((
            item("one", 1, "steam_card_set", 1, SteamCardSetSpec(allowed_bot_pool=("Bot-A",))),
            item("two", 2, "steam_asset", 5,
                 SteamAssetSpec("cs2", 730, "2", {"name": "Widget"}, ("Bot-A",))),
        ))
        self.assertEqual(len(recipe.items), 2)

    def test_unknown_type_and_unknown_spec_fields_are_rejected(self):
        base = {
            "item_id": "x", "position": 1, "kind": "unknown", "spec_version": 1,
            "quantity_per_order": 1, "required": True, "stock_source_id": None,
            "executor_key": "unknown", "spec": {},
        }
        with self.assertRaises(RecipeValidationError):
            RecipeItem.from_dict(base)
        card = item("cards", 1, "steam_card_set", 1,
                    SteamCardSetSpec(allowed_bot_pool=("Bot-A",))).to_dict()
        card["spec"]["execute_python"] = "not allowed"
        with self.assertRaises(RecipeValidationError):
            RecipeItem.from_dict(card)

    def test_invalid_quantities_and_required_types_are_rejected(self):
        with self.assertRaises(RecipeValidationError):
            item("bad", 1, "steam_asset", True,
                 SteamAssetSpec("cs2", 730, "2", {"name": "Widget"}, ("Bot-A",)))
        raw = item("x", 1, "digital_text_unit", 1, DigitalTextUnitSpec(),
                   stock_source_id="codes").to_dict()
        raw["required"] = 1
        with self.assertRaises(RecipeValidationError):
            RecipeItem.from_dict(raw)

    def test_steam_asset_context_id_rejects_non_string_json_types(self):
        raw = item("x", 1, "steam_asset", 1,
                   SteamAssetSpec("cs2", 730, "2", {"name": "Widget"}, ("Bot-A",))).to_dict()
        for invalid in (True, False, 2, 2.0, [], {}):
            with self.subTest(value=invalid):
                invalid_raw = json.loads(json.dumps(raw))
                invalid_raw["spec"]["context_id"] = invalid
                with self.assertRaises(RecipeValidationError):
                    RecipeItem.from_dict(invalid_raw)
        self.assertEqual(RecipeItem.from_dict(raw).spec.context_id, "2")

    def test_recipe_selector_is_immutable_and_invalid_recipe_object_is_revalidated(self):
        recipe = Recipe((item(
            "asset", 1, "steam_asset", 1,
            SteamAssetSpec("cs2", 730, "2", {"name": "Widget"}, ("Bot-A",)),
        ),))
        selector = recipe.items[0].spec.selector
        self.assertIsInstance(selector, MappingProxyType)
        with self.assertRaises(TypeError):
            selector["unknown"] = "not allowed"
        serialized = recipe.to_list()
        serialized[0]["spec"]["selector"]["name"] = "changed copy"
        self.assertEqual(recipe.items[0].spec.selector["name"], "Widget")

        # Even an object corrupted through Python's low-level escape hatch must
        # be rejected before a ConfigVersion is inserted.
        object.__setattr__(recipe.items[0].spec, "selector", {"unknown": "bad"})
        with self.assertRaises(RecipeValidationError):
            Recipe.from_values(recipe)
        with self.assertRaises(RecipeValidationError):
            Recipe((recipe.items[0],))
        with self.assertRaises(RecipeValidationError):
            Recipe.from_values([recipe.items[0]])

    def test_bot_pool_normalization_is_symmetric_and_rejects_empty_or_duplicates(self):
        with self.assertRaises(RecipeValidationError):
            SteamAssetSpec("cs2", 730, "2", {"name": "Widget"}, ("Bot-A", " bot-a "))
        for pool in (("",), ("   ",), ("Bot-A", "bot-a")):
            with self.subTest(pool=pool), self.assertRaises(RecipeValidationError):
                SteamCardSetSpec(allowed_bot_pool=pool)
        spec = SteamCardSetSpec(allowed_bot_pool=(" Bot-A ", "Bot-B"))
        self.assertEqual(spec.allowed_bot_pool, ("Bot-A", "Bot-B"))
        self.assertEqual(SteamCardSetSpec(**spec.to_dict()).allowed_bot_pool,
                         ("Bot-A", "Bot-B"))

    def test_legacy_adapter_converts_only_unambiguous_single_items(self):
        cards = legacy_config_to_recipe({
            "delivery_quantity_per_deal": 2,
            "item_selectors": {"delivery_mode": "steam_cards", "app_id": 753,
                               "context_id": 6, "selection": "buyer_badges_and_complete_sets"},
            "bot_pool": ["Bot-A"],
        })
        self.assertEqual(cards.items[0].kind, "steam_card_set")
        self.assertEqual(cards.items[0].quantity_per_order, 2)
        with self.assertRaises(RecipeValidationError):
            legacy_config_to_recipe({
                "delivery_quantity_per_deal": 1,
                "item_selectors": {"delivery_mode": "steam_cards", "app_id": 753,
                                   "context_id": 6, "selection": "unknown_strategy"},
                "bot_pool": ["Bot-A"],
            })
        with self.assertRaises(UnsupportedLegacyRecipeError):
            legacy_config_to_recipe({
                "delivery_quantity_per_deal": 1,
                "item_selectors": {"item_type": "steam_card_set", "delivery_type": "steam_asset"},
                "bot_pool": ["Bot-A"],
            })
        with self.assertRaises(UnsupportedLegacyRecipeError):
            legacy_config_to_recipe({
                "delivery_quantity_per_deal": 5,
                "item_selectors": {"delivery_mode": "cs2_items", "app_id": 730,
                                   "context_id": 2, "items": [
                                       {"name": "A"}, {"name": "B"},
                                   ]},
                "bot_pool": ["Bot-A"],
            })


class RecipeLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "recipes.sqlite3")
        self.ledger = DeliveryLedger(self.database)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def mixed_items(cs2_quantity: int = 5):
        return (
            item("cards", 1, "steam_card_set", 1,
                 SteamCardSetSpec(game_name="Example", allowed_bot_pool=("Bot-A",))),
            item("cs2", 2, "steam_asset", cs2_quantity,
                 SteamAssetSpec("cs2", 730, "2", {"market_hash_name": "Case"}, ("Bot-A",))),
            item("text", 3, "digital_text_unit", 1, DigitalTextUnitSpec(),
                 stock_source_id="text-stock-1"),
        )

    def make_order(self, config):
        self.ledger.create_deal(
            deal_id="deal-1", source_platform="manual", external_deal_id="deal-1",
            chat_id=None, listing_id=config["listing_id"],
            config_version_id=config["config_version_id"], platform_status="manual",
        )
        self.ledger.create_order(order_id="order-1", deal_id="deal-1", total_quantity=7)

    def test_invalid_recipe_object_is_rejected_before_config_version_write(self):
        recipe = Recipe((item(
            "asset", 1, "steam_asset", 1,
            SteamAssetSpec("cs2", 730, "2", {"name": "Widget"}, ("Bot-A",)),
        ),))
        object.__setattr__(recipe.items[0].spec, "selector", {"unknown": "bad"})
        with self.assertRaises(RecipeValidationError):
            self.ledger.create_recipe_config_version(
                listing_id="invalid-recipe", source_platform="playerok",
                external_listing_id="invalid-recipe", recipe_items=recipe,
            )
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_config_versions WHERE listing_id='invalid-recipe'"
            ).fetchone()[0], 0)

    def test_config_version_items_are_persisted_and_immutable(self):
        config = self.ledger.create_recipe_config_version(
            listing_id="listing-1", source_platform="playerok", external_listing_id="canonical-1",
            recipe_items=self.mixed_items(), title="Mixed",
        )
        self.assertIsNone(config["delivery_quantity_per_deal"])
        with self.database.connect() as db:
            version = db.execute(
                "SELECT recipe_schema_version,recipe_items_json FROM gd_config_versions "
                "WHERE config_version_id=?", (config["config_version_id"],)
            ).fetchone()
            raw_items = json.loads(version["recipe_items_json"])
            self.assertEqual(version["recipe_schema_version"], 1)
            self.assertEqual([(row["position"], row["kind"], row["quantity_per_order"])
                              for row in raw_items], [
                (1, "steam_card_set", 1), (2, "steam_asset", 5), (3, "digital_text_unit", 1),
            ])
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE gd_config_versions SET recipe_items_json='[]' WHERE config_version_id=?",
                           (config["config_version_id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM gd_config_versions WHERE config_version_id=?",
                           (config["config_version_id"],))

    def test_recipe_update_creates_new_config_version(self):
        first = self.ledger.create_recipe_config_version(
            listing_id="listing-1", source_platform="playerok", external_listing_id="canonical-1",
            recipe_items=self.mixed_items(),
        )
        second = self.ledger.create_recipe_config_version(
            listing_id="listing-1", source_platform="playerok", external_listing_id="canonical-1",
            recipe_items=self.mixed_items(cs2_quantity=6),
        )
        self.assertEqual((first["version_number"], second["version_number"]), (1, 2))
        with self.database.connect() as db:
            old = db.execute(
                "SELECT recipe_items_json FROM gd_config_versions WHERE config_version_id=?",
                (first["config_version_id"],)
            ).fetchone()
        self.assertEqual(next(row["quantity_per_order"] for row in json.loads(old[0])
                              if row["kind"] == "steam_asset"), 5)

    def test_recipe_expands_to_version_pinned_order_lines_once(self):
        first = self.ledger.create_recipe_config_version(
            listing_id="listing-1", source_platform="playerok", external_listing_id="canonical-1",
            recipe_items=self.mixed_items(),
        )
        self.make_order(first)
        lines = self.ledger.materialize_recipe_order_lines("order-1")
        repeated = self.ledger.materialize_recipe_order_lines("order-1")
        self.assertEqual(len(lines), 3)
        self.assertEqual([line["line_number"] for line in lines], [1, 2, 3])
        self.assertEqual([line["quantity"] for line in lines], [1, 5, 1])
        self.assertEqual([line["order_line_id"] for line in lines], [
            line["order_line_id"] for line in repeated
        ])
        for line in lines:
            selector = json.loads(line["item_selector_json"])
            self.assertEqual(selector["config_version_id"], first["config_version_id"])
            self.assertEqual(line["config_version_id"], first["config_version_id"])

        second = self.ledger.create_recipe_config_version(
            listing_id="listing-1", source_platform="playerok", external_listing_id="canonical-1",
            recipe_items=self.mixed_items(cs2_quantity=9),
        )
        with self.database.connect() as db:
            bound = db.execute(
                "SELECT d.config_version_id FROM gd_orders o JOIN gd_deals d ON d.deal_id=o.deal_id "
                "WHERE o.order_id='order-1'"
            ).fetchone()[0]
        self.assertEqual(bound, first["config_version_id"])
        self.assertNotEqual(bound, second["config_version_id"])

    def test_legacy_config_is_read_without_rewriting_and_unknown_is_rejected(self):
        legacy = self.ledger.create_listing_config_version(
            listing_id="legacy-1", source_platform="playerok", external_listing_id="legacy-canonical",
            delivery_quantity_per_deal=2,
            item_selectors={"delivery_mode": "steam_cards", "app_id": 753,
                            "context_id": 6, "selection": "buyer_badges_and_complete_sets"},
            bot_pool=["Bot-A"],
        )
        with self.database.connect() as db:
            before = tuple(db.execute(
                "SELECT delivery_quantity_per_deal,item_selectors_json,bot_pool_json "
                "FROM gd_config_versions WHERE config_version_id=?", (legacy["config_version_id"],)
            ).fetchone())
            converted = self.ledger.recipe_for_config_version(db, legacy["config_version_id"])
            after = tuple(db.execute(
                "SELECT delivery_quantity_per_deal,item_selectors_json,bot_pool_json "
                "FROM gd_config_versions WHERE config_version_id=?", (legacy["config_version_id"],)
            ).fetchone())
        self.assertEqual(converted.items[0].kind, "steam_card_set")
        self.assertEqual(before, after)
        with self.assertRaises(UnsupportedLegacyRecipeError):
            legacy_config_to_recipe({"delivery_quantity_per_deal": 1,
                                     "item_selectors": {"delivery_mode": "manual", "auto_messages": {}},
                                     "bot_pool": []})

    def test_existing_incompatible_order_lines_are_not_overwritten(self):
        config = self.ledger.create_recipe_config_version(
            listing_id="listing-1", source_platform="playerok", external_listing_id="canonical-1",
            recipe_items=self.mixed_items(),
        )
        self.make_order(config)
        self.ledger.add_order_line(
            order_line_id="legacy-line", order_id="order-1", line_number=1,
            item_type="legacy", item_name="Existing", item_selector={"old": True}, quantity=1,
        )
        with self.assertRaises(ValueError):
            self.ledger.materialize_recipe_order_lines("order-1")
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_order_lines WHERE order_id='order-1'"
            ).fetchone()[0], 1)

    def test_namespaced_item_ids_are_checked_before_config_version_is_saved(self):
        listing_id = "playerok-listing:" + "a" * 32
        max_item_id = "x" * 64
        config = self.ledger.create_recipe_config_version(
            listing_id=listing_id, source_platform="playerok", external_listing_id="long-id",
            recipe_items=[item(
                max_item_id, 1, "steam_asset", 1,
                SteamAssetSpec("cs2", 730, "2", {"name": "Widget"}, ("Bot-A",)),
            )],
        )
        stored_id = config["recipe_items"][0]["item_id"]
        self.assertEqual(len(stored_id), 128)
        with self.database.connect() as db:
            self.assertEqual(self.ledger.recipe_for_config_version(
                db, config["config_version_id"], allow_legacy=False
            ).items[0].item_id, stored_id)

        before = self.ledger.database.connect()
        try:
            versions_before = before.execute(
                "SELECT COUNT(*) FROM gd_config_versions WHERE listing_id=?", (listing_id,)
            ).fetchone()[0]
        finally:
            before.close()
        with self.assertRaises(RecipeValidationError):
            self.ledger.create_recipe_config_version(
                listing_id=listing_id, source_platform="playerok", external_listing_id="long-id",
                recipe_items=[item(
                    "x" * 65, 1, "steam_asset", 1,
                    SteamAssetSpec("cs2", 730, "2", {"name": "Widget"}, ("Bot-A",)),
                )],
            )
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_config_versions WHERE listing_id=?", (listing_id,)
            ).fetchone()[0], versions_before)

        distinct = self.ledger.create_recipe_config_version(
            listing_id="distinct-ids", source_platform="playerok", external_listing_id="distinct-ids",
            recipe_items=[
                item("first", 1, "steam_asset", 1,
                     SteamAssetSpec("cs2", 730, "2", {"name": "A"}, ("Bot-A",))),
                item("second", 2, "steam_asset", 1,
                     SteamAssetSpec("cs2", 730, "2", {"name": "B"}, ("Bot-A",))),
            ],
        )
        ids = [row["item_id"] for row in distinct["recipe_items"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_transactional_materializer_rejects_config_version_from_another_order(self):
        first = self.ledger.create_recipe_config_version(
            listing_id="listing-1", source_platform="playerok", external_listing_id="listing-1",
            recipe_items=self.mixed_items(),
        )
        self.make_order(first)
        second = self.ledger.create_recipe_config_version(
            listing_id="listing-2", source_platform="playerok", external_listing_id="listing-2",
            recipe_items=self.mixed_items(),
        )
        self.ledger.create_deal(
            deal_id="deal-2", source_platform="manual", external_deal_id="deal-2",
            chat_id=None, listing_id=second["listing_id"],
            config_version_id=second["config_version_id"], platform_status="manual",
        )
        self.ledger.create_order(order_id="order-2", deal_id="deal-2", total_quantity=7)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "не принадлежит сделке"):
                self.ledger.materialize_recipe_order_lines_in_transaction(
                    db, "order-1", second["config_version_id"]
                )
            db.rollback()
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM gd_order_lines WHERE order_id='order-1'"
            ).fetchone()[0], 0)
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(KeyError):
                self.ledger.materialize_recipe_order_lines_in_transaction(
                    db, "missing-order", first["config_version_id"]
                )
            db.rollback()


if __name__ == "__main__":
    unittest.main()
