import unittest
from dataclasses import replace
from decimal import Decimal

from games.cs2.inventory_view import adapt_cs2_inventory_response
from games.cs2.pricing import (
    CachedPrice, PriceState, asset_price_text, calculate_valuation, requestable_positions,
    group_price_text, unsupported_positions,
)
from integrations.openskin.client import PricePosition


def item(bot="Bot A", asset="a1", name="AK-47 | Redline (Field-Tested)", amount=8,
         tradable=True):
    payload = {"Result": {bot: {
        "Assets": [{"appid": 730, "contextid": 2, "assetid": asset,
                    "classid": "c1", "instanceid": "0", "amount": amount}],
        "Descriptions": [{"classid": "c1", "instanceid": "0", "name": name,
                           "type": "Rifle", "market_hash_name": name,
                           "tradable": tradable, "marketable": True}],
    }}}
    return adapt_cs2_inventory_response(payload, bot)[0]


def quote(instance, ask, *, state=PriceState.CURRENT, currency="USD"):
    position = PricePosition(instance.appid or 730, instance.market_hash_name)
    return CachedPrice(position, Decimal(ask) if ask is not None else None,
                       currency, "2026-11-09T10:00:00Z", "2026-11-09T10:01:00Z", state)


class CS2PricingTests(unittest.TestCase):
    def test_threshold_is_strict_and_cheap_items_are_not_converted(self):
        prices = ["0.01", "0.03", "0.049", "0.05", "0.10", "1.00"]
        items = [item(asset=str(index), name=f"Item {index}") for index in range(len(prices))]
        quotes = {quote(instance, amount).position: quote(instance, amount)
                  for instance, amount in zip(items, prices)}

        result = calculate_valuation(items, quotes, Decimal("90"))

        self.assertEqual(result.below_threshold_instances, 3)
        self.assertEqual(result.priced_instances, 3)
        self.assertEqual(result.unknown_price_instances, 0)
        self.assertEqual(result.selected_total_rub, Decimal("103.50"))
        self.assertEqual([entry.price_rub for entry in result.assets[:3]], [None, None, None])
        self.assertEqual(result.assets[3].price_rub, Decimal("4.50"))

    def test_missing_rate_produces_partial_total_and_separate_pending_count(self):
        first, second = item(asset="a1"), item(bot="Bot B", asset="b1")
        quotes = {q.position: q for q in (quote(first, "2.00"), quote(second, "3.00"))}

        result = calculate_valuation([first, second], quotes, None)

        self.assertEqual(result.selected_total_rub, Decimal("0"))
        self.assertEqual(result.awaiting_rate_instances, 2)
        self.assertEqual(result.priced_instances, 0)
        self.assertEqual(result.unknown_price_instances, 0)
        self.assertEqual([value.total_rub for value in result.bot_totals],
                         [Decimal("0"), Decimal("0")])

    def test_unknown_price_is_not_reported_as_zero(self):
        instance = item()

        result = calculate_valuation([instance], {}, Decimal("90"))

        self.assertIsNone(result.selected_total_rub)
        self.assertEqual(result.unknown_price_instances, 1)

    def test_counts_physical_assets_not_asf_amount_and_includes_nontradable(self):
        first = item(bot="Bot A", asset="a1", amount=20, tradable=False)
        second = item(bot="Bot A", asset="a2", amount=None, tradable=True)
        q1, q2 = quote(first, "2.00"), quote(second, "2.00")

        result = calculate_valuation([first, second], {q1.position: q1, q2.position: q2},
                                     Decimal("90"))

        self.assertEqual(result.selected_total_rub, Decimal("360.00"))
        self.assertEqual(result.priced_instances, 2)
        self.assertEqual(result.bot_totals[0].total_rub, Decimal("360.00"))

    def test_selected_bots_filter_overall_and_per_bot_totals(self):
        a, b = item(bot="Bot A", asset="a1"), item(bot="Bot B", asset="b1")
        qa, qb = quote(a, "2.00"), quote(b, "5.00")

        result = calculate_valuation([a, b], {qa.position: qa, qb.position: qb},
                                     Decimal("100"), selected_bots=["Bot B"])

        self.assertEqual(result.selected_total_rub, Decimal("500.00"))
        self.assertEqual([(row.bot_name, row.total_rub) for row in result.bot_totals],
                         [("Bot B", Decimal("500.00"))])

    def test_unknown_price_is_separate_from_below_threshold_and_stale_is_counted(self):
        cheap, missing, stale = (
            item(asset=name, name=f"Item {name}") for name in ("cheap", "missing", "stale")
        )
        qcheap = quote(cheap, "0.03")
        qstale = quote(stale, "1.00", state=PriceState.STALE)
        quotes = {q.position: q for q in (qcheap, quote(missing, None), qstale)}

        result = calculate_valuation([cheap, missing, stale], quotes, Decimal("90"))

        self.assertEqual(result.below_threshold_instances, 1)
        self.assertEqual(result.unknown_price_instances, 1)
        self.assertEqual(result.priced_instances, 1)
        self.assertEqual(result.stale_price_instances, 1)
        self.assertEqual(result.selected_total_rub, Decimal("90.00"))

    def test_exact_pattern_sensitive_items_are_not_generic_priced_without_pattern(self):
        doppler = item(name="★ Karambit | Doppler (Factory New)")
        case_hardened = item(asset="b1", name="AK-47 | Case Hardened (Field-Tested)")

        requests = requestable_positions([doppler, case_hardened])

        self.assertEqual(requests, ())
        result = calculate_valuation([doppler, case_hardened], {}, Decimal("90"))
        self.assertEqual(result.unknown_price_instances, 2)
        self.assertTrue(all(entry.state is PriceState.UNSUPPORTED for entry in result.assets))
        self.assertTrue(all("точный pattern" in asset_price_text(entry)
                            for entry in result.assets))

    def test_seed_sensitive_finishes_are_not_assigned_generic_steam_asks(self):
        names = (
            "AK-47 | Case Hardened (Minimal Wear)",
            "★ Karambit | Doppler (Factory New)",
            "M4A1-S | Fade (Factory New)",
            "Five-SeveN | Crimson Web (Field-Tested)",
            "AK-47 | Slaughter (Minimal Wear)",
            "AK-47 | Heat Treated (Field-Tested)",
        )
        instances = [item(asset=f"seed-{index}", name=name)
                     for index, name in enumerate(names)]

        self.assertEqual(requestable_positions(instances), ())
        self.assertEqual(len(unsupported_positions(instances)), len(names))
        result = calculate_valuation(instances, {}, Decimal("90"))
        self.assertEqual(result.unsupported_instances, len(names))
        self.assertTrue(all(asset.price_usd is None and asset.price_rub is None
                            for asset in result.assets))

    def test_api_error_is_distinct_from_a_successful_missing_price(self):
        instance = item()
        position = PricePosition(730, instance.market_hash_name)
        failed = CachedPrice(position, None, "USD", None, None, PriceState.ERROR, 400)

        result = calculate_valuation([instance], {position: failed}, Decimal("90"))

        self.assertEqual(result.unknown_price_instances, 1)
        self.assertEqual(result.error_price_instances, 1)
        self.assertEqual(result.assets[0].state, PriceState.ERROR)
        self.assertIn("HTTP 400", asset_price_text(result.assets[0]))

    def test_requestable_positions_are_deduplicated_by_appid_name_and_pattern(self):
        first, second = item(bot="Bot A", asset="a1"), item(bot="Bot B", asset="b1")

        self.assertEqual(requestable_positions([first, second]),
                         (PricePosition(730, first.market_hash_name),))

    def test_invalid_exchange_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_valuation([item()], {}, Decimal("0"))
        with self.assertRaises(TypeError):
            calculate_valuation([item()], {}, 90.0)

    def test_unit_price_rounds_half_up_to_cents(self):
        instance = item()
        source = quote(instance, "0.05")

        result = calculate_valuation([instance], {source.position: source}, Decimal("1.1"))

        self.assertEqual(result.assets[0].price_rub, Decimal("0.06"))

    def test_low_ask_display_keeps_source_precision_and_has_no_ruble_value(self):
        instance = item()
        source = quote(instance, "0.049", state=PriceState.STALE)

        result = calculate_valuation([instance], {source.position: source}, Decimal("100"))
        visible = asset_price_text(result.assets[0])

        self.assertIn("$0.049", visible)
        self.assertIn("< $0.05", visible)
        self.assertIn("устарела", visible)
        self.assertNotIn("₽", visible)

    def test_low_ask_group_has_usd_label_and_no_ruble_group_total(self):
        instance = item()
        source = quote(instance, "0.03")
        result = calculate_valuation([instance], {source.position: source}, Decimal("90"))

        text = group_price_text([instance], result)

        self.assertIn("$0.03", text)
        self.assertIn("ниже $0.05", text)
        self.assertNotIn("₽", text)


if __name__ == "__main__":
    unittest.main()
