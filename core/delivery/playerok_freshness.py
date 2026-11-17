"""Fail-closed, one-use proof of a fresh Playerok browser status check.

The proof is an in-process capability. It contains identifiers and timestamps only;
browser credentials and response payloads are never retained here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock, RLock
from uuid import uuid4

from integrations.playerok.browser_sales import consume_trusted_browser_sales_snapshot


class PlayerokFreshnessBlocked(ValueError):
    """A complete browser snapshot did not authorize this exact delivery leg."""


@dataclass(frozen=True)
class PlayerokFreshnessProof:
    account_user_id: str
    deal_id: str
    order_id: str
    leg_id: str
    checked_at: str
    _nonce: str


@dataclass(frozen=True)
class PlayerokOrderLease:
    order_id: str
    _nonce: str


class PlayerokFreshnessGate:
    """Validate full browser snapshots, serialize orders, and burn proof on claim."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._proofs: dict[str, PlayerokFreshnessProof] = {}
        self._order_locks: dict[str, tuple[Lock, PlayerokOrderLease | None]] = {}

    def try_acquire_order(self, order_id: str) -> PlayerokOrderLease | None:
        order = str(order_id or "").strip()
        if not order:
            return None
        with self._lock:
            entry = self._order_locks.setdefault(order, (Lock(), None))
            lock = entry[0]
            if not lock.acquire(blocking=False):
                return None
            lease = PlayerokOrderLease(order, uuid4().hex)
            self._order_locks[order] = (lock, lease)
            return lease

    def release_order(self, lease: PlayerokOrderLease | None) -> None:
        if not isinstance(lease, PlayerokOrderLease):
            return
        order = lease.order_id
        with self._lock:
            entry = self._order_locks.get(order)
            if entry is not None and entry[1] is lease and entry[0].locked():
                entry[0].release()
                self._order_locks.pop(order, None)
                for nonce, proof in tuple(self._proofs.items()):
                    if proof.order_id == order:
                        self._proofs.pop(nonce, None)

    def issue_from_complete_snapshot(
        self,
        snapshot: object,
        *,
        expected_account_user_id: str,
        deal_id: str,
        order_id: str,
        leg_id: str,
        lease: PlayerokOrderLease,
    ) -> PlayerokFreshnessProof:
        account = str(expected_account_user_id or "").strip()
        deal = str(deal_id or "").strip()
        order = str(order_id or "").strip()
        leg = str(leg_id or "").strip()
        if not all((account, deal, order, leg)):
            raise PlayerokFreshnessBlocked("Не удалось связать проверку Playerok с аккаунтом, сделкой, заказом и leg.")
        if not self._owns_lease(lease, order):
            raise PlayerokFreshnessBlocked("Отправка заказа уже выполняется или не захвачена.")
        snapshot_data = consume_trusted_browser_sales_snapshot(snapshot)
        if snapshot_data is None or snapshot_data.get("complete") is not True:
            raise PlayerokFreshnessBlocked("Полный снимок продаж Playerok не подтверждён.")
        if str(snapshot_data.get("account_user_id") or "").strip() != account:
            raise PlayerokFreshnessBlocked("Текущий аккаунт браузера Playerok не совпадает с аккаунтом заказа.")
        deals = snapshot_data.get("deals")
        if not isinstance(deals, list):
            raise PlayerokFreshnessBlocked("Список сделок Playerok отсутствует или неполон.")
        matches = [item for item in deals if isinstance(item, dict) and str(item.get("deal_id") or "").strip() == deal]
        if len(matches) != 1:
            raise PlayerokFreshnessBlocked("Целевая сделка отсутствует или неоднозначна в полном снимке Playerok.")
        if matches[0].get("status") != "PAID":
            raise PlayerokFreshnessBlocked("Свежий статус Playerok не равен PAID; отправка заблокирована.")
        nonce = uuid4().hex
        proof = PlayerokFreshnessProof(
            account, deal, order, leg,
            datetime.now(timezone.utc).isoformat(), nonce,
        )
        with self._lock:
            self._proofs[nonce] = proof
        return proof

    def consume_for_claim(
        self,
        proof: PlayerokFreshnessProof | None,
        *,
        account_user_id: str,
        deal_id: str,
        order_id: str,
        leg_id: str,
        lease: PlayerokOrderLease,
        claim,
    ):
        """Burn the capability before invoking the short atomic DB claim.

        A failed or rolled-back DB claim intentionally burns the proof. A new
        browser read is required for every subsequent attempt.
        """
        if not isinstance(proof, PlayerokFreshnessProof):
            raise PlayerokFreshnessBlocked("Перед отправкой требуется новая browser-проверка Playerok.")
        scope = (
            str(account_user_id or "").strip(), str(deal_id or "").strip(),
            str(order_id or "").strip(), str(leg_id or "").strip(),
        )
        if not all(scope):
            raise PlayerokFreshnessBlocked("Не удалось проверить привязку свежего статуса к отправке.")
        with self._lock:
            if not self._owns_lease_locked(lease, scope[2]):
                raise PlayerokFreshnessBlocked("Заказ больше не захвачен этой операцией отправки.")
            registered = self._proofs.get(proof._nonce)
            expected = (proof.account_user_id, proof.deal_id, proof.order_id, proof.leg_id)
            if registered is not proof or expected != scope:
                raise PlayerokFreshnessBlocked("Подтверждение свежести отсутствует, использовано или относится к другой leg.")
            # Burn before DB work: exceptions and SQLite rollback cannot make it reusable.
            self._proofs.pop(proof._nonce, None)
            return claim()

    def _owns_lease(self, lease: PlayerokOrderLease | None, order: str) -> bool:
        with self._lock:
            return self._owns_lease_locked(lease, order)

    def _owns_lease_locked(self, lease: PlayerokOrderLease | None, order: str) -> bool:
        if not isinstance(lease, PlayerokOrderLease) or lease.order_id != order:
            return False
        entry = self._order_locks.get(order)
        return bool(entry and entry[0].locked() and entry[1] is lease)

    def revoke(self, proof: PlayerokFreshnessProof | None) -> None:
        if isinstance(proof, PlayerokFreshnessProof):
            with self._lock:
                if self._proofs.get(proof._nonce) is proof:
                    self._proofs.pop(proof._nonce, None)
