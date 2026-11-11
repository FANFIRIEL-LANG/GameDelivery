import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from games.cs2.price_store import PriceCacheStore
from games.cs2.pricing import CachedPrice, PriceState
from integrations.openskin.client import PricePosition


class CS2PriceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "prices.sqlite3"
        self.store = PriceCacheStore(self.database)
        self.now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        self.position = PricePosition(730, "AK-47 | Redline (Field-Tested)")

    def tearDown(self):
        self.temp.cleanup()

    def test_init_is_idempotent_and_exact_decimals_survive_reopen(self):
        quote = CachedPrice(self.position, Decimal("0.049"), "USD",
                            self.now.isoformat(), self.now.isoformat(), PriceState.BELOW_THRESHOLD)
        self.store.save_quote(quote, now=self.now)
        reopened = PriceCacheStore(self.database)

        cached = reopened.get(self.position, now=self.now)

        self.assertEqual(cached.steam_ask, Decimal("0.049"))
        self.assertEqual(cached.state, PriceState.BELOW_THRESHOLD)
        db = sqlite3.connect(self.database)
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            db.close()
        self.assertIn("cs2_price_cache", tables)
        self.assertNotIn("game_inventory_snapshot", tables)

    def test_converted_value_and_exchange_provenance_survive_restart(self):
        quote = CachedPrice(self.position, Decimal("1.00"), "USD", self.now.isoformat(),
                            self.now.isoformat(), PriceState.CURRENT)
        self.store.save_quote(quote, now=self.now)
        self.store.save_conversion(
            self.position, Decimal("90.12"), Decimal("90.123"), "2026-11-08",
            "2026-11-09T12:00:00+00:00",
        )

        cached = PriceCacheStore(self.database).get(self.position, now=self.now)

        self.assertEqual(cached.price_rub, Decimal("90.12"))
        self.assertEqual(cached.usd_rub_rate, Decimal("90.123"))
        self.assertEqual(cached.usd_rub_source_date, "2026-11-08")
        self.assertEqual(cached.usd_rub_fetched_at, "2026-11-09T12:00:00+00:00")

    def test_below_threshold_price_cannot_be_persisted_as_converted(self):
        quote = CachedPrice(self.position, Decimal("0.049"), "USD", None,
                            self.now.isoformat(), PriceState.BELOW_THRESHOLD)
        self.store.save_quote(quote, now=self.now)

        with self.assertRaises(ValueError):
            self.store.save_conversion(
                self.position, Decimal("4.41"), Decimal("90"), "2026-11-08",
                self.now.isoformat(),
            )

    def test_exact_threshold_is_excluded_from_ruble_conversion(self):
        quote = CachedPrice(self.position, Decimal("0.05"), "USD",
                            self.now.isoformat(), self.now.isoformat(), PriceState.CURRENT)
        self.store.save_quote(quote, now=self.now)

        cached = self.store.get(self.position, now=self.now)

        self.assertEqual(cached.state, PriceState.BELOW_THRESHOLD)
        self.assertIsNone(cached.price_rub)
        with self.assertRaises(ValueError):
            self.store.save_conversion(
                self.position, Decimal("4.50"), Decimal("90"), "2026-11-09",
                self.now.isoformat(),
            )

    def test_existing_inventory_table_and_row_are_untouched(self):
        db = sqlite3.connect(self.database)
        try:
            db.execute("CREATE TABLE game_inventory_snapshot (asset_id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO game_inventory_snapshot VALUES ('kept-asset')")
            db.commit()
        finally:
            db.close()

        PriceCacheStore(self.database)

        db = sqlite3.connect(self.database)
        try:
            self.assertEqual(db.execute("SELECT asset_id FROM game_inventory_snapshot").fetchall(),
                             [("kept-asset",)])
        finally:
            db.close()

    def test_transient_error_keeps_last_successful_price_and_marks_stale(self):
        old = CachedPrice(self.position, Decimal("10.25"), "USD", "source-date",
                          (self.now - timedelta(hours=1)).isoformat(), PriceState.CURRENT)
        self.store.save_quote(old, now=self.now - timedelta(hours=1))

        self.store.mark_error(self.position, 503, now=self.now)

        cached = self.store.get(self.position)
        self.assertEqual(cached.steam_ask, Decimal("10.25"))
        self.assertEqual(cached.source_updated_at, "source-date")
        self.assertEqual(cached.state, PriceState.STALE)
        self.assertEqual(cached.last_http_error, 503)
        self.assertEqual(cached.fetched_at, old.fetched_at)

    def test_old_source_quote_is_stale_even_if_recently_fetched(self):
        quote = CachedPrice(self.position, Decimal("1.00"), "USD",
                            "2026-11-09T04:00:00Z", self.now.isoformat(), PriceState.CURRENT)
        self.store.save_quote(quote, now=self.now)

        cached = self.store.get(self.position, now=self.now)

        self.assertEqual(cached.state, PriceState.STALE)

    def test_retry_after_and_six_hour_refresh_gate(self):
        quote = CachedPrice(self.position, Decimal("1.00"), "USD", None,
                            self.now.isoformat(), PriceState.CURRENT)
        self.store.save_quote(quote, now=self.now)

        self.assertEqual(self.store.due_positions([self.position], now=self.now + timedelta(minutes=10)), ())
        self.assertEqual(self.store.due_positions([self.position], now=self.now + timedelta(hours=6)),
                         (self.position,))
        self.store.mark_error(self.position, 429, retry_after_seconds=900,
                              now=self.now + timedelta(hours=6))
        self.assertEqual(self.store.due_positions([self.position], now=self.now + timedelta(hours=6, minutes=10)), ())
        self.assertEqual(self.store.due_positions([self.position], now=self.now + timedelta(hours=6, minutes=16)),
                         (self.position,))

    def test_manual_refresh_bypasses_six_hour_cache_age_but_respects_cooldowns(self):
        quote = CachedPrice(self.position, Decimal("1.00"), "USD", None,
                            self.now.isoformat(), PriceState.CURRENT)
        self.store.save_quote(quote, now=self.now)

        self.assertEqual(
            self.store.due_positions([self.position], now=self.now + timedelta(minutes=5), force=True),
            (),
        )
        self.assertEqual(
            self.store.due_positions([self.position], now=self.now + timedelta(minutes=11), force=True),
            (self.position,),
        )

        self.store.mark_error(
            self.position, 429, retry_after_seconds=1800,
            now=self.now + timedelta(minutes=11),
        )
        self.assertEqual(
            self.store.due_positions([self.position], now=self.now + timedelta(minutes=20), force=True),
            (),
        )
        self.assertEqual(
            self.store.due_positions([self.position], now=self.now + timedelta(minutes=42), force=True),
            (self.position,),
        )

    def test_non_retryable_client_error_does_not_repeat_every_ten_minutes(self):
        self.store.mark_error(self.position, 400, now=self.now)

        self.assertEqual(self.store.due_positions([self.position], now=self.now + timedelta(hours=1)), ())
        self.assertEqual(self.store.due_positions([self.position], now=self.now + timedelta(hours=6)),
                         (self.position,))

    def test_exchange_rate_keeps_source_date_separate_from_fetch_time(self):
        self.store.save_fx_rate(Decimal("91.2345"), "2026-11-08", now=self.now)
        cached = PriceCacheStore(self.database).get_fx_rate()

        self.assertEqual(cached.rate, Decimal("91.2345"))
        self.assertEqual(cached.source_date, "2026-11-08")
        self.assertEqual(cached.fetched_at, self.now.isoformat())

    def test_manual_refresh_wait_reports_local_guard_and_provider_retry_after(self):
        self.store.mark_error(
            self.position, 429, retry_after_seconds=900, now=self.now,
        )

        wait = self.store.manual_refresh_wait(
            (self.position,), now=self.now + timedelta(minutes=5),
        )

        self.assertEqual(wait[0], self.now + timedelta(minutes=15))
        self.assertIn("Retry-After OpenSkin", wait[1])
        self.assertIn("локальный интервал Steam Ask", wait[1])

    def test_manual_refresh_wait_returns_none_when_position_is_eligible(self):
        self.assertIsNone(self.store.manual_refresh_wait(
            (self.position,), now=self.now,
        ))

    def test_exchange_error_retains_last_rate_and_error_code(self):
        self.store.save_fx_rate(Decimal("91.2345"), "2026-11-08", now=self.now)
        self.store.mark_fx_error(503, now=self.now + timedelta(days=1))

        cached = self.store.get_fx_rate()
        self.assertEqual(cached.rate, Decimal("91.2345"))
        self.assertEqual(cached.last_http_error, 503)
        self.assertTrue(cached.is_stale)

    def test_exchange_retry_after_is_respected_without_a_cached_rate(self):
        self.store.mark_fx_error(429, retry_after_seconds=900, now=self.now)

        self.assertFalse(self.store.fx_rate_due(now=self.now + timedelta(minutes=10)))
        self.assertTrue(self.store.fx_rate_due(now=self.now + timedelta(minutes=16)))

    def test_unsupported_variant_state_is_persisted_without_a_price(self):
        self.store.mark_unsupported(self.position)

        cached = self.store.get(self.position, now=self.now)

        self.assertEqual(cached.state, PriceState.UNSUPPORTED)
        self.assertIsNone(cached.steam_ask)


if __name__ == "__main__":
    unittest.main()
