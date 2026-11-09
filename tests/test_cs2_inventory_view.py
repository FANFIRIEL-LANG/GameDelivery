import unittest
from dataclasses import replace
from datetime import datetime, timezone

from integrations.asf.client import ASFError
from games.cs2.inventory_view import (
    adapt_cs2_inventory_response, build_cs2_inventory_projection,
)


def payload(bot="Bot A", *, amount="2", tradable=True, marketable=False):
    asset = {"appid": 730, "contextid": 2, "assetid": "a1", "classid": "c1",
             "instanceid": "i1", "amount": amount}
    desc = {"classid": "c1", "instanceid": "i1", "name": "AK", "type": "Rifle",
            "market_hash_name": "AK | Redline", "tradable": tradable, "marketable": marketable}
    return {"Result": {bot: {"Assets": [asset], "Descriptions": [desc]}}}


class CS2AdapterTests(unittest.TestCase):
    def test_preserves_source_fields_and_flags_independently(self):
        item = adapt_cs2_inventory_response(payload(), "Bot A")[0]
        self.assertEqual((item.appid, item.context_id, item.asset_id, item.class_id,
                          item.instance_id, item.amount), (730, 2, "a1", "c1", "i1", 2))
        self.assertEqual((item.is_tradable, item.is_marketable), (True, False))
        self.assertIsNone(item.tradable_after)
        self.assertEqual((item.market_tradable_restriction, item.market_marketable_restriction), (None, None))

    def test_visual_metadata_is_immutable_and_preserved(self):
        data = payload()
        desc = data["Result"]["Bot A"]["Descriptions"][0]
        desc.update({"icon_url": "icon", "icon_url_large": "large",
                     "tags": [{"localized_tag_name": "Rifle"}]})
        item = adapt_cs2_inventory_response(data, "Bot A")[0]
        self.assertEqual((item.icon_url, item.icon_url_large, item.tags), ("icon", "large", ("Rifle",)))

    def test_missing_amount_stays_unknown_and_context_is_not_mixed(self):
        data = payload(); del data["Result"]["Bot A"]["Assets"][0]["amount"]
        self.assertIsNone(adapt_cs2_inventory_response(data, "Bot A")[0].amount)
        with self.assertRaises(ValueError):
            adapt_cs2_inventory_response(payload(), "Bot A", context_id=16)

    def test_null_invalid_and_zero_amounts_are_not_rewritten(self):
        for raw in (None, "bad", -1, 1.5, True):
            with self.subTest(amount=raw):
                self.assertIsNone(adapt_cs2_inventory_response(payload(amount=raw), "Bot A")[0].amount)
        zero = adapt_cs2_inventory_response(payload(amount=0), "Bot A")[0]
        positive = adapt_cs2_inventory_response(payload("Bot B", amount="3"), "Bot B")[0]
        view = build_cs2_inventory_projection([zero, positive])
        self.assertEqual((zero.amount, view.known_amount_sum, view.unknown_amount_instances,
                          view.total_amount), (0, 3, 0, 3))

    def test_asf_scope_and_asset_scope_reject_730_16(self):
        data = payload()
        data["Result"]["Bot A"]["Assets"][0]["contextid"] = 16
        self.assertEqual(adapt_cs2_inventory_response(data, "Bot A"), ())
        for kwargs in ({"appid": 570}, {"context_id": 16}, {"appid": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                adapt_cs2_inventory_response(payload(), "Bot A", **kwargs)

    def test_asset_level_730_16_is_not_mixed_even_if_description_is_missing(self):
        data = payload()
        data["Result"]["Bot A"]["Assets"][0].update({"appid": 730, "contextid": 16, "classid": "other"})
        self.assertEqual(adapt_cs2_inventory_response(data, "Bot A"), ())

    def test_missing_or_conflicting_description_fails_closed(self):
        data = payload(); data["Result"]["Bot A"]["Descriptions"] = []
        with self.assertRaises(ASFError): adapt_cs2_inventory_response(data, "Bot A")

    def test_identical_duplicate_descriptions_are_unambiguous(self):
        data = payload()
        data["Result"]["Bot A"]["Descriptions"].append(
            dict(data["Result"]["Bot A"]["Descriptions"][0]))
        self.assertEqual(len(adapt_cs2_inventory_response(data, "Bot A")), 1)

    def test_duplicate_asset_and_invalid_identity_fields_fail_closed(self):
        data = payload()
        data["Result"]["Bot A"]["Assets"].append(dict(data["Result"]["Bot A"]["Assets"][0]))
        with self.assertRaises(ASFError): adapt_cs2_inventory_response(data, "Bot A")
        data = payload(); data["Result"]["Bot A"]["Assets"][0]["assetid"] = ["bad"]
        with self.assertRaises(ASFError): adapt_cs2_inventory_response(data, "Bot A")

    def test_malformed_success_flag_fails_closed(self):
        data = payload(); data["Success"] = False
        with self.assertRaises(ASFError): adapt_cs2_inventory_response(data, "Bot A")

    def test_assets_with_same_description_identity_remain_physical_instances(self):
        data = payload()
        data["Result"]["Bot A"]["Assets"].append({**data["Result"]["Bot A"]["Assets"][0], "assetid": "a2"})
        items = adapt_cs2_inventory_response(data, "Bot A")
        self.assertEqual({i.asset_id for i in items}, {"a1", "a2"})
        data = payload(); data["Result"]["Bot A"]["Descriptions"].append(
            {"classid": "c1", "instanceid": "i1", "name": "Other"})
        with self.assertRaises(ASFError): adapt_cs2_inventory_response(data, "Bot A")


class CS2ProjectionTests(unittest.TestCase):
    def test_same_asset_on_different_bots_is_distinct_and_grouped(self):
        a = adapt_cs2_inventory_response(payload("Bot A"), "Bot A")[0]
        b = replace(a, bot_name="Bot B")
        view = build_cs2_inventory_projection([a, b])
        self.assertEqual(len(view.instances), 2)
        self.assertEqual((view.groups[0].instance_count, view.groups[0].unique_bot_count,
                          view.groups[0].known_amount_sum, view.groups[0].total_amount), (2, 2, 4, 4))

    def test_duplicate_and_conflicting_physical_keys_rejected(self):
        a = adapt_cs2_inventory_response(payload(), "Bot A")[0]
        with self.assertRaises(ValueError): build_cs2_inventory_projection([a, a])
        with self.assertRaises(ValueError): build_cs2_inventory_projection([a, replace(a, amount=3)])

    def test_unknown_amount_and_restrictions_do_not_become_available(self):
        a = adapt_cs2_inventory_response(payload(amount=None, tradable=False), "Bot A")[0]
        projection = build_cs2_inventory_projection([a])
        self.assertEqual(projection.total_amount, None)
        self.assertEqual(projection.unknown_amount_instances, 1)
        self.assertIsNone(a.tradable_after)
        self.assertEqual(a.restriction_type.value, "unknown")
        self.assertFalse(a.is_reservable)

    def test_direct_models_reject_negative_amount(self):
        item = adapt_cs2_inventory_response(payload(), "Bot A")[0]
        with self.assertRaises(ValueError):
            replace(item, amount=-1)

    def test_elapsed_timestamp_does_not_mutate_source_flags_or_create_deadline(self):
        data = payload(amount=1, tradable=False)
        data["Result"]["Bot A"]["Descriptions"][0]["tradable_after"] = "2000-01-01T00:00:00Z"
        data["Result"]["Bot A"]["Descriptions"][0]["item_expiration"] = "2000-01-01T00:00:00Z"
        data["Result"]["Bot A"]["Descriptions"][0]["descriptions"] = [
            {"value": "Tradable/Marketable After 10 Nov @ 7:00am"}
        ]
        item = adapt_cs2_inventory_response(data, "Bot A")[0]
        self.assertFalse(item.is_tradable)
        self.assertIsNone(item.tradable_after)
        self.assertFalse(item.is_reservable)

    def test_known_and_unknown_amounts_are_not_conflated(self):
        known = adapt_cs2_inventory_response(payload(), "Bot A")[0]
        unknown = replace(adapt_cs2_inventory_response(payload("Bot B", amount=None), "Bot B")[0], asset_id="b1")
        group = build_cs2_inventory_projection([known, unknown]).groups[0]
        self.assertEqual((group.instance_count, group.known_amount_sum,
                          group.unknown_amount_instances, group.total_amount), (2, 2, 1, None))

    def test_selected_bot_filter_and_stable_grouping(self):
        a = adapt_cs2_inventory_response(payload("Bot A"), "Bot A")[0]
        b = replace(a, bot_name="Bot B", asset_id="b1")
        full = build_cs2_inventory_projection([b, a])
        again = build_cs2_inventory_projection([a, b])
        self.assertEqual(full, again)
        self.assertEqual(len(build_cs2_inventory_projection([a, b], ["Bot A"]).instances), 1)
        self.assertEqual(build_cs2_inventory_projection([a, b], []).total_amount, None)
        self.assertEqual(len(build_cs2_inventory_projection([a, b], ["bot a", "BOT A"]).instances), 1)

    def test_missing_image_metadata_does_not_split_same_item_group(self):
        base = adapt_cs2_inventory_response(payload("Bot A"), "Bot A")[0]
        old_snapshot = replace(base, asset_id="old", icon_url="", icon_url_large="")
        current_snapshot = replace(base, bot_name="Bot B", asset_id="current",
                                   icon_url="normalized-icon")

        projection = build_cs2_inventory_projection([old_snapshot, current_snapshot])

        self.assertEqual(len(projection.groups), 1)
        self.assertEqual({item.asset_id for item in projection.groups[0].instances},
                         {"old", "current"})
        self.assertEqual({item.icon_url for item in projection.instances},
                         {"", "normalized-icon"})

    def test_all_independent_flag_combinations_stay_distinct_inside_one_variant_group(self):
        base = adapt_cs2_inventory_response(payload(), "Bot A")[0]
        combos = ((True, True), (True, False), (False, True), (False, False),
                  (None, True), (True, None), (None, None))
        items = [replace(base, asset_id=f"a{i}", is_tradable=t, is_marketable=m)
                 for i, (t, m) in enumerate(combos)]
        view = build_cs2_inventory_projection(items)
        self.assertEqual(len(view.groups), 1)
        self.assertEqual({(i.is_tradable, i.is_marketable)
                          for i in view.groups[0].instances}, set(combos))

    def test_restriction_metadata_and_same_class_assets_are_preserved(self):
        base = adapt_cs2_inventory_response(payload(), "Bot A")[0]
        a = replace(base, asset_id="a1", market_tradable_restriction=2)
        b = replace(base, asset_id="a2", market_tradable_restriction=3)
        projection = build_cs2_inventory_projection([a, b])
        self.assertEqual(len(projection.instances), 2)
        self.assertEqual(len(projection.groups), 1)
        self.assertEqual({i.physical_key for i in projection.instances}, {
            ("cs2", "Bot A", "a1"), ("cs2", "Bot A", "a2")})
        self.assertEqual({i.market_tradable_restriction for i in projection.groups[0].instances}, {2, 3})

    def test_restriction_status_type_and_source_remain_per_instance_in_one_group(self):
        from games.cs2.inventory_view import RestrictionStatus, RestrictionType
        base = adapt_cs2_inventory_response(payload(), "Bot A")[0]
        a = replace(base, asset_id="a1", restriction_type=RestrictionType.NONE,
                    restriction_status=RestrictionStatus.AVAILABLE, restriction_source="snapshot")
        b = replace(base, asset_id="a2", restriction_type=RestrictionType.UNKNOWN,
                    restriction_status=RestrictionStatus.UNKNOWN, restriction_source=None)
        group, = build_cs2_inventory_projection([a, b]).groups
        self.assertEqual({i.physical_key for i in group.instances}, {a.physical_key, b.physical_key})
        self.assertEqual({i.restriction_type for i in group.instances},
                         {RestrictionType.NONE, RestrictionType.UNKNOWN})
        self.assertEqual({i.restriction_status for i in group.instances},
                         {RestrictionStatus.AVAILABLE, RestrictionStatus.UNKNOWN})

    def test_asf_numeric_restrictions_stay_per_instance_and_cause_stays_unknown(self):
        base = adapt_cs2_inventory_response(payload(), "Bot A")[0]
        a = replace(base, asset_id="a1", market_tradable_restriction=7)
        b = replace(base, asset_id="a2", market_tradable_restriction=8)
        view = build_cs2_inventory_projection([a, b])
        self.assertEqual(len(view.groups), 1)
        self.assertEqual({i.market_tradable_restriction for i in view.groups[0].instances}, {7, 8})
        self.assertTrue(all(i.restriction_type.value == "unknown" for i in view.instances))

    def test_frozen_models_deep_freeze_collections_and_are_hashable(self):
        from games.cs2.inventory_view import CS2InventoryGroup, CS2InventoryProjection
        item = adapt_cs2_inventory_response(payload(), "Bot A")[0]
        members = [item]
        group = CS2InventoryGroup(["mutable", {"x": [1]}], members, 1, 2, 0, 2, 1)
        members.clear()
        self.assertEqual(group.instances, (item,))
        self.assertEqual(group.identity, ("mutable", (("x", (1,)),)))
        self.assertIsInstance(hash(group), int)
        projection = CS2InventoryProjection([item], [group], 2, 0, 2)
        self.assertIsInstance(hash(projection), int)
        with self.assertRaises((AttributeError, TypeError)):
            item.asset_id = "changed"
        with self.assertRaises((AttributeError, TypeError)):
            projection.instances += (item,)

    def test_variant_identity_splits_real_variants_but_not_per_instance_status(self):
        a = adapt_cs2_inventory_response(payload(), "Bot A")[0]
        b = replace(a, asset_id="b1", market_hash_name="AK | Blue")
        c = replace(a, asset_id="c1", is_tradable=False)
        view = build_cs2_inventory_projection([a, b, c])
        self.assertEqual(len(view.groups), 2)
        redline = next(g for g in view.groups
                       if any(i.market_hash_name == a.market_hash_name for i in g.instances))
        self.assertEqual({i.is_tradable for i in redline.instances}, {True, False})

    def test_per_instance_deadlines_do_not_split_variant_group(self):
        base = adapt_cs2_inventory_response(payload(), "Bot A")[0]
        first = replace(base, asset_id="a1", tradable_after=datetime(2026, 11, 10, tzinfo=timezone.utc))
        second = replace(base, asset_id="a2", bot_name="Bot B",
                         tradable_after=datetime(2026, 11, 11, tzinfo=timezone.utc))

        group, = build_cs2_inventory_projection([first, second]).groups

        self.assertEqual(len(group.instances), 2)
        self.assertEqual({i.tradable_after for i in group.instances},
                         {first.tradable_after, second.tradable_after})

    def test_quality_and_exterior_tags_are_preserved_for_variant_identity(self):
        data = payload()
        data["Result"]["Bot A"]["Descriptions"][0]["tags"] = [
            {"category": "Rarity", "localized_tag_name": "Classified"},
            {"category": "Quality", "localized_tag_name": "Genuine"},
            {"category": "Exterior", "localized_tag_name": "Minimal Wear"},
        ]
        item = adapt_cs2_inventory_response(data, "Bot A")[0]
        changed = replace(item, asset_id="a2", exterior="Field-Tested")

        self.assertEqual(item.exterior, "Minimal Wear")
        self.assertEqual(item.quality, "Genuine")
        self.assertEqual(item.rarity, "Classified")
        self.assertEqual(len(build_cs2_inventory_projection([item, changed]).groups), 2)

    def test_class_instance_type_and_stattrak_variants_remain_separate(self):
        base = adapt_cs2_inventory_response(payload(), "Bot A")[0]
        variants = [
            base,
            replace(base, asset_id="a2", class_id="different-class"),
            replace(base, asset_id="a3", instance_id="different-instance"),
            replace(base, asset_id="a4", item_type="Souvenir Rifle"),
            replace(base, asset_id="a5", market_hash_name="StatTrak AK | Redline",
                    tags=("StatTrak",)),
        ]

        projection = build_cs2_inventory_projection(variants)

        self.assertEqual(len(projection.groups), len(variants))
        self.assertEqual(sum(group.instance_count for group in projection.groups), len(variants))

    def test_naive_release_timestamp_is_downgraded_to_unknown(self):
        item = adapt_cs2_inventory_response(payload(), "Bot A")[0]

        item = replace(item, tradable_after=datetime(2026, 11, 10))

        self.assertIsNone(item.tradable_after)


if __name__ == "__main__":
    unittest.main()
