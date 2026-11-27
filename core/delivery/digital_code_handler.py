from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.delivery.handler_contract import (
    HANDLER_CONTRACT_VERSION, DeliveryHandlerKey, DeliveryHandlerResult,
)


class DigitalCodeHandler:
    """Operator-assisted code route; it never emits a secret or performs I/O."""

    contract_version = HANDLER_CONTRACT_VERSION
    key = DeliveryHandlerKey("digital_text_unit", "text_delivery")

    def supports(self, request: Mapping[str, Any]) -> bool:
        return (request.get("item_type") == "digital_text_unit"
                and request.get("delivery_method") == "text_delivery"
                and bool(request.get("order_id")) and bool(request.get("leg_id")))

    def preview(self, request: Mapping[str, Any]) -> DeliveryHandlerResult:
        if not self.supports(request):
            return DeliveryHandlerResult("UNSUPPORTED", reason="Маршрут цифрового кода не подтверждён")
        return DeliveryHandlerResult("READY", reason="Код зарезервирован; требуется явное раскрытие оператором")

    def prepare(self, request: Mapping[str, Any]) -> DeliveryHandlerResult:
        preview = self.preview(request)
        return DeliveryHandlerResult("PREPARED" if preview.status == "READY" else preview.status, reason=preview.reason)

    def execute(self, prepared: Mapping[str, Any]) -> DeliveryHandlerResult:
        if not self.supports(prepared):
            return DeliveryHandlerResult("UNSUPPORTED", reason="Маршрут цифрового кода не подтверждён")
        return DeliveryHandlerResult("ACTION_REQUIRED", reason="Раскрытие кода выполняется отдельной подтверждённой операторской операцией")

    def reconcile(self, attempt: Mapping[str, Any]) -> DeliveryHandlerResult:
        if not self.supports(attempt):
            return DeliveryHandlerResult("UNSUPPORTED", reason="Маршрут цифрового кода не подтверждён")
        return DeliveryHandlerResult("ACTION_REQUIRED", reason="Код требует ручной сверки")
