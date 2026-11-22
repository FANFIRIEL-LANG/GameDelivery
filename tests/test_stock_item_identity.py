from dataclasses import dataclass

import pytest

from core.delivery.stock_identity import StockItemIdentity


@dataclass(frozen=True)
class Spec:
    game_id: str = "cs2"
    app_id: int = 730
    context_id: str = "2"
    selector: dict | None = None
    allowed_bot_pool: tuple[str, ...] = ()


@pytest.fixture
def identity():
    return StockItemIdentity(
        game_id="cs2", app_id=730, context_id="2",
        market_hash_name="AK-47 | Redline (Field-Tested)",
        name="AK-47 | Redline", item_type="weapon", class_id="123",
        quality="Field-Tested", exterior="Field-Tested", rarity="Classified",
    )


def test_exact_selector_match_including_every_supplied_field(identity):
    spec = Spec(selector={"market_hash_name": "AK-47 | Redline (Field-Tested)", "class_id": "123", "quality": "Field-Tested"})
    assert identity.matches_spec(spec)


@pytest.mark.parametrize("selector", [
    {"market_hash_name": "AK-47 | Redline"}, {"class_id": "124"},
    {"name": "AK-47 | Redline "}, {"rarity": "Covert"},
])
def test_selector_mismatch_fails_closed(identity, selector):
    assert not identity.matches_spec(Spec(selector=selector))


def test_missing_identity_field_does_not_match():
    incomplete = StockItemIdentity(game_id="cs2", app_id=730, context_id="2", market_hash_name="AK-47 | Redline (Field-Tested)")
    assert not incomplete.matches_spec(Spec(selector={"name": "AK-47 | Redline"}))


def test_game_app_context_scope_must_match(identity):
    assert not identity.matches_spec(Spec(game_id="dota2", selector={"class_id": "123"}))
    assert not identity.matches_spec(Spec(app_id=570, selector={"class_id": "123"}))
    assert not identity.matches_spec(Spec(context_id="6", selector={"class_id": "123"}))


def test_placeholder_name_is_not_identity():
    item = StockItemIdentity(game_id="cs2", app_id=730, context_id="2", name="Unknown item 7656119").normalized()
    assert item.name == ""
    assert not item.matches_spec(Spec(selector={"name": "Unknown item 7656119"}))


def test_invalid_identity_values_are_rejected():
    with pytest.raises(ValueError):
        StockItemIdentity("cs2", True, "2").normalized()
    with pytest.raises(ValueError):
        StockItemIdentity("cs2", 730, "2", class_id=123).normalized()


def test_allowed_bot_pool_is_separate_from_item_match(identity):
    spec = Spec(selector={"class_id": "123"}, allowed_bot_pool=("Bot-A",))
    assert identity.matches_spec(spec)
    assert identity.allows_bot("bot-a", spec)
    assert not identity.allows_bot("Bot-B", spec)
    assert identity.allows_bot("Bot-B", Spec(selector={"class_id": "123"}))
