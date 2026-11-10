"""Additive SQLite cache isolated from the existing inventory snapshot tables."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3

from games.cs2.pricing import PRICE_THRESHOLD_USD, CachedPrice, PriceState
from integrations.openskin.client import PricePosition


@dataclass(frozen=True)
class CachedExchangeRate:
    rate: Decimal
    source_date: str
    fetched_at: str
    last_http_error: int | None = None
    is_stale: bool = False
    retry_after_at: str | None = None


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cache timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


class PriceCacheStore:
    REFRESH_INTERVAL = timedelta(hours=6)
    RETRY_INTERVAL = timedelta(minutes=10)
    MANUAL_REFRESH_INTERVAL = timedelta(minutes=10)

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS cs2_price_cache (
                    appid INTEGER NOT NULL,
                    market_hash_name TEXT NOT NULL,
                    pattern TEXT NOT NULL DEFAULT '',
                    steam_ask TEXT,
                    currency TEXT,
                    source_updated_at TEXT,
                    fetched_at TEXT,
                    last_success_at TEXT,
                    last_attempt_at TEXT,
                    state TEXT NOT NULL,
                    below_threshold INTEGER NOT NULL DEFAULT 0,
                    last_http_error INTEGER,
                    retry_after_at TEXT,
                    price_rub TEXT,
                    usd_rub_rate TEXT,
                    usd_rub_source_date TEXT,
                    usd_rub_fetched_at TEXT,
                    PRIMARY KEY (appid, market_hash_name, pattern)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS cs2_fx_rate_cache (
                    base TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    rate TEXT,
                    source_date TEXT,
                    fetched_at TEXT,
                    last_attempt_at TEXT,
                    last_http_error INTEGER,
                    retry_after_at TEXT,
                    PRIMARY KEY (base, quote)
                )
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(cs2_price_cache)")}
            for name, sql_type in (
                ("price_rub", "TEXT"), ("usd_rub_rate", "TEXT"),
                ("usd_rub_source_date", "TEXT"), ("usd_rub_fetched_at", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE cs2_price_cache ADD COLUMN {name} {sql_type}")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database_path, timeout=10)
        db.execute("PRAGMA busy_timeout = 10000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _key(position: PricePosition):
        return position.appid, position.market_hash_name, position.pattern or ""

    def _retry_at(self, http_status: int | None, retry_after_seconds: int | None,
                  current: datetime) -> datetime:
        if retry_after_seconds is not None:
            delay = max(0, retry_after_seconds)
        elif http_status is not None and 400 <= http_status < 500 and http_status != 429:
            delay = int(self.REFRESH_INTERVAL.total_seconds())
        else:
            delay = int(self.RETRY_INTERVAL.total_seconds())
        return current + timedelta(seconds=delay)

    def get(self, position: PricePosition, *, now: datetime | None = None) -> CachedPrice | None:
        with self._connect() as db:
            row = db.execute("""
                SELECT steam_ask,currency,source_updated_at,fetched_at,state,last_http_error,
                       price_rub,usd_rub_rate,usd_rub_source_date,usd_rub_fetched_at
                FROM cs2_price_cache WHERE appid=? AND market_hash_name=? AND pattern=?
            """, self._key(position)).fetchone()
        if row is None:
            return None
        ask = None
        try:
            ask = Decimal(row[0]) if row[0] is not None else None
        except InvalidOperation:
            pass
        state = PriceState(row[4])
        fetched = datetime.fromisoformat(row[3]) if row[3] else None
        current = _utc(now)
        source_updated = None
        if row[2]:
            try:
                source_updated = datetime.fromisoformat(row[2].replace("Z", "+00:00"))
            except ValueError:
                pass
        old_fetch = fetched is not None and current - _utc(fetched) >= self.REFRESH_INTERVAL
        old_source = (source_updated is not None and source_updated.tzinfo is not None
                      and current - _utc(source_updated) >= self.REFRESH_INTERVAL)
        if (state in {PriceState.CURRENT, PriceState.BELOW_THRESHOLD}
                and (old_fetch or old_source)):
            state = PriceState.STALE
        price_rub = Decimal(row[6]) if row[6] is not None else None
        rate = Decimal(row[7]) if row[7] is not None else None
        return CachedPrice(position, ask, row[1], row[2], row[3], state, row[5],
                           price_rub, rate, row[8], row[9])

    def save_quote(self, quote: CachedPrice, *, now: datetime | None = None):
        timestamp = _utc(now).isoformat()
        ask = str(quote.steam_ask) if quote.steam_ask is not None else None
        below = int(quote.currency == "USD" and quote.steam_ask is not None
                    and quote.steam_ask <= PRICE_THRESHOLD_USD)
        state = PriceState.BELOW_THRESHOLD if below else quote.state
        with self._connect() as db:
            db.execute("""
                INSERT INTO cs2_price_cache
                (appid,market_hash_name,pattern,steam_ask,currency,source_updated_at,
                 fetched_at,last_success_at,last_attempt_at,state,below_threshold,
                 last_http_error,retry_after_at,price_rub,usd_rub_rate,
                 usd_rub_source_date,usd_rub_fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL)
                ON CONFLICT(appid,market_hash_name,pattern) DO UPDATE SET
                  steam_ask=excluded.steam_ask,currency=excluded.currency,
                  source_updated_at=excluded.source_updated_at,fetched_at=excluded.fetched_at,
                  last_success_at=excluded.last_success_at,last_attempt_at=excluded.last_attempt_at,
                  state=excluded.state,below_threshold=excluded.below_threshold,
                  last_http_error=NULL,retry_after_at=NULL,price_rub=NULL,usd_rub_rate=NULL,
                  usd_rub_source_date=NULL,usd_rub_fetched_at=NULL
            """, (*self._key(quote.position), ask, quote.currency,
                  quote.source_updated_at, timestamp, timestamp, timestamp,
                  state.value, below))

    def mark_unsupported(self, position: PricePosition):
        with self._connect() as db:
            db.execute("""
                INSERT INTO cs2_price_cache
                (appid,market_hash_name,pattern,state,below_threshold)
                VALUES (?,?,?,'unsupported',0)
                ON CONFLICT(appid,market_hash_name,pattern) DO UPDATE SET
                  steam_ask=NULL,currency=NULL,source_updated_at=NULL,fetched_at=NULL,
                  last_success_at=NULL,last_attempt_at=NULL,state='unsupported',
                  below_threshold=0,last_http_error=NULL,retry_after_at=NULL,
                  price_rub=NULL,usd_rub_rate=NULL,usd_rub_source_date=NULL,
                  usd_rub_fetched_at=NULL
                WHERE cs2_price_cache.state!='unsupported'
            """, self._key(position))

    def save_conversion(self, position: PricePosition, price_rub: Decimal,
                        usd_rub_rate: Decimal, source_date: str, fetched_at: str):
        for name, value in (("price_rub", price_rub), ("USD/RUB rate", usd_rub_rate)):
            if isinstance(value, (bool, float)):
                raise TypeError(f"{name} must use Decimal, not float")
        price_rub, usd_rub_rate = Decimal(price_rub), Decimal(usd_rub_rate)
        if (not price_rub.is_finite() or price_rub < 0
                or not usd_rub_rate.is_finite() or usd_rub_rate <= 0):
            raise ValueError("converted price and exchange rate must be finite and non-negative")
        date.fromisoformat(source_date)
        fetched = _utc(datetime.fromisoformat(fetched_at)).isoformat()
        with self._connect() as db:
            row = db.execute("""
                SELECT steam_ask,currency FROM cs2_price_cache
                WHERE appid=? AND market_hash_name=? AND pattern=?
            """, self._key(position)).fetchone()
            if row is None:
                raise KeyError("price quote does not exist")
            ask = Decimal(row[0]) if row[0] is not None else None
            if row[1] != "USD" or ask is None or ask <= PRICE_THRESHOLD_USD:
                raise ValueError("price is not eligible for RUB conversion")
            db.execute("""
                UPDATE cs2_price_cache SET price_rub=?,usd_rub_rate=?,
                    usd_rub_source_date=?,usd_rub_fetched_at=?
                WHERE appid=? AND market_hash_name=? AND pattern=?
            """, (str(price_rub), str(usd_rub_rate), source_date, fetched,
                  *self._key(position)))

    def mark_error(self, position: PricePosition, http_status: int | None, *,
                   retry_after_seconds: int | None = None, now: datetime | None = None):
        current = _utc(now)
        with self._connect() as db:
            row = db.execute("""
                SELECT steam_ask,state FROM cs2_price_cache
                WHERE appid=? AND market_hash_name=? AND pattern=?
            """, self._key(position)).fetchone()
            has_price = bool(row and row[0] is not None)
            state = PriceState.STALE if has_price else PriceState.ERROR
            retry = self._retry_at(http_status, retry_after_seconds, current)
            db.execute("""
                INSERT INTO cs2_price_cache
                (appid,market_hash_name,pattern,fetched_at,last_attempt_at,state,
                 below_threshold,last_http_error,retry_after_at)
                VALUES (?,?,?,NULL,?,?,0,?,?)
                ON CONFLICT(appid,market_hash_name,pattern) DO UPDATE SET
                  last_attempt_at=excluded.last_attempt_at,
                  state=CASE WHEN cs2_price_cache.steam_ask IS NULL THEN 'error' ELSE 'stale' END,
                  last_http_error=excluded.last_http_error,retry_after_at=excluded.retry_after_at
            """, (*self._key(position), current.isoformat(), state.value,
                  http_status, retry.isoformat()))

    def due_positions(self, positions, *, now: datetime | None = None,
                      force: bool = False) -> tuple[PricePosition, ...]:
        """Return positions eligible for refresh.

        Manual refresh bypasses the six-hour cache age, but never bypasses
        provider Retry-After or the local ten-minute repeat-request guard.
        """
        current = _utc(now)
        due = []
        seen = set()
        with self._connect() as db:
            for position in positions:
                key = self._key(position)
                if key in seen:
                    continue
                seen.add(key)
                row = db.execute("""
                    SELECT fetched_at,retry_after_at,last_attempt_at FROM cs2_price_cache
                    WHERE appid=? AND market_hash_name=? AND pattern=?
                """, key).fetchone()
                if row is None:
                    due.append(position)
                    continue
                fetched = datetime.fromisoformat(row[0]) if row[0] else None
                retry_after = datetime.fromisoformat(row[1]) if row[1] else None
                last_attempt = datetime.fromisoformat(row[2]) if row[2] else None
                if retry_after and current < _utc(retry_after):
                    continue
                if force:
                    if (last_attempt is not None
                            and current - _utc(last_attempt) < self.MANUAL_REFRESH_INTERVAL):
                        continue
                    due.append(position)
                elif fetched is None or current - _utc(fetched) >= self.REFRESH_INTERVAL:
                    due.append(position)
        return tuple(due)

    def get_fx_rate(self, *, now: datetime | None = None) -> CachedExchangeRate | None:
        with self._connect() as db:
            row = db.execute("""
                SELECT rate,source_date,fetched_at,last_http_error,retry_after_at
                FROM cs2_fx_rate_cache WHERE base='USD' AND quote='RUB'
            """).fetchone()
        if row is None or row[0] is None or row[1] is None or row[2] is None:
            return None
        try:
            rate = Decimal(row[0])
        except InvalidOperation:
            return None
        fetched = datetime.fromisoformat(row[2])
        retry = datetime.fromisoformat(row[4]) if row[4] else None
        stale = bool(row[3]) or _utc(now) - _utc(fetched) >= timedelta(days=1)
        return CachedExchangeRate(rate, row[1], row[2], row[3], stale,
                                  retry.isoformat() if retry else None)

    def fx_rate_due(self, *, now: datetime | None = None) -> bool:
        current = _utc(now)
        with self._connect() as db:
            row = db.execute("""
                SELECT fetched_at,retry_after_at FROM cs2_fx_rate_cache
                WHERE base='USD' AND quote='RUB'
            """).fetchone()
        if row is None:
            return True
        retry = datetime.fromisoformat(row[1]) if row[1] else None
        if retry and current < _utc(retry):
            return False
        fetched = datetime.fromisoformat(row[0]) if row[0] else None
        return fetched is None or current - _utc(fetched) >= timedelta(days=1)

    def save_fx_rate(self, rate: Decimal, source_date: str, *, now: datetime | None = None):
        if isinstance(rate, bool) or isinstance(rate, float):
            raise TypeError("USD/RUB rate must use Decimal, not float")
        rate = Decimal(rate)
        if not rate.is_finite() or rate <= 0:
            raise ValueError("USD/RUB rate must be positive")
        date.fromisoformat(source_date)
        timestamp = _utc(now).isoformat()
        with self._connect() as db:
            db.execute("""
                INSERT INTO cs2_fx_rate_cache
                (base,quote,rate,source_date,fetched_at,last_attempt_at,last_http_error,retry_after_at)
                VALUES ('USD','RUB',?,?,?,?,NULL,NULL)
                ON CONFLICT(base,quote) DO UPDATE SET rate=excluded.rate,
                  source_date=excluded.source_date,fetched_at=excluded.fetched_at,
                  last_attempt_at=excluded.last_attempt_at,last_http_error=NULL,retry_after_at=NULL
            """, (str(rate), source_date, timestamp, timestamp))

    def mark_fx_error(self, http_status: int | None, *, retry_after_seconds: int | None = None,
                      now: datetime | None = None):
        current = _utc(now)
        retry = self._retry_at(http_status, retry_after_seconds, current)
        with self._connect() as db:
            db.execute("""
                INSERT INTO cs2_fx_rate_cache
                (base,quote,last_attempt_at,last_http_error,retry_after_at)
                VALUES ('USD','RUB',?,?,?)
                ON CONFLICT(base,quote) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                  last_http_error=excluded.last_http_error,retry_after_at=excluded.retry_after_at
            """, (current.isoformat(), http_status, retry.isoformat()))
