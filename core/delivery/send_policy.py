from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from uuid import uuid4


@dataclass(frozen=True)
class ManualDispatchAuthorization:
    order_id: str
    authorization_id: str
    leg_id: str


class DeliverySendPolicy:
    """Fail-closed global auto-send setting and one-use, order-scoped manual grants."""

    SETTING_KEY = "delivery/auto_send_enabled"

    def __init__(self, settings) -> None:
        self._settings = settings
        self._lock = RLock()
        self._manual_grants: dict[str, tuple[str, set[str]]] = {}
        self._storage_unhealthy = False
        self.reset_off_on_start()

    def _sync(self) -> None:
        clear_status = getattr(self._settings, "clearStatus", None)
        if callable(clear_status):
            clear_status()
        self._settings.sync()
        status = self._settings.status()
        if status != 0:
            raise OSError("Не удалось надёжно сохранить настройку автоматической отправки.")

    def _write(self, enabled: bool) -> bool:
        try:
            self._settings.setValue(self.SETTING_KEY, enabled)
            self._sync()
            saved = self._settings.value(self.SETTING_KEY, False)
            if type(saved) is not bool or saved is not enabled:
                raise OSError("Не удалось подтвердить сохранение настройки отправки.")
        except Exception:
            self._storage_unhealthy = True
            return False
        self._storage_unhealthy = False
        return True

    def reset_off_on_start(self) -> bool:
        """Persist OFF at every app start; a storage error still keeps this instance OFF."""
        with self._lock:
            return self._write(False)

    def set_auto_send_enabled(self, enabled: bool) -> bool:
        if type(enabled) is not bool:
            raise TypeError("enabled должен иметь тип bool")
        with self._lock:
            if self._write(enabled):
                return True
            if enabled:
                # Best effort to leave a safe persisted value after a failed enable.
                self._write(False)
            self._storage_unhealthy = True
            return False

    def auto_send_enabled(self) -> bool:
        with self._lock:
            return self._auto_send_enabled_locked()

    def _auto_send_enabled_locked(self) -> bool:
        if self._storage_unhealthy:
            return False
        try:
            self._sync()
            value = self._settings.value(self.SETTING_KEY, False)
            if type(value) is not bool:
                return False
            return value is True
        except Exception:
            self._storage_unhealthy = True
            return False

    def run_auto_dispatch_claim(self, claim):
        """Linearize a short DB dispatch claim against the UI's OFF toggle."""
        with self._lock:
            if not self._auto_send_enabled_locked():
                return False, None
            return True, claim()

    def issue_manual_authorization(
        self, order_id: str, leg_id: str
    ) -> ManualDispatchAuthorization:
        order = str(order_id or "").strip()
        leg = str(leg_id or "").strip()
        if not order or not leg:
            raise ValueError("Для ручного разрешения нужны order_id и конкретная DeliveryLeg.")
        authorization_id = uuid4().hex
        grant = ManualDispatchAuthorization(order, authorization_id, leg)
        with self._lock:
            self._manual_grants[authorization_id] = (order, {leg})
        return grant

    def validate_manual_authorization(
        self, grant: ManualDispatchAuthorization | None, order_id: str, leg_id: str
    ) -> bool:
        if not isinstance(grant, ManualDispatchAuthorization):
            return False
        order, leg = str(order_id or "").strip(), str(leg_id or "").strip()
        with self._lock:
            return self._valid_manual_locked(grant, order, leg)

    def _valid_manual_locked(
        self, grant: ManualDispatchAuthorization | None, order: str, leg: str
    ) -> bool:
        if not isinstance(grant, ManualDispatchAuthorization):
            return False
        registered = self._manual_grants.get(grant.authorization_id)
        return bool(
            registered
            and grant.order_id == order == registered[0]
            and leg == grant.leg_id
            and leg in registered[1]
        )

    def run_manual_dispatch_claim(self, grant, order_id: str, leg_id: str, claim):
        """Run the final DB compare-and-set under a one-use scoped confirmation."""
        order, leg = str(order_id or "").strip(), str(leg_id or "").strip()
        with self._lock:
            if not self._valid_manual_locked(grant, order, leg):
                return False, None
            result = claim()
            if isinstance(result, dict) and result.get("_claimed") is True:
                self._consume_manual_locked(grant, leg)
            return True, result

    def _consume_manual_locked(self, grant: ManualDispatchAuthorization, leg: str) -> None:
        registered = self._manual_grants[grant.authorization_id]
        registered[1].remove(leg)
        if not registered[1]:
            self._manual_grants.pop(grant.authorization_id, None)

    def consume_manual_authorization(
        self, grant: ManualDispatchAuthorization | None, order_id: str, leg_id: str
    ) -> bool:
        if not isinstance(grant, ManualDispatchAuthorization):
            return False
        order, leg = str(order_id or "").strip(), str(leg_id or "").strip()
        with self._lock:
            if not self._valid_manual_locked(grant, order, leg):
                return False
            self._consume_manual_locked(grant, leg)
            return True

    def revoke_manual_authorization(self, grant: ManualDispatchAuthorization | None) -> None:
        if isinstance(grant, ManualDispatchAuthorization):
            with self._lock:
                self._manual_grants.pop(grant.authorization_id, None)
