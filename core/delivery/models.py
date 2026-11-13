from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class Listing:
    listing_id: str
    source_platform: str
    external_listing_id: str | None
    title: str
    status: str
    auto_relist: bool = False


@dataclass(frozen=True)
class ConfigVersion:
    config_version_id: str
    listing_id: str
    version_number: int
    delivery_quantity_per_deal: int | None
    item_selectors: dict[str, Any] | list[Any]
    bot_pool: tuple[str, ...]


@dataclass(frozen=True)
class Deal:
    deal_id: str
    source_platform: str
    external_deal_id: str
    chat_id: str | None
    listing_id: str
    config_version_id: str
    platform_status: str


@dataclass(frozen=True)
class Order:
    order_id: str
    deal_id: str
    status: str
    total_quantity: int


@dataclass(frozen=True)
class OrderLine:
    order_line_id: str
    order_id: str
    line_number: int
    item_type: str
    item_name: str
    item_selector: dict[str, Any] | list[Any]
    quantity: int


@dataclass(frozen=True)
class DeliveryLeg:
    leg_id: str
    order_line_id: str
    bot_name: str
    app_id: int
    context_id: str
    asset_ids: tuple[str, ...]
    trade_offer_id: str | None
    status: str


@dataclass(frozen=True)
class DeliveryAttempt:
    attempt_id: str
    order_id: str
    leg_id: str
    retry_generation: int
    idempotency_key: str
    status: str


@dataclass(frozen=True)
class ResourceReservation:
    reservation_id: str
    owner_type: Literal["listing", "order", "legacy"]
    listing_id: str | None
    order_line_id: str | None
    game_id: str
    app_id: int
    context_id: str
    bot_name: str
    asset_id: str
    units: int
    state: Literal["active", "released", "consumed", "legacy_conflict"]
