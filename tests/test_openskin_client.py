import json
import unittest
from decimal import Decimal

from integrations.openskin.client import OpenSkinClient, OpenSkinError, PricePosition


class OpenSkinClientTests(unittest.TestCase):
    def test_uses_ordered_results_and_only_steam_ask(self):
        calls = []

        def request(url, body, headers, timeout):
            payload = json.loads(body)
            calls.append((url, payload, headers, timeout))
            names = payload["items"]
            results = []
            for item in names:
                name = item["item"] if isinstance(item, dict) else item
                pattern = item.get("pattern") if isinstance(item, dict) else None
                ask = "1.25" if pattern == "Phase 1" else "2.50"
                results.append({
                    "item": name, "pattern": pattern,
                    "steam": {"ask": Decimal(ask), "currency": "USD",
                              "updated_at": "2026-11-09T10:00:00Z", "median": 999},
                    "skinport": {"ask": 0.01}, "lowest_ask": {"price": 0.01},
                })
            return 200, json.dumps({"results": results}, default=float).encode(), {}

        positions = [
            PricePosition(730, "★ Knife | Doppler (Factory New)", "Phase 1"),
            PricePosition(730, "★ Knife | Doppler (Factory New)", "Phase 2"),
        ]
        result = OpenSkinClient(request_func=request).fetch_prices(positions)

        self.assertEqual([quote.steam_ask for quote in result],
                         [Decimal("1.25"), Decimal("2.50")])
        self.assertEqual([quote.position.pattern for quote in result], ["Phase 1", "Phase 2"])
        self.assertEqual(calls[0][0], "https://api.openskin.dev/v1/prices/batch")
        self.assertEqual(calls[0][1]["items"], [
            {"item": "★ Knife | Doppler (Factory New)", "pattern": "Phase 1"},
            {"item": "★ Knife | Doppler (Factory New)", "pattern": "Phase 2"},
        ])
        self.assertNotIn("X-API-Key", calls[0][2])

    def test_splits_batches_to_500_and_serializes_requests(self):
        sizes = []

        def request(_url, body, _headers, _timeout):
            names = json.loads(body)["items"]
            sizes.append(len(names))
            return 200, json.dumps({"results": [
                {"item": item, "steam": {"ask": None, "currency": "USD"}}
                for item in names
            ]}).encode(), {}

        positions = [PricePosition(730, f"Item {index}") for index in range(1001)]
        quotes = OpenSkinClient(request_func=request).fetch_prices(positions)

        self.assertEqual(sizes, [500, 500, 1])
        self.assertEqual(len(quotes), 1001)

    def test_rejects_result_identity_mismatch(self):
        def request(_url, _body, _headers, _timeout):
            return 200, b'{"results":[{"item":"different item","steam":{"ask":1,"currency":"USD"}}]}', {}

        with self.assertRaises(OpenSkinError):
            OpenSkinClient(request_func=request).fetch_prices([PricePosition(730, "Expected")])

    def test_string_ask_is_unknown_because_contract_requires_a_number(self):
        def request(_url, _body, _headers, _timeout):
            return 200, b'{"results":[{"item":"Item","steam":{"ask":"1.25","currency":"USD"}}]}', {}

        quote, = OpenSkinClient(request_func=request).fetch_prices([PricePosition(730, "Item")])

        self.assertIsNone(quote.steam_ask)

    def test_preserves_safe_http_code_and_retry_after_without_body(self):
        secret = "private response"

        def request(_url, _body, _headers, _timeout):
            return 429, secret.encode(), {"Retry-After": "120"}

        with self.assertRaises(OpenSkinError) as caught:
            OpenSkinClient(request_func=request).fetch_prices([PricePosition(730, "Item")])

        self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(caught.exception.retry_after_seconds, 120)
        self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
