from __future__ import annotations

import unittest

from games.steam_cards.recipient_diagnostics import normalize_recipient_card_diagnostics


RECIPIENT = 76561197960278073


def diagnostic():
    return {
        "success": True,
        "mode": "diagnostic-read-only",
        "sendEnabled": False,
        "tradeOfferCreated": False,
        "recipientSteamId64": RECIPIENT,
        "recipientIdentity": {"verified": True},
        "badgeChecks": [
            {
                "subject": "recipient", "appId": 570, "success": True,
                "httpStatusCode": 200, "responseIdentityVerified": True,
                "level": 4, "maxLevel": 5, "border": 0,
                "cardCount": 2, "uniqueHashCount": 2, "duplicateHashes": [],
            },
            {
                "subject": "recipient", "appId": 730, "success": True,
                "httpStatusCode": 200, "responseIdentityVerified": True,
                "level": 5, "maxLevel": 5, "border": 0,
                "cardCount": 2, "uniqueHashCount": 2, "duplicateHashes": [],
            },
        ],
        "comparisons": [
            {
                "appId": 570, "success": True,
                "matchedHashes": ["Dota Alpha"], "missingHashes": ["Dota Beta"],
                "duplicateBadgeHashes": [], "duplicateInventoryHashes": [],
            },
            {
                "appId": 730, "success": True,
                "matchedHashes": ["Game Alpha"], "missingHashes": ["Game Beta"],
                "duplicateBadgeHashes": [], "duplicateInventoryHashes": [],
            },
        ],
        "inventory": {
            "cards": [
                {"realAppID": 570, "marketHashName": "Dota Alpha", "amount": 2, "nonTradableAmount": 1},
                {"realAppID": 570, "marketHashName": "Dota Beta", "amount": 1, "nonTradableAmount": 0},
                {"realAppID": 730, "marketHashName": "Game Alpha", "amount": 3, "nonTradableAmount": 0},
                {"realAppID": 730, "marketHashName": "Game Beta", "amount": 0, "nonTradableAmount": 0},
            ],
            "foilAmount": 9, "otherTypeAmount": 10, "unknownTypeAmount": 0,
            "missingHashAmount": 0, "unknownAppAmount": 0,
            "invalidAmountCount": 0, "invalidEntryCount": 0,
        },
    }


class RecipientCardDiagnosticsTests(unittest.TestCase):
    def test_normalizes_verified_badges_complete_sets_and_exact_full_card_hashes(self):
        result = normalize_recipient_card_diagnostics(
            diagnostic(),
            recipient_steam_id64=RECIPIENT,
            game_names_by_app_id={570: "Dota 2", 730: "Example Game"},
        )
        self.assertEqual(
            [(badge.game_name, badge.level, badge.is_foil) for badge in result.recipient_badges],
            [("Dota 2", 4, False), ("Example Game", 5, False)],
        )
        self.assertEqual(result.buyer_complete_sets_by_app_id, {570: 1, 730: 0})
        self.assertEqual(
            result.card_hashes_by_app_id,
            {570: ("Dota Alpha", "Dota Beta"), 730: ("Game Alpha", "Game Beta")},
        )

    def test_rejects_wrong_identity_or_non_read_only_response(self):
        value = diagnostic()
        value["recipientIdentity"] = {"verified": False}
        with self.assertRaisesRegex(ValueError, "SteamID64"):
            normalize_recipient_card_diagnostics(
                value, recipient_steam_id64=RECIPIENT,
                game_names_by_app_id={570: "Dota 2", 730: "Example Game"},
            )
        value = diagnostic()
        value["sendEnabled"] = True
        with self.assertRaisesRegex(ValueError, "read-only"):
            normalize_recipient_card_diagnostics(
                value, recipient_steam_id64=RECIPIENT,
                game_names_by_app_id={570: "Dota 2", 730: "Example Game"},
            )

    def test_rejects_incomplete_badge_inventory_summary(self):
        value = diagnostic()
        value["inventory"]["missingHashAmount"] = 1
        with self.assertRaisesRegex(ValueError, "неполон"):
            normalize_recipient_card_diagnostics(
                value, recipient_steam_id64=RECIPIENT,
                game_names_by_app_id={570: "Dota 2", 730: "Example Game"},
            )
        value = diagnostic()
        value["badgeChecks"][0]["responseIdentityVerified"] = False
        with self.assertRaisesRegex(ValueError, "не подтверждён"):
            normalize_recipient_card_diagnostics(
                value, recipient_steam_id64=RECIPIENT,
                game_names_by_app_id={570: "Dota 2", 730: "Example Game"},
            )


if __name__ == "__main__":
    unittest.main()
