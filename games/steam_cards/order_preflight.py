from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from games.catalog import GAME_CATALOG
from games.steam_cards.badge_allocation import (
    GameAllocation,
    GameCapacity,
    plan_badge_allocation,
)
from games.steam_cards.owned_selection import OwnedAsset, OwnedSet, select_owned_complete_sets
from games.steam_cards.recipient_badge import BadgeCapacity, RecipientBadge, evaluate_recipient_badge_capacity
from games.steam_cards.reservations import AssetReservationStore
from games.steam_cards.selection import CardCopy
from games.steam_cards.shared_sets import SharedGameSets, find_shared_game_sets


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


@dataclass(frozen=True)
class MultiGameOrderPreflight:
    order_id: str
    requested_sets: int
    allocations: tuple[GameAllocation, ...]
    selected_sets: tuple[OwnedSet, ...]
    reserved_assets: tuple[tuple[str, str], ...]
    ready_for_manual_review: bool
    message: str


def _reservation_key(bot_name: str, asset_id: str) -> tuple[str, str]:
    asset = str(asset_id).strip()
    if asset.isdigit() and int(asset) > 0:
        asset = str(int(asset))
    return bot_name.strip().casefold(), asset


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

    def prepare_multi_game(
        self,
        *,
        order_id: str,
        requested_sets: int,
        inventories: Mapping[str, Iterable[CardCopy]],
        recipient_badges: Iterable[RecipientBadge],
        badge_data_verified: bool,
        buyer_complete_sets_by_app_id: Mapping[int, int],
        buyer_inventory_verified: bool,
        seller_inventory_verified: bool,
        card_hashes_by_app_id: Mapping[int, Iterable[str]],
        card_definitions_verified: bool,
    ) -> MultiGameOrderPreflight:
        """Plan and reserve an exact multi-game order; never sends a trade."""
        order = order_id.strip()
        if not order:
            raise ValueError("order_id обязателен")
        if (
            isinstance(requested_sets, bool)
            or not isinstance(requested_sets, int)
            or requested_sets < 1
        ):
            raise ValueError("Количество комплектов должно быть целым числом больше нуля")
        if not isinstance(badge_data_verified, bool):
            raise ValueError("badge_data_verified должен быть bool")
        if not isinstance(buyer_inventory_verified, bool):
            raise ValueError("buyer_inventory_verified должен быть bool")
        if not isinstance(seller_inventory_verified, bool):
            raise ValueError("seller_inventory_verified должен быть bool")
        if not isinstance(card_definitions_verified, bool):
            raise ValueError("card_definitions_verified должен быть bool")
        if not isinstance(buyer_complete_sets_by_app_id, Mapping):
            raise ValueError("buyer_complete_sets_by_app_id должен быть Mapping")
        if not isinstance(card_hashes_by_app_id, Mapping):
            raise ValueError("card_hashes_by_app_id должен быть Mapping")

        def blocked(
            message: str,
            allocations: tuple[GameAllocation, ...] = (),
            selected: tuple[OwnedSet, ...] = (),
        ) -> MultiGameOrderPreflight:
            return MultiGameOrderPreflight(
                order, requested_sets, allocations, selected, (), False, message
            )

        if not buyer_inventory_verified:
            return blocked("Полные наборы в инвентаре покупателя не подтверждены")
        if not seller_inventory_verified:
            return blocked("Снимки инвентаря ботов не подтверждены или устарели")
        if not card_definitions_verified:
            return blocked("Составы наборов по MarketHashName не подтверждены")

        reserved = {
            _reservation_key(bot_name, asset_id)
            for bot_name, asset_id in self.reservations.list_reserved_assets(
                excluding_order_id=order
            )
        }
        inventory_lists = {bot_name: list(cards) for bot_name, cards in inventories.items()}
        available_inventories = {
            bot_name: [
                card for card in cards
                if _reservation_key(bot_name, card.asset_id) not in reserved
            ]
            for bot_name, cards in inventory_lists.items()
        }

        catalog_by_name = {
            game.name.strip().casefold(): game.app_id
            for game in GAME_CATALOG
            if game.module_id != "steam_cards"
        }
        badges = tuple(recipient_badges)
        game_names: dict[str, str] = {}
        for cards in inventory_lists.values():
            for card in cards:
                game_name = card.game_name.strip()
                if not card.is_trading_card or not game_name:
                    continue
                key = game_name.casefold()
                if key in game_names and game_names[key] != game_name:
                    return blocked(f"Название игры неоднозначно: {game_name}")
                game_names[key] = game_name

        game_inputs: dict[int, tuple[str, int]] = {}
        expected_hashes_by_game: dict[str, tuple[str, ...]] = {}
        for game_key, game_name in sorted(game_names.items()):
            app_id = catalog_by_name.get(game_key)
            if app_id is None:
                return blocked(f"Для игры {game_name} нет проверенного AppID")
            if app_id in game_inputs:
                return blocked(f"AppID {app_id} связан с несколькими названиями игр")
            raw_hashes = card_hashes_by_app_id.get(app_id)
            if raw_hashes is None or isinstance(raw_hashes, str):
                return blocked(f"Нет полного состава набора для AppID {app_id}")
            hashes = tuple(raw_hashes)
            if (
                not hashes
                or any(not isinstance(value, str) or not value.strip() for value in hashes)
            ):
                return blocked(f"Состав набора для AppID {app_id} некорректен")
            hashes = tuple(value.strip() for value in hashes)
            if len(set(hashes)) != len(hashes):
                return blocked(f"Состав набора для AppID {app_id} содержит повтор")

            if app_id not in buyer_complete_sets_by_app_id:
                return blocked(f"Нет подтверждённого количества наборов покупателя для AppID {app_id}")

            buyer_sets = buyer_complete_sets_by_app_id[app_id]
            if isinstance(buyer_sets, bool) or not isinstance(buyer_sets, int) or buyer_sets < 0:
                raise ValueError(f"Количество наборов покупателя для AppID {app_id} некорректно")

            badge_capacity = evaluate_recipient_badge_capacity(
                game_name,
                1,
                badges,
                badge_data_verified=badge_data_verified,
            )
            if badge_capacity.remaining_sets is None:
                return blocked(
                    f"Прогресс значка для {game_name} не подтверждён: "
                    f"{badge_capacity.reason}"
                )

            remaining_sets = max(0, badge_capacity.remaining_sets - buyer_sets)
            game_inputs[app_id] = (game_name, remaining_sets)
            expected_hashes_by_game[game_name] = hashes

        shared_games = find_shared_game_sets(
            available_inventories,
            expected_card_hashes_by_game_name=expected_hashes_by_game,
        )
        shared_by_name = {game.game_name.casefold(): game for game in shared_games}
        capacities: list[GameCapacity] = []
        game_by_app_id: dict[int, SharedGameSets] = {}
        for app_id, (game_name, remaining_sets) in game_inputs.items():
            shared_game = shared_by_name.get(game_name.casefold())
            available_sets = shared_game.available_sets if shared_game else 0
            capacities.append(GameCapacity(app_id, remaining_sets, available_sets))
            if shared_game:
                game_by_app_id[app_id] = shared_game

        try:
            allocations = plan_badge_allocation(capacities, requested_sets=requested_sets)
        except ValueError as exc:
            return blocked(f"Точное количество наборов недоступно: {exc}")

        selected: list[OwnedSet] = []
        for allocation in allocations:
            shared_game = game_by_app_id[allocation.app_id]
            selected.extend(
                OwnedSet(
                    game_name=shared_game.game_name,
                    cards_per_set=shared_game.cards_per_set,
                    asset_ids=tuple(
                        OwnedAsset(bot_name, asset_id)
                        for bot_name, asset_id in asset_set
                    ),
                )
                for asset_set in shared_game.sets[:allocation.sets]
            )
        selected_sets = tuple(selected)
        assets = tuple(
            (asset.bot_name, asset.asset_id)
            for selected_set in selected_sets
            for asset in selected_set.asset_ids
        )

        existing_assets = {
            _reservation_key(row["bot_name"], row["asset_id"])
            for row in self.reservations.list_for_order(order)
        }
        planned_assets = {_reservation_key(bot_name, asset_id) for bot_name, asset_id in assets}
        if existing_assets and existing_assets != planned_assets:
            return blocked(
                "Для заказа уже зарезервирован другой состав карточек; "
                "сначала требуется явно отменить прежний preflight.",
                allocations,
                selected_sets,
            )

        reservation = self.reservations.reserve(order, assets)
        if not reservation["reserved"]:
            conflicts = reservation["conflicts"]
            details = ", ".join(
                f"{item['bot_name']}:{item['asset_id']} (заказ {item['reserved_by']})"
                for item in conflicts
            )
            return blocked(
                f"Заказ заблокирован: карточки уже зарезервированы — {details}",
                allocations,
                selected_sets,
            )

        return MultiGameOrderPreflight(
            order,
            requested_sets,
            allocations,
            selected_sets,
            tuple(reservation["assets"]),
            True,
            "Подготовлено к ручной проверке. Отправка обмена не выполнялась.",
        )

    def cancel_preflight(self, order_id: str) -> int:
        """Release local reservations when a human rejects the review."""
        return self.reservations.release(order_id)
