import unittest

from games.steam_cards.badge_allocation import (
    GameCapacity,
    GameAllocation,
    plan_badge_allocation,
)


class BadgeAllocationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
