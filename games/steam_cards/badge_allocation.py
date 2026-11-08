"""Pure deterministic allocation for multi-game badge orders."""

from dataclasses import dataclass


@dataclass(frozen=True)
class GameCapacity:
    app_id: int
    remaining_sets: int
    available_sets: int


@dataclass(frozen=True)
class GameAllocation:
    app_id: int
    sets: int
    completes_badge: bool


def plan_badge_allocation(
    games: list[GameCapacity], *, requested_sets: int
) -> tuple[GameAllocation, ...]:
    """Plan an exact order using the approved C.2.7 completion priorities."""
    if isinstance(requested_sets, bool) or not isinstance(requested_sets, int) or requested_sets <= 0:
        raise ValueError("requested_sets must be a positive integer")

    app_ids: set[int] = set()
    candidates: list[GameCapacity] = []
    incomplete_capacity = 0
    for game in games:
        if not isinstance(game.app_id, int) or isinstance(game.app_id, bool) or game.app_id <= 0:
            raise ValueError("AppID must be a positive integer")
        if game.app_id in app_ids:
            raise ValueError(f"duplicate AppID: {game.app_id}")
        app_ids.add(game.app_id)
        for name, value in (
            ("remaining_sets", game.remaining_sets),
            ("available_sets", game.available_sets),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

        if game.remaining_sets == 0:
            continue
        candidates.append(game)
        incomplete_capacity += min(game.available_sets, game.remaining_sets - 1)

    completable = sorted(
        (game for game in candidates if game.available_sets >= game.remaining_sets),
        key=lambda game: (game.remaining_sets, game.app_id),
    )

    completed: set[int] | None = None
    for count in range(min(len(completable), requested_sets), -1, -1):
        selected = completable[:count]
        completion_cost = sum(game.remaining_sets for game in selected)
        if completion_cost <= requested_sets <= incomplete_capacity + count:
            completed = {game.app_id for game in selected}
            break

    if completed is None:
        raise ValueError("cannot allocate exact quantity from available sets")

    allocations = {app_id: 0 for app_id in app_ids}
    for game in candidates:
        if game.app_id in completed:
            allocations[game.app_id] = game.remaining_sets

    remaining = requested_sets - sum(allocations.values())
    for game in sorted(candidates, key=lambda item: (item.remaining_sets, item.app_id)):
        if game.app_id in completed:
            continue
        amount = min(remaining, game.available_sets, game.remaining_sets - 1)
        allocations[game.app_id] = amount
        remaining -= amount
        if remaining == 0:
            break

    if remaining != 0:
        raise ValueError("cannot allocate exact quantity from available sets")

    return tuple(
        GameAllocation(
            app_id=game.app_id,
            sets=allocations[game.app_id],
            completes_badge=game.app_id in completed,
        )
        for game in sorted(candidates, key=lambda item: item.app_id)
        if allocations[game.app_id] > 0
    )
