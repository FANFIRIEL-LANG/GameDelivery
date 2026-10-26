from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


NORMAL_BADGE_MAX_LEVEL = 5


@dataclass(frozen=True)
class RecipientBadge:
    """A badge observed on the recipient's Steam profile."""

    game_name: str
    level: int
    is_foil: bool = False


@dataclass(frozen=True)
class BadgeCapacity:
    """Safe result for deciding whether a normal card-set order fits."""

    game_name: str
    current_level: int | None
    max_level: int
    remaining_sets: int | None
    requested_sets: int
    allowed_sets: int
    can_proceed: bool
    reason: str


def evaluate_recipient_badge_capacity(
    game_name: str,
    requested_sets: int,
    badges: Iterable[RecipientBadge],
    *,
    badge_data_verified: bool,
) -> BadgeCapacity:
    """Limit normal card sets by the recipient's verified game-badge level.

    A verified absence of a matching badge means level 0. Missing/unverified
    profile data, ambiguous matches, and foil badges fail closed. This helper
    does not contact Steam or send trades.
    """
    normalized_name = game_name.strip().casefold()
    if not normalized_name:
        raise ValueError("Название игры не должно быть пустым")
    if isinstance(requested_sets, bool) or not isinstance(requested_sets, int) or requested_sets < 1:
        raise ValueError("Количество комплектов должно быть целым числом больше нуля")

    if not badge_data_verified:
        return BadgeCapacity(
            game_name=game_name.strip(), current_level=None, max_level=NORMAL_BADGE_MAX_LEVEL,
            remaining_sets=None, requested_sets=requested_sets, allowed_sets=0,
            can_proceed=False, reason="Данные значка получателя не подтверждены",
        )

    matches = [
        badge for badge in badges
        if badge.game_name.strip().casefold() == normalized_name
    ]
    if len(matches) > 1:
        return BadgeCapacity(
            game_name=game_name.strip(), current_level=None, max_level=NORMAL_BADGE_MAX_LEVEL,
            remaining_sets=None, requested_sets=requested_sets, allowed_sets=0,
            can_proceed=False, reason="Найдено несколько совпадающих значков; нужна проверка",
        )
    if matches and matches[0].is_foil:
        return BadgeCapacity(
            game_name=game_name.strip(), current_level=matches[0].level,
            max_level=NORMAL_BADGE_MAX_LEVEL, remaining_sets=None,
            requested_sets=requested_sets, allowed_sets=0, can_proceed=False,
            reason="Обнаружен фольгированный значок; обычные комплекты не рассчитываются",
        )

    level = matches[0].level if matches else 0
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= NORMAL_BADGE_MAX_LEVEL:
        return BadgeCapacity(
            game_name=game_name.strip(), current_level=None, max_level=NORMAL_BADGE_MAX_LEVEL,
            remaining_sets=None, requested_sets=requested_sets, allowed_sets=0,
            can_proceed=False, reason="Уровень значка некорректен",
        )

    remaining = NORMAL_BADGE_MAX_LEVEL - level
    allowed = min(requested_sets, remaining)
    if remaining == 0:
        reason = "Обычный значок уже максимального уровня"
    elif requested_sets > remaining:
        reason = f"Можно отправить не более {remaining} комплектов"
    else:
        reason = "Количество комплектов соответствует уровню значка"

    return BadgeCapacity(
        game_name=game_name.strip(), current_level=level, max_level=NORMAL_BADGE_MAX_LEVEL,
        remaining_sets=remaining, requested_sets=requested_sets, allowed_sets=allowed,
        can_proceed=allowed == requested_sets, reason=reason,
    )
