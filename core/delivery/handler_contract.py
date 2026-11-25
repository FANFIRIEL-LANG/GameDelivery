from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


HANDLER_CONTRACT_VERSION = 1


@dataclass(frozen=True, slots=True)
class DeliveryHandlerKey:
    """Stable routing key; product display names are deliberately excluded."""

    item_type: str
    delivery_method: str

    def __post_init__(self) -> None:
        item_type = self.item_type.strip().casefold() if isinstance(self.item_type, str) else ""
        delivery_method = (
            self.delivery_method.strip().casefold()
            if isinstance(self.delivery_method, str) else ""
        )
        if not item_type or not delivery_method:
            raise ValueError("item_type и delivery_method обязательны")
        object.__setattr__(self, "item_type", item_type)
        object.__setattr__(self, "delivery_method", delivery_method)


@dataclass(frozen=True, slots=True)
class DeliveryHandlerResult:
    """Normalized result returned by a handler lifecycle operation."""

    status: str
    retryable: bool = False
    external_action_started: bool = False
    external_reference: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        status = self.status.strip().upper() if isinstance(self.status, str) else ""
        if not status:
            raise ValueError("status обязателен")
        if type(self.retryable) is not bool or type(self.external_action_started) is not bool:
            raise TypeError("retryable и external_action_started должны быть bool")
        reference = self.external_reference
        if reference is not None:
            if not isinstance(reference, str) or not reference.strip():
                raise ValueError("external_reference должен быть непустой строкой или None")
            reference = reference.strip()
        if not isinstance(self.reason, str):
            raise TypeError("reason должен быть строкой")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "external_reference", reference)


@runtime_checkable
class DeliveryHandlerV1(Protocol):
    """Versioned lifecycle contract. Implementations own item-specific rules."""

    contract_version: int
    key: DeliveryHandlerKey

    def supports(self, request: Mapping[str, Any]) -> bool: ...
    def preview(self, request: Mapping[str, Any]) -> DeliveryHandlerResult: ...
    def prepare(self, request: Mapping[str, Any]) -> DeliveryHandlerResult: ...
    def execute(self, prepared: Mapping[str, Any]) -> DeliveryHandlerResult: ...
    def reconcile(self, attempt: Mapping[str, Any]) -> DeliveryHandlerResult: ...


class DeliveryHandlerRegistry:
    """Exact-key registry; no fallback by display name or guessed item category."""

    def __init__(self) -> None:
        self._handlers: dict[DeliveryHandlerKey, DeliveryHandlerV1] = {}

    def register(self, handler: DeliveryHandlerV1) -> None:
        if getattr(handler, "contract_version", None) != HANDLER_CONTRACT_VERSION:
            raise ValueError("Неподдерживаемая версия DeliveryHandler contract")
        key = getattr(handler, "key", None)
        if not isinstance(key, DeliveryHandlerKey):
            raise TypeError("handler.key должен быть DeliveryHandlerKey")
        missing = [
            name for name in ("supports", "preview", "prepare", "execute", "reconcile")
            if not callable(getattr(handler, name, None))
        ]
        if missing:
            raise TypeError("handler не реализует обязательные методы: " + ", ".join(missing))
        if key in self._handlers:
            raise ValueError(f"Обработчик уже зарегистрирован: {key.item_type}/{key.delivery_method}")
        self._handlers[key] = handler

    def resolve(
        self, *, item_type: str, delivery_method: str, request: Mapping[str, Any]
    ) -> DeliveryHandlerV1:
        key = DeliveryHandlerKey(item_type, delivery_method)
        handler = self._handlers.get(key)
        if handler is None:
            raise LookupError(
                f"Нет обработчика для {key.item_type}/{key.delivery_method}"
            )
        if not handler.supports(request):
            raise LookupError(
                f"Обработчик {key.item_type}/{key.delivery_method} отклонил запрос"
            )
        return handler

    def registered_keys(self) -> tuple[DeliveryHandlerKey, ...]:
        return tuple(sorted(
            self._handlers,
            key=lambda key: (key.item_type, key.delivery_method),
        ))
