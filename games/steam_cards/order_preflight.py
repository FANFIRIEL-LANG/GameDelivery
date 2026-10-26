from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from games.steam_cards.owned_selection import OwnedSet, select_owned_complete_sets
from games.steam_cards.recipient_badge import BadgeCapacity, RecipientBadge, evaluate_recipient_badge_capacity
from games.steam_cards.reservations import AssetReservationStore
from games.steam_cards.selection import CardCopy


@dataclass(frozen=True)
class OrderPreflight:
    order_id: str
    game_name: str
    requested_sets: int
    badge_capacity: BadgeCapacity
    selected_sets: tuple[OwnedSet, ...]
    reserved_assets: tuple[tuple[str, str], ...]
    ready_for_manual_review: bool
    message: str


class SteamCardOrderPreflight:
    """Prepare and reserve a card order for human review; never sends a trade."""

    def __init__(self, reservations: AssetReservationStore):
        self.reservations = reservations

    def prepare(
        self,
        *,
        order_id: str,
        game_name: str,
        requested_sets: int,
        inventories: Mapping[str, Iterable[CardCopy]],
        recipient_badges: Iterable[RecipientBadge],
        badge_data_verified: bool,
    ) -> OrderPreflight:
        order = order_id.strip()
        game = game_name.strip()
        if not order or not game:
            raise ValueError("order_id и game_name обязательны")

        capacity = evaluate_recipient_badge_capacity(
            game, requested_sets, recipient_badges,
            badge_data_verified=badge_data_verified,
        )
        if not capacity.can_proceed:
            return OrderPreflight(
                order, game, requested_sets, capacity, (), (), False,
                f"Заказ заблокирован проверкой значка: {capacity.reason}",
            )

        selected = select_owned_complete_sets(inventories, game, requested_sets)
        assets = tuple(
            (owned.bot_name, owned.asset_id)
            for selected_set in selected
            for owned in selected_set.asset_ids
        )
        reservation = self.reservations.reserve(order, assets)
        if not reservation["reserved"]:
            conflicts = reservation["conflicts"]
            details = ", ".join(
                f"{item['bot_name']}:{item['asset_id']} (заказ {item['reserved_by']})"
                for item in conflicts
            )
            return OrderPreflight(
                order, game, requested_sets, capacity, tuple(selected), (), False,
                f"Заказ заблокирован: карточки уже зарезервированы — {details}",
            )

        return OrderPreflight(
            order, game, requested_sets, capacity, tuple(selected), tuple(assets), True,
            "Подготовлено к ручной проверке. Отправка обмена не выполнялась.",
        )

    def cancel_preflight(self, order_id: str) -> int:
        """Release local reservations when a human rejects the review."""
        return self.reservations.release(order_id)
