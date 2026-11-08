import unittest
from itertools import product

from games.steam_cards.badge_allocation import (
    GameCapacity,
    GameAllocation,
    plan_badge_allocation,
)


class BadgeAllocationTests(unittest.TestCase):
    def test_matches_exhaustive_reference_for_small_capacity_space(self):
        app_ids = (730, 570, 440)
        capacity_options = tuple(product(range(3), range(4)))

        for capacities in product(capacity_options, repeat=len(app_ids)):
            games = tuple(
                GameCapacity(app_id, remaining, available)
                for app_id, (remaining, available) in zip(app_ids, capacities)
            )
            for requested_sets in range(1, 7):
                expected = _exhaustive_reference(games, requested_sets)
                with self.subTest(capacities=capacities, requested_sets=requested_sets):
                    if expected is None:
                        with self.assertRaisesRegex(ValueError, "exact quantity"):
                            plan_badge_allocation(games, requested_sets=requested_sets)
                    else:
                        self.assertEqual(
                            plan_badge_allocation(games, requested_sets=requested_sets),
                            expected,
                        )

    def test_maximizes_completed_badges_for_exact_order(self):
        result = plan_badge_allocation(
            [GameCapacity(440, 5, 5), GameCapacity(570, 5, 5), GameCapacity(730, 2, 2)],
            requested_sets=10,
        )

        self.assertEqual(result, (
            GameAllocation(440, 5, completes_badge=True),
            GameAllocation(570, 3, completes_badge=False),
            GameAllocation(730, 2, completes_badge=True),
        ))

    def test_minimizes_remaining_sets_for_the_maximum_completion_count(self):
        result = plan_badge_allocation(
            [
                GameCapacity(40, 1, 1),
                GameCapacity(20, 2, 2),
                GameCapacity(30, 4, 4),
                GameCapacity(10, 5, 5),
            ],
            requested_sets=8,
        )

        self.assertEqual(result, (
            GameAllocation(10, 1, completes_badge=False),
            GameAllocation(20, 2, completes_badge=True),
            GameAllocation(30, 4, completes_badge=True),
            GameAllocation(40, 1, completes_badge=True),
        ))

    def test_breaks_equal_completion_cost_by_app_id(self):
        result = plan_badge_allocation(
            [GameCapacity(570, 2, 2), GameCapacity(440, 2, 2), GameCapacity(730, 5, 5)],
            requested_sets=2,
        )

        self.assertEqual(result, (GameAllocation(440, 2, completes_badge=True),))

    def test_allocates_remainder_by_remaining_sets_then_app_id(self):
        result = plan_badge_allocation(
            [
                GameCapacity(10, 2, 2),
                GameCapacity(30, 3, 3),
                GameCapacity(20, 3, 3),
                GameCapacity(40, 4, 4),
            ],
            requested_sets=4,
        )

        self.assertEqual(result, (
            GameAllocation(10, 2, completes_badge=True),
            GameAllocation(20, 2, completes_badge=False),
        ))

    def test_rejects_infeasible_exact_quantity_without_partial_plan(self):
        with self.assertRaisesRegex(ValueError, "exact quantity"):
            plan_badge_allocation(
                [GameCapacity(440, 2, 1), GameCapacity(570, 4, 0)],
                requested_sets=2,
            )

    def test_skips_games_with_no_remaining_or_available_sets(self):
        result = plan_badge_allocation(
            [GameCapacity(10, 0, 4), GameCapacity(20, 3, 0), GameCapacity(30, 2, 2)],
            requested_sets=2,
        )

        self.assertEqual(result, (GameAllocation(30, 2, completes_badge=True),))

    def test_rejects_invalid_quantities_and_duplicate_app_ids(self):
        for requested_sets in (0, -1, True, 1.5):
            with self.subTest(requested_sets=requested_sets):
                with self.assertRaises(ValueError):
                    plan_badge_allocation([], requested_sets=requested_sets)

        with self.assertRaisesRegex(ValueError, "AppID"):
            plan_badge_allocation(
                [GameCapacity(440, 1, 1), GameCapacity(440, 2, 2)],
                requested_sets=1,
            )

    def test_rejects_invalid_game_capacity_values(self):
        invalid_inputs = (
            ([GameCapacity(True, 1, 1)], "AppID"),
            ([GameCapacity(0, 1, 1)], "AppID"),
            ([GameCapacity(440, True, 1)], "remaining_sets"),
            ([GameCapacity(440, -1, 1)], "remaining_sets"),
            ([GameCapacity(440, 1.5, 1)], "remaining_sets"),
            ([GameCapacity(440, 1, True)], "available_sets"),
            ([GameCapacity(440, 1, -1)], "available_sets"),
            ([GameCapacity(440, 1, 1.5)], "available_sets"),
        )
        for games, message in invalid_inputs:
            with self.subTest(games=games):
                with self.assertRaisesRegex(ValueError, message):
                    plan_badge_allocation(games, requested_sets=1)


def _exhaustive_reference(games, requested_sets):
    """Return the best exact allocation by directly enumerating all choices."""
    best = None
    best_key = None
    max_sets = tuple(min(game.remaining_sets, game.available_sets) for game in games)

    for amounts in product(*(range(limit + 1) for limit in max_sets)):
        if sum(amounts) != requested_sets:
            continue

        by_app_id = dict(zip((game.app_id for game in games), amounts))
        completed = tuple(
            sorted(
                game.app_id
                for game in games
                if game.remaining_sets > 0
                and by_app_id[game.app_id] == game.remaining_sets
            )
        )
        completed_set = set(completed)
        incomplete = sorted(
            (game for game in games if game.app_id not in completed_set),
            key=lambda game: (game.remaining_sets, game.app_id),
        )
        key = (
            -len(completed),
            sum(game.remaining_sets for game in games if game.app_id in completed_set),
            completed,
            tuple(-by_app_id[game.app_id] for game in incomplete),
        )
        if best_key is None or key < best_key:
            best_key = key
            best = by_app_id

    if best is None:
        return None

    completed_set = set(best_key[2])
    return tuple(
        GameAllocation(game.app_id, best[game.app_id], game.app_id in completed_set)
        for game in sorted(games, key=lambda game: game.app_id)
        if best[game.app_id] > 0
    )


if __name__ == "__main__":
    unittest.main()
