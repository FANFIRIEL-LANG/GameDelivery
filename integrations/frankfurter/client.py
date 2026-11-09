"""Small standard-library client for Frankfurter's blended USD/RUB rate."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import urllib.error
import urllib.request
from typing import Callable


_ENDPOINT = "https://api.frankfurter.dev/v2/rate/usd/rub"


@dataclass(frozen=True)
class ExchangeRate:
    base: str
    quote: str
    rate: Decimal
    source_date: str
    fetched_at: datetime


class FrankfurterError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


def _request_url(url: str, timeout: float):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        raise FrankfurterError(f"Frankfurter вернул HTTP {exc.code}.",
                               http_status=exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise FrankfurterError("Не удалось получить курс USD/RUB.") from None


class FrankfurterClient:
    def __init__(self, *, timeout: float = 15.0,
                 request_func: Callable | None = None):
        self.timeout = timeout
        self._request_func = request_func or _request_url

    def fetch_usd_rub(self) -> ExchangeRate:
        try:
            status, raw = self._request_func(_ENDPOINT, self.timeout)
        except FrankfurterError:
            raise
        except Exception:
            raise FrankfurterError("Не удалось получить курс USD/RUB.") from None
        if status != 200:
            raise FrankfurterError(f"Frankfurter вернул HTTP {status}.", http_status=status)
        try:
            payload = json.loads(raw.decode("utf-8"), parse_float=Decimal)
            raw_rate = payload["rate"]
            if isinstance(raw_rate, bool) or not isinstance(raw_rate, (Decimal, int)):
                raise ValueError("rate must be numeric")
            rate = Decimal(raw_rate) if isinstance(raw_rate, int) else raw_rate
            source_date = payload["date"]
            parsed_date = date.fromisoformat(source_date)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError,
                ValueError, InvalidOperation):
            raise FrankfurterError("Frankfurter вернул некорректный курс.") from None
        if (payload.get("base") != "USD" or payload.get("quote") != "RUB"
                or not isinstance(source_date, str) or not rate.is_finite() or rate <= 0):
            raise FrankfurterError("Frankfurter вернул некорректный курс USD/RUB.")
        return ExchangeRate("USD", "RUB", rate, parsed_date.isoformat(),
                            datetime.now(timezone.utc))
