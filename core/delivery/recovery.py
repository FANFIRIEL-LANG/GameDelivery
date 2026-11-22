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
        if (
            getattr(snapshot, "app_id", None) != app_id
            or str(getattr(snapshot, "context_id", "")) != context_id
        ):
            raise DeliveryRecoveryBlocked("ASF snapshot относится к другому app/context")
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
        if (
            not isinstance(asset_ids, list) or not asset_ids
            or any(not isinstance(value, str) or not value.strip() for value in asset_ids)
            or len(set(asset_ids)) != len(asset_ids)
        ):
            raise DeliveryRecoveryBlocked("Некорректный manifest DeliveryLeg")
        snapshot_items = getattr(snapshot, "items", None)
        if not isinstance(snapshot_items, (list, tuple)):
            raise DeliveryRecoveryBlocked("ASF snapshot не содержит полный список inventory items")
        available = {}
        for item in snapshot_items:
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
            "expires_at": (refreshed_at.astimezone(timezone.utc) + self.max_inventory_age).isoformat(),
            "asf_verified": True,
        }

    @staticmethod
    def _active_reservations(connection, target: dict[str, Any]) -> list[dict[str, Any]]:
        rows = connection.execute("""
            SELECT r.reservation_id,r.asset_id,r.state,
                   (SELECT COUNT(*) FROM gd_resource_reservations blocking
                    WHERE blocking.bot_name=r.bot_name COLLATE NOCASE
                      AND blocking.asset_id_key=r.asset_id_key
                      AND blocking.state IN ('active','legacy_conflict')) AS blocking_count
            FROM gd_resource_reservations r
            WHERE r.order_line_id=? AND r.bot_name=? COLLATE NOCASE
              AND owner_type='order' AND app_id=? AND context_id=? AND state='active'
              AND asset_id IN (SELECT value FROM json_each(?))
            ORDER BY asset_id
        """, (target["order_line_id"], target["bot_name"],
              int(target["app_id"]), str(target["context_id"]),
              target["asset_ids_json"])).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _assert_reservations_safe(
        reservations: list[dict[str, Any]], expected_assets: list[str]
    ) -> None:
        if sorted(str(row["asset_id"]) for row in reservations) != expected_assets:
            raise DeliveryRecoveryBlocked("Активные резервы не совпадают с manifest DeliveryLeg")
        if any(int(row.get("blocking_count", 0)) != 1 for row in reservations):
            raise DeliveryRecoveryBlocked("Для одного или нескольких assets обнаружен конфликтующий blocking reservation")

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
        self._assert_reservations_safe(reservations, expected_assets)
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
            self._assert_reservations_safe(current_reservations, expected_assets)
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


    def cancel_order(self, *, order_id: str, actor: str, reason: str) -> dict[str, Any]:
        """Cancel an ACTION_REQUIRED order only when every leg is proven unsent.

        Steam acceptance, in-flight/unknown offers, incomplete leg history, or
        missing/ambiguous reservations fail closed. No inventory is reassigned
        by this operation; it only releases this order's exact active reserves.
        """
        order = str(order_id).strip()
        actor, reason = str(actor).strip(), str(reason).strip()
        if not order or not actor or not reason:
            raise ValueError("order_id, actor и reason обязательны")

        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            order_row = db.execute(
                "SELECT status,recipient_steam_id64 FROM gd_orders WHERE order_id=?",
                (order,),
            ).fetchone()
            if order_row is None:
                raise DeliveryRecoveryBlocked("Заказ не найден")
            if str(order_row["status"]) != "ACTION_REQUIRED":
                raise DeliveryRecoveryBlocked("Отмена доступна только для ACTION_REQUIRED заказа")
            legs = db.execute("""
                SELECT l.*,ol.order_line_id
                FROM gd_delivery_legs l
                JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id
                WHERE ol.order_id=?
                ORDER BY l.leg_id
            """, (order,)).fetchall()
            if not legs:
                raise DeliveryRecoveryBlocked("У заказа нет DeliveryLeg; безопасно подтвердить состав отмены нельзя")

            verified_assets: list[tuple[str, str, list[str]]] = []
            for raw_leg in legs:
                leg = dict(raw_leg)
                if leg["status"] not in {"FAILED", "DECLINED"}:
                    raise DeliveryRecoveryBlocked(
                        f"Отмена заблокирована: DeliveryLeg {leg['leg_id']} не имеет доказанного FAILED/DECLINED результата"
                    )
                self._assert_latest_attempt_is_terminal(db, order, str(leg["leg_id"]))
                assets = json.loads(leg["asset_ids_json"])
                if (not isinstance(assets, list) or not assets
                    or any(not isinstance(value, str) or not value.strip() for value in assets)
                    or len(set(assets)) != len(assets)):
                    raise DeliveryRecoveryBlocked("Некорректный manifest DeliveryLeg")
                reservations = db.execute("""
                    SELECT r.reservation_id,r.asset_id,
                           (SELECT COUNT(*) FROM gd_resource_reservations blocking
                            WHERE blocking.bot_name=r.bot_name COLLATE NOCASE
                              AND blocking.asset_id_key=r.asset_id_key
                              AND blocking.state IN ('active','legacy_conflict')) AS blocking_count
                    FROM gd_resource_reservations r
                    WHERE r.order_line_id=? AND r.bot_name=? COLLATE NOCASE
                      AND r.owner_type='order' AND r.app_id=? AND r.context_id=?
                      AND r.state='active'
                      AND r.asset_id IN (SELECT value FROM json_each(?))
                    ORDER BY r.asset_id
                """, (leg["order_line_id"], leg["bot_name"], int(leg["app_id"]),
                      str(leg["context_id"]), leg["asset_ids_json"])).fetchall()
                self._assert_reservations_safe([dict(row) for row in reservations], sorted(assets))
                verified_assets.append((str(leg["leg_id"]), str(leg["bot_name"]), assets))

        # External checks are deliberately outside the SQLite write transaction.
        # Any timeout or mismatch blocks cancellation; nothing is released.
        for leg_id, bot_name, _ in verified_assets:
            self._verify_recipient(order, leg_id, order_row["recipient_steam_id64"])
            self._verify_no_offer(order, leg_id, bot_name)

        action_ids: list[str] = []
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT status FROM gd_orders WHERE order_id=?", (order,)
            ).fetchone()
            if current is None or current["status"] != "ACTION_REQUIRED":
                raise DeliveryRecoveryBlocked("Статус заказа изменился во время проверки")
            for leg_id, _, assets in verified_assets:
                leg = db.execute("""
                    SELECT l.status,l.order_line_id,l.asset_ids_json,l.bot_name,l.app_id,l.context_id
                    FROM gd_delivery_legs l
                    JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id
                    WHERE l.leg_id=? AND ol.order_id=?
                """, (leg_id, order)).fetchone()
                if leg is None or leg["status"] not in {"FAILED", "DECLINED"}:
                    raise DeliveryRecoveryBlocked("Состояние DeliveryLeg изменилось во время проверки")
                self._assert_latest_attempt_is_terminal(db, order, leg_id)
                current_reservations = db.execute("""
                    SELECT r.reservation_id,r.asset_id,
                           (SELECT COUNT(*) FROM gd_resource_reservations blocking
                            WHERE blocking.bot_name=r.bot_name COLLATE NOCASE
                              AND blocking.asset_id_key=r.asset_id_key
                              AND blocking.state IN ('active','legacy_conflict')) AS blocking_count
                    FROM gd_resource_reservations r
                    WHERE r.order_line_id=? AND r.bot_name=? COLLATE NOCASE
                      AND r.owner_type='order' AND r.app_id=? AND r.context_id=?
                      AND r.state='active'
                      AND r.asset_id IN (SELECT value FROM json_each(?))
                    ORDER BY r.asset_id
                """, (leg["order_line_id"], leg["bot_name"], int(leg["app_id"]),
                      str(leg["context_id"]), leg["asset_ids_json"])).fetchall()
                self._assert_reservations_safe([dict(row) for row in current_reservations], sorted(assets))
                action_id = "delivery-action:" + uuid.uuid4().hex
                evidence = {
                    "order_cancelled": True,
                    "offer_absence_confirmed": True,
                    "released_asset_ids": sorted(assets),
                }
                db.execute("""
                    INSERT INTO gd_delivery_operator_actions
                        (action_id,order_id,leg_id,action_type,retry_generation,actor,reason,evidence_json)
                    VALUES(?,?,?,'release',NULL,?,?,?)
                """, (action_id, order, leg_id, actor, reason,
                      json.dumps(evidence, ensure_ascii=False, sort_keys=True)))
                changed = db.execute("""
                    UPDATE gd_resource_reservations SET state='released',
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE order_line_id=? AND bot_name=? COLLATE NOCASE
                      AND owner_type='order' AND app_id=? AND context_id=?
                      AND state='active'
                      AND asset_id IN (SELECT value FROM json_each(?))
                """, (leg["order_line_id"], leg["bot_name"], int(leg["app_id"]),
                      str(leg["context_id"]), leg["asset_ids_json"])).rowcount
                if changed != len(assets):
                    raise DeliveryRecoveryBlocked("Не удалось атомарно освободить полный набор резервов")
                db.execute(
                    "UPDATE gd_delivery_legs SET status='CANCELLED',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE leg_id=?",
                    (leg_id,),
                )
                action_ids.append(action_id)
            db.execute(
                "UPDATE gd_orders SET status='CANCELLED',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE order_id=?",
                (order,),
            )
        return {"order_id": order, "status": "CANCELLED",
                "released_legs": len(verified_assets), "action_ids": action_ids}

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
        self._assert_reservations_safe(reservations, expected_assets)
        action_id = "delivery-action:" + uuid.uuid4().hex
        evidence = {"offer_absence_confirmed": True, "released_asset_ids": expected_assets}
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._load_target(db, order, leg)
            self._assert_latest_attempt_is_terminal(db, order, leg)
            current_reservations = self._active_reservations(db, current)
            self._assert_reservations_safe(current_reservations, expected_assets)
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
