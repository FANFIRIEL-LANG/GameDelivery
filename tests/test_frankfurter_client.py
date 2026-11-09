import json
import unittest
from decimal import Decimal

from integrations.frankfurter.client import FrankfurterClient, FrankfurterError


class FrankfurterClientTests(unittest.TestCase):
    def test_returns_blended_usd_rub_and_keeps_source_date_distinct_from_fetch_time(self):
        calls = []

        def request(url, timeout):
            calls.append((url, timeout))
            return 200, b'{"date":"2026-11-08","base":"USD","quote":"RUB","rate":81.795}'

        result = FrankfurterClient(request_func=request).fetch_usd_rub()

        self.assertEqual(calls[0][0], "https://api.frankfurter.dev/v2/rate/usd/rub")
        self.assertEqual((result.base, result.quote, result.rate),
                         ("USD", "RUB", Decimal("81.795")))
        self.assertEqual(result.source_date, "2026-11-08")
        self.assertIsNotNone(result.fetched_at.tzinfo)

    def test_rejects_wrong_pair_and_non_positive_rate(self):
        for payload in (
            {"date": "2026-11-08", "base": "EUR", "quote": "RUB", "rate": 90},
            {"date": "2026-11-08", "base": "USD", "quote": "RUB", "rate": 0},
        ):
            with self.subTest(payload=payload):
                def request(_url, _timeout):
                    return 200, json.dumps(payload).encode()
                with self.assertRaises(FrankfurterError):
                    FrankfurterClient(request_func=request).fetch_usd_rub()

    def test_string_rate_is_rejected_instead_of_coerced(self):
        def request(_url, _timeout):
            return 200, b'{"date":"2026-11-08","base":"USD","quote":"RUB","rate":"90"}'

        with self.assertRaises(FrankfurterError):
            FrankfurterClient(request_func=request).fetch_usd_rub()

    def test_http_failure_exposes_only_status_code(self):
        def request(_url, _timeout):
            return 503, b"private body"

        with self.assertRaises(FrankfurterError) as caught:
            FrankfurterClient(request_func=request).fetch_usd_rub()

        self.assertEqual(caught.exception.http_status, 503)
        self.assertNotIn("private body", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
