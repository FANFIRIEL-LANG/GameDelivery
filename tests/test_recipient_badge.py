import unittest

from games.steam_cards.recipient_badge import (
    RecipientBadge,
    evaluate_recipient_badge_capacity,
)


class RecipientBadgeCapacityTests(unittest.TestCase):
    def test_verified_missing_badge_means_level_zero(self):
        result = evaluate_recipient_badge_capacity(
            "Example Game", 5, [], badge_data_verified=True
        )
        self.assertEqual(result.current_level, 0)
        self.assertEqual(result.remaining_sets, 5)
        self.assertTrue(result.can_proceed)

    def test_game_name_match_is_case_insensitive_and_exact(self):
        result = evaluate_recipient_badge_capacity(
            "Example Game", 2,
            [RecipientBadge("example game", 3), RecipientBadge("Example Game Deluxe", 5)],
            badge_data_verified=True,
        )
        self.assertEqual(result.current_level, 3)
        self.assertEqual(result.remaining_sets, 2)
        self.assertTrue(result.can_proceed)

    def test_order_larger_than_remaining_is_blocked_and_capped(self):
        result = evaluate_recipient_badge_capacity(
            "Example Game", 3, [RecipientBadge("Example Game", 4)],
            badge_data_verified=True,
        )
        self.assertEqual(result.remaining_sets, 1)
        self.assertEqual(result.allowed_sets, 1)
        self.assertFalse(result.can_proceed)

    def test_max_level_blocks_normal_sets(self):
        result = evaluate_recipient_badge_capacity(
            "Example Game", 1, [RecipientBadge("Example Game", 5)],
            badge_data_verified=True,
        )
        self.assertEqual(result.allowed_sets, 0)
        self.assertFalse(result.can_proceed)

    def test_unverified_badge_data_fails_closed(self):
        result = evaluate_recipient_badge_capacity(
            "Example Game", 1, [], badge_data_verified=False
        )
        self.assertEqual(result.allowed_sets, 0)
        self.assertIsNone(result.remaining_sets)
        self.assertFalse(result.can_proceed)

    def test_ambiguous_badges_fail_closed(self):
        result = evaluate_recipient_badge_capacity(
            "Example Game", 1,
            [RecipientBadge("Example Game", 1), RecipientBadge("example game", 2)],
            badge_data_verified=True,
        )
        self.assertEqual(result.allowed_sets, 0)
        self.assertFalse(result.can_proceed)

    def test_foil_badge_is_not_treated_as_normal_badge(self):
        result = evaluate_recipient_badge_capacity(
            "Example Game", 1, [RecipientBadge("Example Game", 1, is_foil=True)],
            badge_data_verified=True,
        )
        self.assertEqual(result.allowed_sets, 0)
        self.assertFalse(result.can_proceed)

    def test_invalid_requested_set_count_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_recipient_badge_capacity("Example Game", 0, [], badge_data_verified=True)


if __name__ == "__main__":
    unittest.main()
