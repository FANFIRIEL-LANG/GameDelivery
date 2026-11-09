"""Exact Steam Ask valuation for physical CS2 inventory assets."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Mapping

from games.cs2.inventory_view import CS2InventoryInstance
from integrations.openskin.client import OpenSkinPrice, PricePosition


PRICE_THRESHOLD_USD = Decimal("0.05")
CENT = Decimal("0.01")
_PATTERN_MARKERS = (
    "doppler", "case hardened", "heat treated", "fade", "crimson web", "slaughter",
)


class PriceState(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"
    ERROR = "error"
    BELOW_THRESHOLD = "below_threshold"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class CachedPrice:
    position: PricePosition
    steam_ask: Decimal | None
    currency: str | None
    source_updated_at: str | None
    fetched_at: str | None
    state: PriceState = PriceState.CURRENT
    last_http_error: int | None = None
    price_rub: Decimal | None = None
    usd_rub_rate: Decimal | None = None
    usd_rub_source_date: str | None = None
    usd_rub_fetched_at: str | None = None

    def __post_init__(self):
        if self.steam_ask is not None:
            if isinstance(self.steam_ask, bool) or isinstance(self.steam_ask, float):
                raise TypeError("Steam Ask must use Decimal, not float")
            value = (Decimal(self.steam_ask) if isinstance(self.steam_ask, int)
                     else Decimal(str(self.steam_ask)))
            if not value.is_finite() or value < 0:
                raise ValueError("Steam Ask must be a finite non-negative Decimal")
            object.__setattr__(self, "steam_ask", value)
        for field in ("price_rub", "usd_rub_rate"):
            value = getattr(self, field)
            if value is not None:
                if isinstance(value, (bool, float)):
                    raise TypeError(f"{field} must use Decimal, not float")
                value = Decimal(value) if isinstance(value, int) else Decimal(str(value))
                if not value.is_finite() or value < 0:
                    raise ValueError(f"{field} must be a finite non-negative Decimal")
                object.__setattr__(self, field, value)
        if not isinstance(self.state, PriceState):
            object.__setattr__(self, "state", PriceState(self.state))


@dataclass(frozen=True)
class AssetValuation:
    physical_key: tuple[str, str, str]
    position: PricePosition | None
    price_usd: Decimal | None
    price_rub: Decimal | None
    state: PriceState
    source_updated_at: str | None
    fetched_at: str | None = None
    is_stale: bool = False
    last_http_error: int | None = None


@dataclass(frozen=True)
class BotValuation:
    bot_name: str
    total_rub: Decimal | None
    priced_instances: int
    below_threshold_instances: int
    unknown_price_instances: int
    awaiting_rate_instances: int
    error_price_instances: int
    unsupported_instances: int


@dataclass(frozen=True)
class InventoryValuation:
    assets: tuple[AssetValuation, ...]
    bot_totals: tuple[BotValuation, ...]
    selected_total_rub: Decimal | None
    priced_instances: int
    below_threshold_instances: int
    unknown_price_instances: int
    awaiting_rate_instances: int
    stale_price_instances: int
    rate_is_stale: bool
    error_price_instances: int
    unsupported_instances: int

    @property
    def is_partial(self) -> bool:
        return bool(self.unknown_price_instances or self.awaiting_rate_instances)


def _needs_exact_pattern(instance: CS2InventoryInstance) -> bool:
    name = instance.market_hash_name.casefold()
    tags = " ".join(instance.tags).casefold()
    return any(marker in name or marker in tags for marker in _PATTERN_MARKERS)


def price_position_for(instance: CS2InventoryInstance) -> PricePosition | None:
    if instance.appid != 730 or instance.context_id != 2 or not instance.market_hash_name:
        return None
    return PricePosition(730, instance.market_hash_name)


def requestable_positions(instances) -> tuple[PricePosition, ...]:
    positions = {position for item in instances
                 if (position := price_position_for(item)) is not None
                 and not _needs_exact_pattern(item)}
    return tuple(sorted(positions, key=lambda value: (
        value.appid, value.market_hash_name, value.pattern or ""
    )))


def unsupported_positions(instances) -> tuple[PricePosition, ...]:
    positions = {position for item in instances
                 if (position := price_position_for(item)) is not None
                 and _needs_exact_pattern(item)}
    return tuple(sorted(positions, key=lambda value: (
        value.appid, value.market_hash_name, value.pattern or ""
    )))


def cached_price_from_openskin(price: OpenSkinPrice, *, fetched_at: datetime | None = None) -> CachedPrice:
    ask = price.steam_ask
    state = (
        PriceState.UNKNOWN if ask is None or price.currency != "USD"
        else PriceState.BELOW_THRESHOLD if ask < PRICE_THRESHOLD_USD
        else PriceState.CURRENT
    )
    fetched = fetched_at or datetime.now(timezone.utc)
    return CachedPrice(price.position, ask, price.currency, price.source_updated_at,
                       fetched.isoformat(), state)


def calculate_valuation(
    instances,
    quotes: Mapping[PricePosition, CachedPrice],
    usd_rub: Decimal | None,
    *,
    selected_bots=None,
    rate_is_stale: bool = False,
) -> InventoryValuation:
    instances = tuple(instances)
    seen = set()
    selected = None if selected_bots is None else {str(name) for name in selected_bots}
    instances = tuple(item for item in instances if selected is None or item.bot_name in selected)
    for item in instances:
        if item.physical_key in seen:
            raise ValueError(f"duplicate physical asset: {item.physical_key!r}")
        seen.add(item.physical_key)
    if usd_rub is not None:
        if isinstance(usd_rub, bool) or isinstance(usd_rub, float):
            raise TypeError("USD/RUB rate must use Decimal, not float")
        usd_rub = Decimal(usd_rub) if isinstance(usd_rub, int) else Decimal(str(usd_rub))
        if not usd_rub.is_finite() or usd_rub <= 0:
            raise ValueError("USD/RUB rate must be a positive finite Decimal")

    assets = []
    by_bot: dict[str, list[AssetValuation]] = {}
    for item in instances:
        position = price_position_for(item)
        quote = quotes.get(position) if position is not None else None
        if position is None:
            asset = AssetValuation(item.physical_key, None, None, None,
                                   PriceState.UNSUPPORTED, None)
        elif _needs_exact_pattern(item):
            asset = AssetValuation(item.physical_key, position, None, None,
                                   PriceState.UNSUPPORTED, None)
        elif quote is None:
            asset = AssetValuation(item.physical_key, position, None, None,
                                   PriceState.UNKNOWN, None)
        elif quote.steam_ask is None or quote.currency != "USD":
            state = quote.state if quote.state in {PriceState.ERROR, PriceState.STALE} else PriceState.UNKNOWN
            asset = AssetValuation(item.physical_key, position, None, None, state,
                                   quote.source_updated_at, quote.fetched_at,
                                   quote.state is PriceState.STALE, quote.last_http_error)
        elif quote.steam_ask < PRICE_THRESHOLD_USD:
            asset = AssetValuation(item.physical_key, position, quote.steam_ask, None,
                                   PriceState.BELOW_THRESHOLD, quote.source_updated_at,
                                   quote.fetched_at, quote.state is PriceState.STALE,
                                   quote.last_http_error)
        elif usd_rub is None:
            asset = AssetValuation(item.physical_key, position, quote.steam_ask, None,
                                   PriceState.STALE if quote.state is PriceState.STALE else PriceState.CURRENT,
                                   quote.source_updated_at, quote.fetched_at,
                                   quote.state is PriceState.STALE or rate_is_stale,
                                   quote.last_http_error)
        else:
            rub = (quote.steam_ask * usd_rub).quantize(CENT, rounding=ROUND_HALF_UP)
            asset = AssetValuation(item.physical_key, position, quote.steam_ask, rub,
                                   PriceState.STALE if quote.state is PriceState.STALE else PriceState.CURRENT,
                                   quote.source_updated_at, quote.fetched_at,
                                   quote.state is PriceState.STALE or rate_is_stale,
                                   quote.last_http_error)
        assets.append(asset)
        by_bot.setdefault(item.bot_name, []).append(asset)

    bot_names = (selected if selected is not None else set(by_bot))
    bot_totals = []
    for bot_name in sorted(bot_names, key=str.casefold):
        bot_assets = by_bot.get(bot_name, [])
        converted = [asset.price_rub for asset in bot_assets if asset.price_rub is not None]
        awaiting = sum(asset.price_usd is not None and asset.price_rub is None
                       and asset.state in {PriceState.CURRENT, PriceState.STALE}
                       for asset in bot_assets)
        bot_total = (sum(converted, Decimal(0)) if converted
                     else Decimal(0) if awaiting else None)
        bot_totals.append(BotValuation(
            bot_name, bot_total,
            sum(asset.price_rub is not None for asset in bot_assets),
            sum(asset.state is PriceState.BELOW_THRESHOLD for asset in bot_assets),
            sum(asset.state in {PriceState.UNKNOWN, PriceState.UNSUPPORTED, PriceState.ERROR}
                for asset in bot_assets),
            awaiting,
            sum(asset.state is PriceState.ERROR or asset.last_http_error is not None
                for asset in bot_assets),
            sum(asset.state is PriceState.UNSUPPORTED for asset in bot_assets),
        ))

    converted = [asset.price_rub for asset in assets if asset.price_rub is not None]
    awaiting = sum(asset.price_usd is not None and asset.price_rub is None
                   and asset.state in {PriceState.CURRENT, PriceState.STALE} for asset in assets)
    return InventoryValuation(
        tuple(assets), tuple(bot_totals),
        sum(converted, Decimal(0)) if converted else Decimal(0) if awaiting else None,
        sum(asset.price_rub is not None for asset in assets),
        sum(asset.state is PriceState.BELOW_THRESHOLD for asset in assets),
        sum(asset.state in {PriceState.UNKNOWN, PriceState.UNSUPPORTED, PriceState.ERROR}
            for asset in assets),
        awaiting,
        sum(asset.is_stale for asset in assets), rate_is_stale,
        sum(asset.state is PriceState.ERROR or asset.last_http_error is not None
            for asset in assets),
        sum(asset.state is PriceState.UNSUPPORTED for asset in assets),
    )


def format_usd(value: Decimal) -> str:
    return "$" + format(value.normalize(), "f")


def asset_price_text(asset: AssetValuation) -> str:
    if asset.state is PriceState.UNSUPPORTED:
        return "Цена неизвестна: требуется точный pattern/seed"
    if asset.state is PriceState.ERROR:
        suffix = f" HTTP {asset.last_http_error}" if asset.last_http_error else ""
        return f"Цена недоступна: ошибка OpenSkin{suffix}"
    if asset.state is PriceState.UNKNOWN:
        return "Цена неизвестна"
    if asset.state is PriceState.BELOW_THRESHOLD and asset.price_usd is not None:
        stale = " · устарела" if asset.is_stale else ""
        return f"Цена актива: {format_usd(asset.price_usd)} · не учитывается в оценке (< $0.05){stale}"
    if asset.price_rub is not None:
        suffix = " · устарела" if asset.is_stale else ""
        return f"Цена актива: ₽{asset.price_rub:.2f}{suffix}"
    if asset.price_usd is not None:
        return f"Цена актива: {format_usd(asset.price_usd)} · цена в ₽ недоступна"
    return "Цена актива: недоступна"


def group_price_text(instances, valuation: InventoryValuation) -> str:
    by_key = {asset.physical_key: asset for asset in valuation.assets}
    assets = [by_key[item.physical_key] for item in instances if item.physical_key in by_key]
    if not assets:
        return "Цена неизвестна"
    if all(asset.state is PriceState.UNSUPPORTED for asset in assets):
        return "Цена неизвестна: требуется точный pattern/seed"
    if all(asset.state is PriceState.ERROR for asset in assets):
        statuses = sorted({asset.last_http_error for asset in assets if asset.last_http_error})
        suffix = f" · HTTP {statuses[-1]}" if statuses else ""
        return f"Цена недоступна: ошибка OpenSkin{suffix}"
    rub_values = [asset.price_rub for asset in assets if asset.price_rub is not None]
    if rub_values:
        partial = len(rub_values) != len(assets)
        label = "Частичная стоимость группы" if partial else "Стоимость группы"
        total = sum(rub_values, Decimal(0))
        unit = next((asset.price_rub for asset in assets if asset.price_rub is not None), None)
        tail = f" · {label}: ₽{total:.2f}"
        if partial:
            tail += f" ({len(rub_values)} из {len(assets)} оценено)"
        stale = " · есть устаревшие данные" if any(asset.is_stale for asset in assets) else ""
        return f"Цена единицы: ₽{unit:.2f}{tail}{stale}"
    below = [asset for asset in assets if asset.state is PriceState.BELOW_THRESHOLD]
    if below and len(below) == len(assets):
        return (f"Цена: {format_usd(below[0].price_usd)} · "
                "Не учитывается в оценке — цена ниже $0.05")
    usd = next((asset.price_usd for asset in assets if asset.price_usd is not None), None)
    if usd is not None:
        return f"Цена в ₽ недоступна · исходный Steam Ask: {format_usd(usd)}"
    return "Цена неизвестна"
