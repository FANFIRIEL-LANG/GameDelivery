from dataclasses import dataclass
import unittest

from core.delivery.stock_identity import StockItemIdentity


@dataclass(frozen=True)
class Spec:
    game_id: str = "cs2"
    app_id: int = 730
    context_id: str = "2"
    selector: dict | None = None
    allowed_bot_pool: tuple[str, ...] = ()


class StockItemIdentityTests(unittest.TestCase):
    def setUp(self):
        self.identity = StockItemIdentity(
            game_id="cs2", app_id=730, context_id="2",
            market_hash_name="AK-47 | Redline (Field-Tested)",
            name="AK-47 | Redline", item_type="weapon", class_id="123",
            quality="Field-Tested", exterior="Field-Tested", rarity="Classified",
        )

    def test_exact_selector_match_including_every_supplied_field(self):
        spec = Spec(selector={"market_hash_name": "AK-47 | Redline (Field-Tested)", "class_id": "123", "quality": "Field-Tested"})
        self.assertTrue(self.identity.matches_spec(spec))

    def test_selector_mismatch_fails_closed(self):
        selectors = (
            {"market_hash_name": "AK-47 | Redline"}, {"class_id": "124"},
            {"name": "AK-47 | Redline X"}, {"rarity": "Covert"},
        )
        for selector in selectors:
            with self.subTest(selector=selector):
                self.assertFalse(self.identity.matches_spec(Spec(selector=selector)))

    def test_missing_identity_field_does_not_match(self):
        incomplete = StockItemIdentity(game_id="cs2", app_id=730, context_id="2", market_hash_name="AK-47 | Redline (Field-Tested)")
        self.assertFalse(incomplete.matches_spec(Spec(selector={"name": "AK-47 | Redline"})))

    def test_game_app_context_scope_must_match(self):
        specs = (
            Spec(game_id="dota2", selector={"class_id": "123"}),
            Spec(app_id=570, selector={"class_id": "123"}),
            Spec(context_id="6", selector={"class_id": "123"}),
        )
        for spec in specs:
            with self.subTest(spec=spec):
                self.assertFalse(self.identity.matches_spec(spec))

    def test_placeholder_name_is_not_identity(self):
        item = StockItemIdentity(game_id="cs2", app_id=730, context_id="2", name="Unknown item 7656119").normalized()
        self.assertEqual(item.name, "")
        self.assertFalse(item.matches_spec(Spec(selector={"name": "Unknown item 7656119"})))

    def test_invalid_identity_values_fail_closed_or_are_rejected(self):
        with self.assertRaises(ValueError):
            StockItemIdentity("cs2", True, "2").normalized()
        malformed = StockItemIdentity("cs2", True, "2")
        self.assertFalse(malformed.matches_spec(Spec(selector={"class_id": "123"})))
        with self.assertRaises(ValueError):
            StockItemIdentity("cs2", 730, "2", class_id=123).normalized()

    def test_allowed_bot_pool_is_separate_from_item_match(self):
        spec = Spec(selector={"class_id": "123"}, allowed_bot_pool=("Bot-A",))
        self.assertTrue(self.identity.matches_spec(spec))
        self.assertTrue(self.identity.allows_bot("bot-a", spec))
        self.assertFalse(self.identity.allows_bot("Bot-B", spec))
        self.assertTrue(self.identity.allows_bot("Bot-B", Spec(selector={"class_id": "123"})))

    def test_malformed_bot_pool_fails_closed(self):
        spec = Spec(selector={"class_id": "123"}, allowed_bot_pool="Bot-A")
        self.assertFalse(self.identity.allows_bot("B", spec))


if __name__ == "__main__":
    unittest.main()
