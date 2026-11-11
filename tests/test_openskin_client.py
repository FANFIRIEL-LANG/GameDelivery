import json
import unittest
from decimal import Decimal

from integrations.openskin.client import OpenSkinClient, OpenSkinError, PricePosition
from integrations.openskin.proxy import OpenSkinProxy


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
            # OpenSkin returns both a name-keyed data map and an ordered results list.
            data = {row["item"]: row for row in results}
            return 200, json.dumps(
                {"data": data, "results": results}, default=float,
            ).encode(), {}

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

    def test_invalid_results_collection_diagnostics_cover_schema_variants(self):
        position = PricePosition(730, "PRIVATE ITEM NAME")
        cases = (
            ("missing_results", b'{"data":{"PRIVATE ITEM NAME":{}}}', "results_type=missing"),
            ("results_not_list", b'{"data":{},"results":{}}', "results_type=dict"),
            ("results_count_mismatch", b'{"data":{},"results":[]}', "results_count=0"),
            ("root_not_object", b'[]', "root_type=list"),
        )
        for label, response, expected_detail in cases:
            with self.subTest(label=label):
                def request(_url, _body, _headers, _timeout):
                    return 200, response, {
                        "Content-Type": "application/json",
                        "Content-Length": str(len(response)),
                    }

                with self.assertLogs("integrations.openskin.client", level="WARNING") as captured:
                    with self.assertRaises(OpenSkinError) as caught:
                        OpenSkinClient(request_func=request).fetch_prices([position])

                self.assertEqual(caught.exception.category, "invalid_response")
                self.assertIn(expected_detail, caught.exception.diagnostic_detail)
                self.assertIn("requested_count=1", caught.exception.diagnostic_detail)
                self.assertIn("body_bytes=", caught.exception.diagnostic_detail)
                self.assertIn("content_type=application/json", caught.exception.diagnostic_detail)
                combined = "\\n".join(captured.output) + caught.exception.diagnostic_detail
                self.assertNotIn("PRIVATE ITEM NAME", combined)
                self.assertNotIn(response.decode("utf-8"), combined)

    def test_invalid_result_rows_report_index_and_reason_without_item_names(self):
        position = PricePosition(730, "PRIVATE ITEM NAME", "Ruby")
        cases = (
            ("not_object", ["not an object"], "row_not_object"),
            ("identity_mismatch", [{"item": "OTHER PRIVATE ITEM", "pattern": "Ruby"}], "item_identity_mismatch"),
            ("pattern_mismatch", [{"item": "PRIVATE ITEM NAME", "pattern": "Sapphire"}], "pattern_mismatch"),
        )
        for label, rows, expected_reason in cases:
            with self.subTest(label=label):
                response = json.dumps({"results": rows}).encode()

                def request(_url, _body, _headers, _timeout):
                    return 200, response, {}

                with self.assertLogs("integrations.openskin.client", level="WARNING") as captured:
                    with self.assertRaises(OpenSkinError) as caught:
                        OpenSkinClient(request_func=request).fetch_prices([position])

                self.assertEqual(caught.exception.category, "invalid_response")
                self.assertIn("result_index=0", caught.exception.diagnostic_detail)
                self.assertIn(f"reason={expected_reason}", caught.exception.diagnostic_detail)
                combined = "\\n".join(captured.output) + caught.exception.diagnostic_detail
                self.assertNotIn("PRIVATE ITEM NAME", combined)
                self.assertNotIn("OTHER PRIVATE ITEM", combined)

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


    def test_unexpected_proxy_request_logs_safe_traceback_without_credentials(self):
        proxy = OpenSkinProxy(
            "http", "proxy.example", 8080, "private-user", "private pass",
        )

        def broken_request(_url, _body, _headers, _timeout, _proxy):
            raise TypeError("Qt overload rejected private-user private pass")

        with self.assertLogs("integrations.openskin.client", level="WARNING") as captured:
            with self.assertRaises(OpenSkinError) as caught:
                OpenSkinClient(
                    proxy=proxy, proxy_request_func=broken_request,
                ).fetch_prices([PricePosition(730, "AK-47 | Redline")])

        output = "\n".join(captured.output)
        diagnostic = caught.exception.diagnostic_detail
        for value in ("context=price_request", "exception=TypeError",
                      "test_openskin_client.py", "broken_request"):
            self.assertIn(value, output)
            self.assertIn(value, diagnostic)
        self.assertEqual(caught.exception.category, "client_error")
        self.assertNotIn("private-user", output)
        self.assertNotIn("private pass", output)
        self.assertNotIn("private-user", diagnostic)
        self.assertNotIn("private pass", diagnostic)


if __name__ == "__main__":
    unittest.main()
