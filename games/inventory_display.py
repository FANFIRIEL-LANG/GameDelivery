"""Shared, read-only display grouping for locally stored game inventory."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Literal

from games.inventory import InventoryItem


InventoryGroupingMode = Literal["shared", "separate"]


@dataclass(frozen=True)
class InventoryDisplayGroup:
    """Stable display group retaining every original physical inventory row."""

    identity: tuple[object, ...]
    items: tuple[InventoryItem, ...]


@dataclass(frozen=True)
class InventoryMetrics:
    """Counts of physical asset records; stack amounts are intentionally excluded."""

    total: int
    non_tradable: int
    owner_accounts: int
    tradable: int
    unknown_status: int


PRICE_UNAVAILABLE = "Цена недоступна: источник не настроен"


def count_physical_metrics(
    owners_and_statuses: Iterable[tuple[str, bool | None]],
) -> InventoryMetrics:
    total = non_tradable = tradable = unknown = 0
    owners: set[str] = set()
    for owner, status in owners_and_statuses:
        total += 1
        owner_name = str(owner).strip()
        if owner_name:
            owners.add(owner_name.casefold())
        if status is True:
            tradable += 1
        elif status is False:
            non_tradable += 1
        else:
            unknown += 1
    return InventoryMetrics(total, non_tradable, len(owners), tradable, unknown)


def build_inventory_groups(
    items: Iterable[InventoryItem], *, mode: InventoryGroupingMode = "shared"
) -> tuple[InventoryDisplayGroup, ...]:
    """Group equal game items without discarding owners, assets, or distinct flags."""
    if mode not in {"shared", "separate"}:
        raise ValueError("mode должен быть 'shared' или 'separate'")

    grouped: dict[tuple[object, ...], list[InventoryItem]] = defaultdict(list)
    for item in items:
        identity = (
            item.game_id, item.class_id, item.instance_id, item.name, item.item_type,
            item.market_hash_name, item.quality, item.rarity, item.exterior, item.tags,
            item.is_tradable, item.is_marketable,
            item.bot_name.casefold() if mode == "separate" else None,
        )
        grouped[identity].append(item)

    return tuple(
        InventoryDisplayGroup(identity, tuple(sorted(members, key=lambda item: (
            item.bot_name.casefold(), item.asset_id,
        ))))
        for identity, members in sorted(grouped.items(), key=lambda pair: tuple(
            "" if value is None else str(value).casefold() for value in pair[0]
        ))
    )
