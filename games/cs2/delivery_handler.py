from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core.delivery.asf_trade_handler import ASFTradeOfferHandler
from core.delivery.handler_contract import DeliveryHandlerRegistry


@dataclass(frozen=True, slots=True)
class CS2CapabilityAcceptance:
    """Explicit evidence required before enabling the CS2 delivery route."""

    fresh_inventory_verified: bool = False
    tradable_and_restriction_verified: bool = False
    preview_verified: bool = False
    test_account_send_verified: bool = False

    def __post_init__(self) -> None:
        for name in (
            "fresh_inventory_verified", "tradable_and_restriction_verified",
            "preview_verified", "test_account_send_verified",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")

    @property
    def accepted(self) -> bool:
        return all((
            self.fresh_inventory_verified,
            self.tradable_and_restriction_verified,
            self.preview_verified,
            self.test_account_send_verified,
        ))

    def missing(self) -> tuple[str, ...]:
        checks = (
            ("fresh_inventory", self.fresh_inventory_verified),
            ("tradable_restriction", self.tradable_and_restriction_verified),
            ("preview", self.preview_verified),
            ("test_account_send", self.test_account_send_verified),
        )
        return tuple(name for name, passed in checks if not passed)


class CS2TradeOfferHandler(ASFTradeOfferHandler):
    """Dedicated CS2 route; it reuses the guarded ASF core after acceptance."""

    def __init__(self, *, capability: CS2CapabilityAcceptance, **kwargs: Any) -> None:
        if not capability.accepted:
            missing = ", ".join(capability.missing()) or "capability"
            raise ValueError(f"CS2 delivery capability is not accepted: {missing}")
        super().__init__(item_type="cs2_item", **kwargs)
        self.capability = capability

    def supports(self, request: Mapping[str, Any]) -> bool:
        if not super().supports(request):
            return False
        try:
            return int(request.get("app_id")) == 730 and str(request.get("context_id")) == "2"
        except (TypeError, ValueError):
            return False


def register_cs2_trade_offer_handler(
    registry: DeliveryHandlerRegistry,
    *,
    capability: CS2CapabilityAcceptance,
    execute_callback=None,
    reconcile_callback=None,
) -> bool:
    """Register CS2 only after all explicit acceptance evidence is present."""
    if not capability.accepted:
        return False
    registry.register(CS2TradeOfferHandler(
        capability=capability,
        execute_callback=execute_callback,
        reconcile_callback=reconcile_callback,
    ))
    return True
