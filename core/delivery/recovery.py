from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from core.database.db import Database


class DeliveryRecoveryBlocked(RuntimeError):
    """Raised when an operator retry/release cannot be proven safe."""


class DeliveryRecoveryService:
    """Explicit, audited retry/release workflow; never invoked by reconciliation."""

    def __init__(
        self,
        database: Database,
        *,
        inventory_provider: Callable[[str, int, str], Any],
        recipient_verifier: Callable[[str, str], str | int | None],
        offer_absence_verifier: Callable[[str, str, str], bool],
        max_inventory_age: timedelta = timedelta(minutes=2),
    ) -> None:
        self.database = database
        self.inventory_provider = inventory_provider
        self.recipient_verifier = recipient_verifier
        self.offer_absence_verifier = offer_absence_verifier
        self.max_inventory_age = max_inventory_age

    @staticmethod
    def _load_target(connection, order_id: str, leg_id: str) -> dict[str, Any]:
        row = connection.execute("""
            SELECT l.*,ol.order_id,o.status AS order_status,
                   o.recipient_steam_id64
            FROM gd_delivery_legs l
            JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id
            JOIN gd_orders o ON o.order_id=ol.order_id
            WHERE l.leg_id=? AND ol.order_id=?
        """, (leg_id, order_id)).fetchone()
        if row is None:
            raise DeliveryRecoveryBlocked("Заказ или DeliveryLeg не найден")
        result = dict(row)
        if result["order_status"] != "ACTION_REQUIRED":
            raise DeliveryRecoveryBlocked("Операторское восстановление доступно только для ACTION_REQUIRED")
        if result["status"] not in {"FAILED", "DECLINED"}:
            raise DeliveryRecoveryBlocked("Повтор или освобождение разрешены только для FAILED/DECLINED leg")
        in_flight = connection.execute(
            "SELECT 1 FROM gd_delivery_legs l "
            "JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id "
            "WHERE ol.order_id=? AND l.status IN ('UNKNOWN','SENT_TO_STEAM','DISPATCHING') LIMIT 1",
            (order_id,),
        ).fetchone()
        if in_flight:
            raise DeliveryRecoveryBlocked("Сначала необходимо завершить reconciliation всех уже начатых offers")
        return result

    def _verify_no_offer(self, order_id: str, leg_id: str, bot_name: str) -> None:
        try:
            confirmed = self.offer_absence_verifier(order_id, leg_id, bot_name)
        except Exception as exc:
            raise DeliveryRecoveryBlocked("Не удалось доказать отсутствие активного или неопределённого Steam offer") from exc
        if confirmed is not True:
            raise DeliveryRecoveryBlocked("ASF не подтвердил отсутствие активного или неопределённого Steam offer")

    def _verify_recipient(self, order_id: str, leg_id: str, expected: Any) -> str:
        if expected is None or not str(expected).strip():
            raise DeliveryRecoveryBlocked("У заказа отсутствует закреплённый Steam-получатель")
        try:
            actual = self.recipient_verifier(order_id, leg_id)
        except Exception as exc:
            raise DeliveryRecoveryBlocked("Не удалось проверить Steam Trade URL получателя") from exc
        if actual is None or str(actual) != str(expected):
            raise DeliveryRecoveryBlocked("Steam Trade URL не подтверждает закреплённого получателя")
        return str(actual)

    def _verify_inventory(self, target: dict[str, Any]) -> dict[str, Any]:
        bot = str(target["bot_name"])
        app_id = int(target["app_id"])
        context_id = str(target["context_id"])
        try:
            snapshot = self.inventory_provider(bot, app_id, context_id)
        except Exception as exc:
            raise DeliveryRecoveryBlocked("Не удалось получить свежий инвентарь ASF") from exc
        if snapshot is None or getattr(snapshot, "asf_verified", False) is not True:
            raise DeliveryRecoveryBlocked("Инвентарь не подтверждён ASF")
        if str(getattr(snapshot, "bot_name", "")).casefold() != bot.casefold():
            raise DeliveryRecoveryBlocked("Инвентарь получен не от владельца зарезервированных предметов")
        refreshed_at = getattr(snapshot, "refreshed_at", None)
        if not isinstance(refreshed_at, datetime):
            raise DeliveryRecoveryBlocked("У снимка ASF отсутствует время успешного обновления")
        if refreshed_at.tzinfo is None:
            refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - refreshed_at.astimezone(timezone.utc)
        if age < timedelta(0) or age > self.max_inventory_age:
            raise DeliveryRecoveryBlocked("Снимок ASF устарел или имеет некорректное время")
        try:
            asset_ids = json.loads(target["asset_ids_json"])
        except (TypeError, ValueError) as exc:
            raise DeliveryRecoveryBlocked("Некорректный manifest DeliveryLeg") from exc
        if not isinstance(asset_ids, list) or not asset_ids or len(set(asset_ids)) != len(asset_ids):
            raise DeliveryRecoveryBlocked("Некорректный manifest DeliveryLeg")
        available = {}
        for item in getattr(snapshot, "items", ()):
            asset_id = str(getattr(item, "asset_id", ""))
            if asset_id:
                available[asset_id] = item
        for asset_id in asset_ids:
            item = available.get(str(asset_id))
            if item is None or getattr(item, "is_tradable", None) is not True:
                raise DeliveryRecoveryBlocked(
                    "Каждый зарезервированный предмет должен присутствовать в свежем ASF inventory и иметь tradable=true"
                )
            item_bot = getattr(item, "bot_name", bot)
            if str(item_bot).casefold() != bot.casefold():
                raise DeliveryRecoveryBlocked("Предмет обнаружен не на исходном боте-владельце")
        return {
            "bot_name": bot,
            "app_id": app_id,
            "context_id": context_id,
            "asset_ids": [str(value) for value in asset_ids],
            "refreshed_at": refreshed_at.astimezone(timezone.utc).isoformat(),
            "asf_verified": True,
        }

    @staticmethod
    def _active_reservations(connection, target: dict[str, Any]) -> list[dict[str, Any]]:
        rows = connection.execute("""
            SELECT reservation_id,asset_id,state
            FROM gd_resource_reservations
            WHERE order_line_id=? AND bot_name=? COLLATE NOCASE
              AND owner_type='order' AND app_id=? AND context_id=? AND state='active'
              AND asset_id IN (SELECT value FROM json_each(?))
            ORDER BY asset_id
        """, (target["order_line_id"], target["bot_name"],
              int(target["app_id"]), str(target["context_id"]),
              target["asset_ids_json"])).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _assert_latest_attempt_is_terminal(connection, order_id: str, leg_id: str) -> int:
        rows = connection.execute("""
            SELECT retry_generation,status
            FROM gd_delivery_attempts
            WHERE order_id=? AND leg_id=?
            ORDER BY retry_generation DESC
        """, (order_id, leg_id)).fetchall()
        if not rows:
            raise DeliveryRecoveryBlocked("У leg отсутствует история DeliveryAttempt")
        if str(rows[0]["status"]).casefold() not in {"failed", "declined"}:
            raise DeliveryRecoveryBlocked("Последняя попытка не имеет доказанного терминального результата")
        if any(str(row["status"]).casefold() in {"dispatching", "unknown", "sent_to_steam"}
               for row in rows):
            raise DeliveryRecoveryBlocked("Для leg существует активная или неопределённая попытка")
        return int(rows[0]["retry_generation"])

    def retry(self, *, order_id: str, leg_id: str, actor: str, reason: str) -> dict[str, Any]:
        order, leg = str(order_id).strip(), str(leg_id).strip()
        actor, reason = str(actor).strip(), str(reason).strip()
        if not order or not leg or not actor or not reason:
            raise ValueError("order_id, leg_id, actor и reason обязательны")
        with self.database.connect() as db:
            target = self._load_target(db, order, leg)
            generation = self._assert_latest_attempt_is_terminal(db, order, leg)
            reservations = self._active_reservations(db, target)
        expected_assets = sorted(str(value) for value in json.loads(target["asset_ids_json"]))
        if sorted(row["asset_id"] for row in reservations) != expected_assets:
            raise DeliveryRecoveryBlocked("Активные резервы не совпадают с manifest DeliveryLeg")
        inventory_evidence = self._verify_inventory(target)
        recipient = self._verify_recipient(order, leg, target["recipient_steam_id64"])
        self._verify_no_offer(order, leg, str(target["bot_name"]))
        evidence = {
            "inventory": inventory_evidence,
            "recipient_steam_id64": recipient,
            "offer_absence_confirmed": True,
        }
        new_generation = generation + 1
        action_id = "delivery-action:" + uuid.uuid4().hex
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._load_target(db, order, leg)
            current_generation = self._assert_latest_attempt_is_terminal(db, order, leg)
            if current_generation != generation:
                raise DeliveryRecoveryBlocked("История попыток изменилась во время проверки; повторите операцию")
            current_reservations = self._active_reservations(db, current)
            if sorted(row["asset_id"] for row in current_reservations) != expected_assets:
                raise DeliveryRecoveryBlocked("Резервы изменились во время проверки")
            db.execute("""
                INSERT INTO gd_delivery_operator_actions
                    (action_id,order_id,leg_id,action_type,retry_generation,actor,reason,evidence_json)
                VALUES(?,?,?,'retry',?,?,?,?)
            """, (action_id, order, leg, new_generation, actor, reason,
                  json.dumps(evidence, ensure_ascii=False, sort_keys=True)))
            attempt_key = f"{order}:{leg}:{new_generation}"
            db.execute("""
                INSERT INTO gd_delivery_attempts
                    (attempt_id,order_id,leg_id,retry_generation,idempotency_key,status,result_json)
                VALUES(?,?,?,?,?,'prepared',?)
            """, (
                f"attempt:{attempt_key}", order, leg, new_generation, attempt_key,
                json.dumps({"operator_retry_action_id": action_id,
                            "operator_actor": actor, "operator_reason": reason,
                            "recovery_evidence": evidence},
                           ensure_ascii=False, sort_keys=True),
            ))
            db.execute(
                "UPDATE gd_delivery_legs SET status='reserved',trade_offer_id=NULL,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE leg_id=?",
                (leg,),
            )
            db.execute(
                "UPDATE gd_orders SET status='PROCESSING',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE order_id=?",
                (order,),
            )
        return {"action_id": action_id, "order_id": order, "leg_id": leg,
                "status": "reserved", "retry_generation": new_generation}

    def release(self, *, order_id: str, leg_id: str, actor: str, reason: str) -> dict[str, Any]:
        order, leg = str(order_id).strip(), str(leg_id).strip()
        actor, reason = str(actor).strip(), str(reason).strip()
        if not order or not leg or not actor or not reason:
            raise ValueError("order_id, leg_id, actor и reason обязательны")
        with self.database.connect() as db:
            target = self._load_target(db, order, leg)
            self._assert_latest_attempt_is_terminal(db, order, leg)
            reservations = self._active_reservations(db, target)
        self._verify_recipient(order, leg, target["recipient_steam_id64"])
        self._verify_no_offer(order, leg, str(target["bot_name"]))
        expected_assets = sorted(str(value) for value in json.loads(target["asset_ids_json"]))
        if sorted(row["asset_id"] for row in reservations) != expected_assets:
            raise DeliveryRecoveryBlocked("Активные резервы не совпадают с manifest DeliveryLeg")
        action_id = "delivery-action:" + uuid.uuid4().hex
        evidence = {"offer_absence_confirmed": True, "released_asset_ids": expected_assets}
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._load_target(db, order, leg)
            self._assert_latest_attempt_is_terminal(db, order, leg)
            current_reservations = self._active_reservations(db, current)
            if sorted(row["asset_id"] for row in current_reservations) != expected_assets:
                raise DeliveryRecoveryBlocked("Резервы изменились во время проверки")
            db.execute("""
                INSERT INTO gd_delivery_operator_actions
                    (action_id,order_id,leg_id,action_type,retry_generation,actor,reason,evidence_json)
                VALUES(?,?,?,'release',NULL,?,?,?)
            """, (action_id, order, leg, actor, reason,
                  json.dumps(evidence, ensure_ascii=False, sort_keys=True)))
            cursor = db.execute("""
                UPDATE gd_resource_reservations SET state='released',
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE order_line_id=? AND bot_name=? COLLATE NOCASE
                  AND owner_type='order' AND app_id=? AND context_id=? AND state='active'
                  AND asset_id IN (SELECT value FROM json_each(?))
            """, (current["order_line_id"], current["bot_name"],
                  int(current["app_id"]), str(current["context_id"]),
                  current["asset_ids_json"]))
            if cursor.rowcount != len(expected_assets):
                raise DeliveryRecoveryBlocked("Не удалось атомарно освободить полный набор резервов")
        return {"action_id": action_id, "order_id": order, "leg_id": leg,
                "status": "released", "released_asset_ids": expected_assets}
