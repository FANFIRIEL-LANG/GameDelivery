from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.delivery.handler_contract import (
    HANDLER_CONTRACT_VERSION,
    DeliveryHandlerKey,
    DeliveryHandlerResult,
    DeliveryHandlerRegistry,
)


class ASFTradeOfferHandler:
    """Adapter that preserves the existing guarded ASF dispatch/reconcile paths."""

    contract_version = HANDLER_CONTRACT_VERSION

    def __init__(
        self,
        *,
        item_type: str,
        execute_callback: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
        reconcile_callback: Callable[[Mapping[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.key = DeliveryHandlerKey(item_type, "steam_trade_offer")
        self._execute_callback = execute_callback
        self._reconcile_callback = reconcile_callback

    def supports(self, request: Mapping[str, Any]) -> bool:
        return (
            request.get("item_type") == self.key.item_type
            and request.get("delivery_method") == self.key.delivery_method
            and isinstance(request.get("order_id"), str)
            and bool(request.get("order_id"))
            and isinstance(request.get("leg_id"), str)
            and bool(request.get("leg_id"))
        )

    def preview(self, request: Mapping[str, Any]) -> DeliveryHandlerResult:
        return DeliveryHandlerResult(
            "READY" if self.supports(request) else "UNSUPPORTED",
            reason="" if self.supports(request) else "Маршрут доставки не поддерживается",
        )

    def prepare(self, request: Mapping[str, Any]) -> DeliveryHandlerResult:
        preview = self.preview(request)
        return DeliveryHandlerResult(
            "PREPARED" if preview.status == "READY" else preview.status,
            reason=preview.reason,
        )

    def execute(self, prepared: Mapping[str, Any]) -> DeliveryHandlerResult:
        if not self.supports(prepared) or self._execute_callback is None:
            return DeliveryHandlerResult("UNSUPPORTED", reason="Исполнитель доставки недоступен")
        outcome = self._execute_callback(prepared)
        status = str(outcome.get("status") or "UNKNOWN")
        return DeliveryHandlerResult(
            status,
            external_action_started=bool(outcome.get("dispatched"))
            or status in {"SENT_TO_STEAM", "UNKNOWN", "PRIVACY_WAIT", "TRADE_RESTRICTED"},
            external_reference=outcome.get("trade_offer_id"),
            reason=str(outcome.get("reason") or ""),
            details=outcome,
        )

    def reconcile(self, attempt: Mapping[str, Any]) -> DeliveryHandlerResult:
        if not self.supports(attempt) or self._reconcile_callback is None:
            return DeliveryHandlerResult("UNSUPPORTED", reason="Сверка маршрута недоступна")
        rows = self._reconcile_callback(attempt)
        match = next((row for row in rows if row.get("leg_id") == attempt.get("leg_id")), None)
        if match is None:
            return DeliveryHandlerResult("UNKNOWN", reason="Сверка не вернула эту DeliveryLeg")
        return DeliveryHandlerResult(
            str(match.get("status") or "UNKNOWN"),
            reason=str(match.get("reason") or ""),
            external_reference=match.get("trade_offer_id"),
            details=match,
        )


def register_asf_trade_offer_handlers(
    registry: DeliveryHandlerRegistry,
    *,
    execute_callback: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    reconcile_callback: Callable[[Mapping[str, Any]], list[dict[str, Any]]] | None = None,
) -> None:
    for item_type in ("steam_asset", "steam_card_set"):
        registry.register(ASFTradeOfferHandler(
            item_type=item_type,
            execute_callback=execute_callback,
            reconcile_callback=reconcile_callback,
        ))
